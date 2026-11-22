"""Tests for Codex JSONL backend parsing, LocalSandboxAdapter, ConsoleNotifier, SandboxSession, and Sandbox UI."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from watchpost.adapters.local_sandbox import LocalSandboxAdapter
from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.models import AutonomyMode, ChannelConfig, ThreadRecord, ThreadStatus, TriageMode
from watchpost.notifiers.console import ConsoleNotifier
from watchpost.sandbox import SandboxSession
from watchpost.sandbox_ui import create_sandbox_ui_app


def test_codex_jsonl_parsing_and_readonly_sandbox_flag() -> None:
    async def _run() -> None:
        cli = CLIAgentBackend("codex")
        captured_cmds: list[list[str]] = []

        async def fake_exec(*cmd: str, **kwargs):
            del kwargs
            captured_cmds.append(list(cmd))
            proc = mock.AsyncMock()
            proc.returncode = 0
            if len(captured_cmds) == 1:
                events = "\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "019a0000-1111-7222-8333-444455556666"}),
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "id": "item_1",
                                    "type": "agent_message",
                                    "text": "总结：\n日志定位到握手超时。\n\n建议回复：\n我刚核查过日志，先按新参数测一下。\n\n需要注意：\n无",
                                },
                            }
                        ),
                        json.dumps({"type": "turn.completed"}),
                    ]
                )
            else:
                events = json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_2",
                            "type": "agent_message",
                            "text": "总结：\n已按批准方案完成调整。",
                        },
                    }
                )
            proc.communicate = mock.AsyncMock(return_value=(events.encode("utf-8"), b""))
            return proc

        ch = ChannelConfig(
            id="ch_ro",
            adapter="sandbox",
            external_chat_id="sandbox_dm",
            name="Alex",
            autonomy_mode=AutonomyMode.diagnose_only,
        )
        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            ref, res = await cli.start_thread("Test", "Prompt body", cwd=Path("."), channel=ch)
            assert ref == "019a0000-1111-7222-8333-444455556666"
            assert "--sandbox" in captured_cmds[0]
            idx = captured_cmds[0].index("--sandbox")
            assert captured_cmds[0][idx + 1] == "read-only"
            assert "日志定位到握手超时" in res.summary_bubbles[0]
            assert "我刚核查过日志" in res.suggested_reply
            assert res.needs_attention == ""

            steer_res = await cli.steer_thread(ref, "Test", "go", cwd=Path("."), channel=ch)
            assert captured_cmds[1][:6] == ["codex", "exec", "resume", "--json", "--skip-git-repo-check", ref]
            assert "已按批准方案完成调整" in steer_res.summary_bubbles[0]

    asyncio.run(_run())


def test_local_sandbox_adapter_and_console_notifier() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        img_src = root / "bay2_photo.png"
        img_src.write_bytes(b"FAKEPNGDATA")

        adapter = LocalSandboxAdapter(root / "sb")
        m1 = adapter.append_message("sandbox_group", sender_name="Sam", text="photo", image_path=img_src)
        m2 = adapter.append_message("sandbox_group", sender_name="Alex", text="check", reply_to_id=m1.message_id, mentions_owner=True)
        adapter.recall_message("sandbox_group", m1.message_id)

        ch = ChannelConfig(id="sandbox_group", adapter="sandbox", external_chat_id="sandbox_group", name="Ops")
        recent = asyncio.run(adapter.fetch_recent(ch))
        assert len(recent) == 3
        assert recent[0].deleted is True
        assert recent[2].deleted is True

        by_id = asyncio.run(adapter.fetch_by_ids(ch, [m2.message_id]))
        assert len(by_id) == 1 and by_id[0].reply_to_id == m1.message_id

        arts = asyncio.run(adapter.download_media(ch, m1, root / "dl"))
        assert len(arts) == 1 and Path(arts[0].path).read_bytes() == b"FAKEPNGDATA"

    buf = io.StringIO()
    ntf = ConsoleNotifier(stream=buf, language="zh")
    th = ThreadRecord(
        id="th_1",
        channel_id="ch_1",
        backend="mock",
        backend_thread_ref="r1",
        status=ThreadStatus.awaiting_go,
        triage_mode=TriageMode.mode_4_bug,
        pending_go=True,
    )
    toks = asyncio.run(ntf.notify_completion(ch, th, ["Summary bubble", "Draft reply"]))
    assert toks == ["c-1", "c-2"]
    assert "c-2" in buf.getvalue()
    assert "/phone go" in buf.getvalue()

    ntf.enqueue_owner_text("go", reply_token="c-2")
    actions = asyncio.run(ntf.poll_owner_actions())
    assert len(actions) == 1 and actions[0].kind == "approve_go" and actions[0].reply_token == "c-2"


def test_interactive_sandbox_session_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        img_path = root / "hmi_timeout.png"
        img_path.write_bytes(b"IMG_BYTES")
        out = io.StringIO()

        session = SandboxSession(root / "state", backend_name="mock", workspace_dir=root, stream=out)

        async def _drive() -> None:
            # 1. Small talk ignored in DM
            assert await session.handle_line("OK") is True
            assert "Ignored" in out.getvalue()

            # 2. Switch to @me group, drop Sam's screenshot without @owner -> stored, not triggered
            assert await session.handle_line("/mode group") is True
            assert await session.handle_line("/as Sam") is True
            assert await session.handle_line(f"/img {img_path} Bay 2 handshake timed out after 690s @Alex") is True
            assert "not triggered" in out.getvalue()

            # 3. Alex escalates with /at -> downloads Sam's image, diagnoses bug, prints awaiting_go bubbles
            assert await session.handle_line("/as Alex") is True
            assert await session.handle_line("/at 昨晚 Bay 2 报错超时麻烦看一下") is True
            threads = session.store.list_threads()
            assert len(threads) == 1
            th = threads[0]
            assert th.triage_mode == TriageMode.mode_4_bug
            assert th.pending_go is True

            # Find bubble token from notifier history bound to th.id
            bound_tokens = [tok for tok, _ in session.notifier.history if session.store.resolve_outbound("console", tok) == th.id]
            assert bound_tokens

            # 4. Owner checks status and approves via @token go
            assert await session.handle_line("/phone status") is True
            assert "待确认执行" in out.getvalue()
            assert await session.handle_line(f"/phone @{bound_tokens[-1]} go") is True

            updated = session.store.get_thread(th.id)
            assert updated is not None
            assert updated.pending_go is False
            assert updated.status == ThreadStatus.completed

            # 5. Recall ignores cleanly
            assert await session.handle_line("/recall sb_2") is True
            assert "Ignored recalled message" in out.getvalue()

            # 6. /quit returns False cleanly
            assert await session.handle_line("/quit") is False

        asyncio.run(_drive())


def test_sandbox_ui_routes_ime_guidance_filter_and_phone_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        app = create_sandbox_ui_app(
            root / "ui",
            backend_name="mock",
            workspace_dir=root,
            debounce_s=0.0,
        )
        client = TestClient(app)

        # Verify security headers, modern guidance patterns, and zero innerHTML sinks
        html_res = client.get("/")
        assert html_res.status_code == 200
        assert html_res.headers["X-Content-Type-Options"] == "nosniff"
        assert html_res.headers["X-Frame-Options"] == "DENY"
        assert "script-src 'nonce-" in html_res.headers["Content-Security-Policy"]
        assert '<meta name="color-scheme" content="light dark">' in html_res.text
        assert "event.isComposing || event.keyCode === 229" in html_res.text
        assert 'command' in html_res.text and "'--copy'" in html_res.text
        assert "field-sizing: content" in html_res.text
        assert "innerHTML" not in html_res.text

        # 1. Bare question mark filtered cleanly as small talk
        bare = client.post("/api/colleague/send", json={"mode": "dm", "text": "？", "immediate": True})
        assert bare.status_code == 200
        st0 = client.get("/api/state").json()
        assert len(st0["threads"]) == 0
        assert st0["messages"][0]["status_label"] == "ignored (small talk)"

        # 2. Switch to group, send screenshot + real question mentioning owner
        assert client.post("/api/settings", json={"mode": "group"}).status_code == 200
        tiny_png = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEPNG").decode("ascii")
        real = client.post(
            "/api/colleague/send",
            json={
                "mode": "group",
                "sender_name": "Sam",
                "text": "昨晚 Bay 2 报错超时麻烦看一下",
                "mentions_owner": True,
                "immediate": True,
                "image_data_url": f"data:image/png;base64,{tiny_png}",
                "image_name": "bay2.png",
            },
        )
        assert real.status_code == 200
        st1 = client.get("/api/state").json()
        assert len(st1["threads"]) == 1
        assert st1["has_pending_go"] is True
        assert any(b["kind"] == "draft" for b in st1["phone_bubbles"])
        img_url = next(m["image_url"] for m in st1["messages"] if m["image_url"])
        assert client.get(img_url).status_code == 200

        # 3. Phone status + "go" approval updates thread to completed
        assert client.post("/api/phone/send", json={"text": "/status", "wait": True}).status_code == 200
        assert client.post("/api/phone/send", json={"text": "go", "wait": True}).status_code == 200
        st2 = client.get("/api/state").json()
        assert st2["has_pending_go"] is False
        assert st2["threads"][0]["status"] == "completed"
        assert any(b["kind"] == "status" and "Watchpost 运行中" in b["text"] for b in st2["phone_bubbles"])
