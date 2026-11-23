"""Tests for Watchpost v0.3.0 parity features: Antigravity, Fallback, lark-cli, plugins, iMessage & commands."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.backends.antigravity import (
    AntigravityBackend,
    extract_latest_assistant_response,
    handle_stop_hook_payload,
)
from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.backends.fallback import FallbackBackend
from watchpost.cli import main as cli_main
from watchpost.config import (
    BackendConfig,
    ExternalAdapterConfig,
    FeishuConfig,
    IMessageConfig,
    WatchpostConfig,
    resolve_config,
    save_config,
)
from watchpost.daemon import WatchpostDaemon, build_engine_from_config, run_doctor
from watchpost.engine import WatchpostEngine
from watchpost.models import (
    AgentResult,
    AutonomyMode,
    ChannelConfig,
    ChatType,
    EngineConfig,
    NormalizedMessage,
    OwnerAction,
    ThreadRecord,
    ThreadStatus,
    TriggerMode,
)
from watchpost.notifiers import classify_owner_command
from watchpost.notifiers.imessage import IMessageNotifier
from watchpost.store import WatchpostStore


class _RecordingBackend:
    def __init__(self, name: str = "rec", *, fail_start: bool = False, fail_steer: bool = False) -> None:
        self.name = name
        self.fail_start = fail_start
        self.fail_steer = fail_steer
        self.starts: list[tuple[str, str, Path]] = []
        self.steers: list[tuple[str, str, str, Path]] = []

    async def start_thread(
        self,
        title: str,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        channel: ChannelConfig | None = None,
    ) -> tuple[str, AgentResult]:
        del model, channel
        self.starts.append((title, prompt, Path(cwd)))
        if self.fail_start:
            return "", AgentResult(raw_text="primary start boom", error="primary start boom")
        raw = (
            "[问题结论]\n1. 已定位服务重启原因。\n"
            "[是否需要介入]\n否 — 只读排查完成\n"
            "[建议回复]\n请核对配置文件后再试。\n"
            "[待办状态]\n无需改动代码。"
        )
        return f"ref_{len(self.starts)}", AgentResult(
            raw_text=raw,
            summary_bubbles=[f"[{self.name}] 已定位服务重启原因。"],
            suggested_reply="请核对配置文件后再试。",
        )

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel: ChannelConfig | None = None,
    ) -> AgentResult:
        del channel
        self.steers.append((backend_thread_ref, title, message, Path(cwd)))
        if self.fail_steer:
            return AgentResult(raw_text="primary steer boom", error="primary steer boom")
        raw = f"[问题结论]\n1. [{self.name}] 跟进完成。\n[是否需要介入]\n否\n[建议回复]\n收到。\n[待办状态]\n已完成。"
        return AgentResult(raw_text=raw, summary_bubbles=[f"[{self.name}] 跟进完成。"], suggested_reply="收到。")


class _RecordingNotifier:
    name = "imessage"

    def __init__(self) -> None:
        self.starts: list[tuple[str, str]] = []
        self.completions: list[tuple[str, list[str]]] = []
        self.direct_replies: list[str] = []

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
    ) -> list[str]:
        del thread, is_steer
        self.starts.append((channel.id, preview))
        return ["tok-start-1"]

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        self.completions.append((thread.id, list(bubbles)))
        return [f"tok-{thread.id}-{i}" for i in range(len(bubbles))]

    async def poll_owner_actions(self) -> list[OwnerAction]:
        return []

    async def send_message(self, text: str) -> bool:
        self.direct_replies.append(text)
        return True


class _FakeWhisper:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def transcribe_audio(self, audio_path: Path, *, prompt: str = "", language: str = "zh") -> tuple[str, str]:
        del language
        self.calls.append((audio_path, prompt))
        return "帮我查一下四号站日志", ""


def test_antigravity_stop_hook_and_backend_roundtrip(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "watchpost.db"
    store = WatchpostStore(db_path)

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "USER_INPUT", "content": "hi"}),
                json.dumps({"type": "PLANNER_RESPONSE", "content": "Initial draft"}),
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "content": (
                            "[问题结论]\n1. 接口超时。\n"
                            "[是否需要介入]\n否\n"
                            "[建议回复]\n请检查网络。\n"
                            "[待办状态]\n无需改动。"
                        ),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert "接口超时" in extract_latest_assistant_response(transcript)

    conv_id = "11111111-2222-3333-4444-555555555555"
    # Untracked conversationId is ignored cleanly
    assert handle_stop_hook_payload({"conversationId": conv_id, "transcriptPath": str(transcript)}, store=store) is False

    # Registered ref with fullyIdle=False is skipped
    store.register_backend_ref("antigravity", conv_id)
    assert (
        handle_stop_hook_payload(
            {"conversationId": conv_id, "transcriptPath": str(transcript), "fullyIdle": False},
            store=store,
        )
        is False
    )

    # Invoke `watchpost hook-stop` CLI via stdin JSON
    hook_payload = json.dumps({"conversationId": conv_id, "transcriptPath": str(transcript), "fullyIdle": True})
    monkeypatch.setattr(sys, "stdin", io.StringIO(hook_payload))
    assert cli_main(["hook-stop", "--db", str(db_path)]) == 0
    comp = store.get_new_completion("antigravity", conv_id, after_id=0)
    assert comp is not None and "接口超时" in comp["raw_text"]

    # Duplicate digest is deduplicated
    assert handle_stop_hook_payload({"conversationId": conv_id, "transcriptPath": str(transcript)}, store=store) is False

    # Mock `agentapi` script that returns newConversation JSON and spawns a delayed completion update
    mock_agentapi = tmp_path / "agentapi"
    mock_conv = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    steer_transcript = tmp_path / "steer_transcript.jsonl"
    steer_transcript.write_text(
        json.dumps({"type": "PLANNER_RESPONSE", "content": "[问题结论]\n1. AGY 追问回复。\n[是否需要介入]\n否\n[建议回复]\n好的。\n[待办状态]\n完成。"})
        + "\n",
        encoding="utf-8",
    )
    mock_agentapi.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, subprocess, sys
            cmd = sys.argv[1]
            if cmd == "new-conversation":
                print(json.dumps({{"response": {{"newConversation": {{"conversationId": "{mock_conv}"}}}}}}))
                t_path = {str(transcript)!r}
            else:
                t_path = {str(steer_transcript)!r}

            payload = json.dumps({{"conversationId": "{mock_conv}", "transcriptPath": t_path}})
            runner = (
                "import json, sys, time; from pathlib import Path; "
                "from watchpost.store import WatchpostStore; "
                "from watchpost.backends.antigravity import handle_stop_hook_payload; "
                "time.sleep(0.15); "
                "handle_stop_hook_payload(json.loads(sys.argv[1]), store=WatchpostStore(Path(sys.argv[2])))"
            )
            subprocess.Popen(
                [sys.executable, "-c", runner, payload, {str(db_path)!r}],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            """
        ),
        encoding="utf-8",
    )
    mock_agentapi.chmod(mock_agentapi.stat().st_mode | stat.S_IEXEC)

    async def _run() -> None:
        backend = AntigravityBackend(store, agentapi_bin=str(mock_agentapi), poll_interval_s=0.05, timeout_s=10.0)
        ref, res1 = await backend.start_thread("Test", "Check status", cwd=tmp_path)
        assert ref == mock_conv
        assert not res1.error
        assert "接口超时" in res1.raw_text

        res2 = await backend.steer_thread(ref, "Test", "Follow up", cwd=tmp_path)
        assert not res2.error
        assert "AGY 追问回复" in res2.raw_text

    asyncio.run(_run())


def test_fallback_backend_failover_and_recovery(tmp_path: Path) -> None:
    async def _run() -> None:
        # 1. Start failover: primary fails -> backup runs and gets tagged ref
        p1 = _RecordingBackend("antigravity", fail_start=True)
        b1 = _RecordingBackend("codex")
        fb1 = FallbackBackend(p1, b1)
        assert fb1.name == "antigravity+codex"
        ref, res = await fb1.start_thread("Bug", "Investigate", cwd=tmp_path)
        assert ref.startswith("codex:")
        assert not res.error
        assert len(b1.starts) == 1

        # 2. Steer failover: thread started on primary, steer fails -> backup recovery turn
        p2 = _RecordingBackend("antigravity", fail_steer=True)
        b2 = _RecordingBackend("codex")
        fb2 = FallbackBackend(p2, b2)
        pref, pres = await fb2.start_thread("Bug", "Investigate", cwd=tmp_path)
        assert pref.startswith("antigravity:")
        assert not pres.error
        sres = await fb2.steer_thread(pref, "Bug", "Need details", cwd=tmp_path)
        assert not sres.error
        assert len(b2.starts) == 1
        assert "Backup recovery" in b2.starts[0][1]

    asyncio.run(_run())


def test_feishu_lark_cli_and_external_command_adapter(tmp_path: Path) -> None:
    fake_lark = tmp_path / "lark-cli"
    downloaded_dir = tmp_path / "downloads"
    fake_lark.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path
            args = sys.argv[1:]
            sub = args[1] if len(args) > 1 else ""
            if sub == "+chat-list":
                ctype = args[args.index("--types") + 1]
                chat = {{"chat_id": f"oc_{{ctype}}_1", "name": f"Chat {{ctype}}"}}
                print(json.dumps({{"ok": True, "data": {{"chats": [chat]}}}}))
            elif sub == "+chat-messages-list":
                print(json.dumps({{"ok": True, "data": {{"messages": [{{
                    "message_id": "om_lark_1",
                    "message_position": 42,
                    "create_time": "1730000000",
                    "msg_type": "text",
                    "content": '<at id="ou_owner">@Lead</at> 检查网关日志',
                    "sender": {{"id": "ou_colleague", "name": "Dev", "sender_type": "user"}},
                    "mentions": [],
                    "deleted": False,
                }}]}}}}))
            elif sub == "+messages-mget":
                print(json.dumps({{"ok": True, "data": {{"messages": [{{
                    "message_id": "om_lark_0",
                    "message_position": 41,
                    "create_time": "1729999900",
                    "msg_type": "image",
                    "content": '<image key="img_abc123"/>',
                    "sender": {{"id": "ou_colleague", "name": "Dev", "sender_type": "user"}},
                }}]}}}}))
            elif sub == "+messages-resources-download":
                out_name = args[args.index("--output") + 1]
                p = Path.cwd() / out_name
                p.write_bytes(b"PNGDATA")
                print(json.dumps({{"ok": True, "data": {{"saved_path": str(p)}}}}))
            else:
                print(json.dumps({{"ok": False, "error": {{"message": "unknown cmd"}}}}))
                sys.exit(1)
            """
        ),
        encoding="utf-8",
    )
    fake_lark.chmod(fake_lark.stat().st_mode | stat.S_IEXEC)

    fake_ext = tmp_path / "ext_plugin.py"
    fake_ext.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path
            action = sys.argv[1]
            payload = json.loads(sys.stdin.read() or "{{}}")
            if action == "list-chats":
                print(json.dumps([{{"external_chat_id": "room_1", "name": "Ops Room", "chat_type": "group"}}]))
            elif action == "fetch-recent":
                print(json.dumps([{{
                    "message_id": "m_ext_1",
                    "channel_id": payload["channel"]["id"],
                    "position": 100,
                    "create_ms": 1730000000000,
                    "sender_id": "u_ext",
                    "sender_name": "Peer",
                    "clean_text": "check sensor 2",
                    "msg_type": "text",
                }}]))
            elif action == "fetch-by-ids":
                print(json.dumps([]))
            elif action == "download-media":
                dest = Path(payload["dest_dir"])
                dest.mkdir(parents=True, exist_ok=True)
                out = dest / "sample.png"
                out.write_bytes(b"IMG")
                print(json.dumps([{{"kind": "image", "key": "k1", "path": str(out)}}]))
            """
        ),
        encoding="utf-8",
    )
    fake_ext.chmod(fake_ext.stat().st_mode | stat.S_IEXEC)

    async def _run() -> None:
        feishu = FeishuAdapter(
            FeishuConfig(enabled=True, auth_mode="lark_cli", lark_bin=str(fake_lark), owner_open_id="ou_owner")
        )
        chats = await feishu.list_available_chats()
        assert {c["external_chat_id"] for c in chats} == {"oc_p2p_1", "oc_group_1"}

        ch = ChannelConfig(id="ch_lark", adapter="feishu", external_chat_id="oc_group_1", name="LarkGrp")
        msgs = await feishu.fetch_recent(ch)
        assert len(msgs) == 1
        assert msgs[0].mentions_owner is True
        assert msgs[0].position == 42
        assert "@Lead" in msgs[0].clean_text

        by_id = await feishu.fetch_by_ids(ch, ["om_lark_0"])
        assert len(by_id) == 1 and by_id[0].media_refs[0].key == "img_abc123"
        arts = await feishu.download_media(ch, by_id[0], downloaded_dir)
        assert len(arts) == 1 and Path(arts[0].path).read_bytes() == b"PNGDATA"

        ext = ExternalCommandAdapter(ExternalAdapterConfig(name="custom_chat", command=[str(fake_ext)]))
        ext_chats = await ext.list_available_chats()
        assert ext_chats[0]["external_chat_id"] == "room_1"
        ext_ch = ChannelConfig(id="ch_ext", adapter="custom_chat", external_chat_id="room_1", name="Room")
        ext_msgs = await ext.fetch_recent(ext_ch)
        assert len(ext_msgs) == 1 and ext_msgs[0].message_id == "m_ext_1"
        ext_arts = await ext.download_media(ext_ch, ext_msgs[0], downloaded_dir)
        assert len(ext_arts) == 1 and Path(ext_arts[0].path).read_bytes() == b"IMG"

    asyncio.run(_run())


def test_imessage_allowed_handles_filters_and_attachments(tmp_path: Path) -> None:
    db_path = tmp_path / "chat.db"
    audio_file = tmp_path / "voice_note.caf"
    audio_file.write_bytes(b"CAF")
    img_file = tmp_path / "photo.png"
    img_file.write_bytes(b"PNG")
    sticker_file = tmp_path / "sticker.heic"
    sticker_file.write_bytes(b"HEIC")

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            filename TEXT,
            mime_type TEXT,
            uti TEXT,
            transfer_name TEXT,
            is_sticker INTEGER DEFAULT 0
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            text TEXT,
            attributedBody BLOB,
            is_from_me INTEGER DEFAULT 0,
            handle_id INTEGER,
            thread_originator_guid TEXT DEFAULT '',
            reply_to_guid TEXT DEFAULT '',
            associated_message_type INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0,
            date_retracted INTEGER DEFAULT 0,
            is_audio_message INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);

        INSERT INTO handle VALUES (1, '15550100222'), (2, '15559990000');
        INSERT INTO chat VALUES (1, '15550100222'), (2, '15559990000');
        INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (1, 'g0', 'seed', 0, 1);
        INSERT INTO chat_message_join VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    async def _run() -> None:
        ntf = IMessageNotifier(
            IMessageConfig(
                enabled=True,
                owner_handle="+1 555-010-0001",
                allowed_handles=["+86 155-5010-0222", "15550100222"],
                chat_db_path=str(db_path),
            ),
            send_fn=lambda _h, _t: True,
        )
        assert await ntf.poll_owner_actions() == []

        c = sqlite3.connect(db_path)
        c.execute("INSERT INTO attachment VALUES (1, ?, 'audio/x-caf', 'com.apple.coreaudio-format', 'voice.caf', 0)", (str(audio_file),))
        c.execute("INSERT INTO attachment VALUES (2, ?, 'image/png', 'public.png', 'photo.png', 0)", (str(img_file),))
        c.execute("INSERT INTO attachment VALUES (3, ?, 'image/heic', 'public.heic', 'sticker.heic', 1)", (str(sticker_file),))
        # Row 2: reaction -> skipped
        c.execute("INSERT INTO message (ROWID, guid, text, handle_id, associated_message_type) VALUES (2, 'g2', 'Loved \"hi\"', 1, 2000)")
        c.execute("INSERT INTO chat_message_join VALUES (1, 2)")
        # Row 3: retracted -> skipped
        c.execute("INSERT INTO message (ROWID, guid, text, handle_id, date_retracted) VALUES (3, 'g3', 'oops', 1, 999)")
        c.execute("INSERT INTO chat_message_join VALUES (1, 3)")
        # Row 4: audio-only message from allowed_handles with \ufffc char
        c.execute("INSERT INTO message (ROWID, guid, text, handle_id, is_audio_message) VALUES (4, 'g4', '\ufffc', 1, 1)")
        c.execute("INSERT INTO chat_message_join VALUES (1, 4)")
        c.execute("INSERT INTO message_attachment_join VALUES (4, 1)")
        # Row 5: text + image + sticker (sticker filtered out)
        c.execute("INSERT INTO message (ROWID, guid, text, handle_id, reply_to_guid) VALUES (5, 'g5', '\ufffc #1 检查图片', 1, '')")
        c.execute("INSERT INTO chat_message_join VALUES (1, 5)")
        c.execute("INSERT INTO message_attachment_join VALUES (5, 2), (5, 3)")
        c.commit()
        c.close()

        actions = await ntf.poll_owner_actions()
        assert len(actions) == 2
        assert actions[0].kind == "steer" and actions[0].text == ""
        assert len(actions[0].attachments) == 1 and actions[0].attachments[0]["is_audio"] == "true"

        assert actions[1].kind == "steer" and actions[1].list_index == 1 and actions[1].text == "检查图片"
        assert len(actions[1].attachments) == 1 and actions[1].attachments[0]["name"] == "photo.png"

    asyncio.run(_run())


def test_command_classifier_and_engine_v03_parity(tmp_path: Path) -> None:
    assert classify_owner_command("/help", notifier="imessage").kind == "help"  # type: ignore[union-attr]
    assert classify_owner_command("帮助", notifier="imessage").kind == "help"  # type: ignore[union-attr]
    assert classify_owner_command("/reset", notifier="imessage").kind == "reset_personal"  # type: ignore[union-attr]
    np = classify_owner_command("/new check logs", notifier="imessage")
    assert np is not None and np.kind == "new_personal" and np.text == "check logs"
    idx_go = classify_owner_command("#2 go", notifier="imessage")
    assert idx_go is not None and idx_go.kind == "approve_go" and idx_go.list_index == 2
    assert classify_owner_command("skip", notifier="imessage").kind == "dismiss_go"  # type: ignore[union-attr]
    assert classify_owner_command("忽略", notifier="imessage").kind == "dismiss_go"  # type: ignore[union-attr]
    idx_dismiss = classify_owner_command("#2 dismiss", notifier="imessage")
    assert idx_dismiss is not None and idx_dismiss.kind == "dismiss_go" and idx_dismiss.list_index == 2

    persona_path = tmp_path / "ops_persona.md"
    persona_path.write_text("Monitor critical gateway alarms calmly.", encoding="utf-8")
    ch_ws = tmp_path / "ch_repo"
    personal_ws = tmp_path / "personal_repo"
    ch_ws.mkdir()
    personal_ws.mkdir()

    raw_cfg = WatchpostConfig(
        workspace_dir=str(tmp_path),
        channels=[
            ChannelConfig(
                id="ch_ops",
                adapter="custom_chat",
                external_chat_id="room_ops",
                name="Ops",
                chat_type=ChatType.p2p,
                trigger_mode=TriggerMode.all,
                autonomy_mode=AutonomyMode.diagnose_only,
                persona_file=str(persona_path),
                workspace_dir=str(ch_ws),
            )
        ],
    )
    resolved = resolve_config(raw_cfg)
    assert "Monitor critical gateway alarms calmly." in resolved.channels[0].persona_text

    async def _run() -> None:
        store = WatchpostStore(tmp_path / "eng.db")
        backend = _RecordingBackend("antigravity")
        notifier = _RecordingNotifier()
        whisper = _FakeWhisper()
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(
                workspace_dir=tmp_path,
                language="zh",
                personal_workspace_dir=personal_ws,
                personal_session_ttl_s=3600,
                ack_on_receive=True,
            ),
            notifiers=[notifier],
            media_processor=whisper,
        )
        store.upsert_channel(resolved.channels[0])

        # Trigger channel message -> uses channel.workspace_dir as cwd
        created = await engine.ingest_messages(
            "ch_ops",
            [
                NormalizedMessage(
                    message_id="msg_1",
                    channel_id="ch_ops",
                    position=1,
                    create_ms=int(time.time() * 1000),
                    sender_id="u1",
                    sender_name="Peer",
                    clean_text="站控进程退出了",
                )
            ],
        )
        assert len(created) == 1
        assert backend.starts[0][2] == ch_ws.resolve() or backend.starts[0][2] == ch_ws

        # /help, status, list -> #1 follow-up
        await engine.handle_owner_action(classify_owner_command("/help", notifier="imessage"))  # type: ignore[arg-type]
        assert any("指令说明" in txt for txt in notifier.direct_replies)

        await engine.handle_owner_action(classify_owner_command("status", notifier="imessage"))  # type: ignore[arg-type]
        assert any("Watchpost 运行中" in txt for txt in notifier.direct_replies)

        await engine.handle_owner_action(classify_owner_command("list", notifier="imessage"))  # type: ignore[arg-type]
        assert store.get_kv("last_listed_thread_ids") == [created[0].id]

        steered = await engine.handle_owner_action(
            classify_owner_command("#1 再看一下配置", notifier="imessage")  # type: ignore[arg-type]
        )
        assert steered is not None and steered.id == created[0].id
        assert backend.steers[-1][3] == ch_ws

        # Audio attachment unquoted steer -> routes to Personal Direct Chat + Whisper transcribes audio
        audio_path = tmp_path / "cmd.caf"
        audio_path.write_bytes(b"AUDIO")
        p1 = await engine.handle_owner_action(
            classify_owner_command(
                "",
                notifier="imessage",
                attachments=[{"path": str(audio_path), "name": "cmd.caf", "is_audio": "true"}],
            )  # type: ignore[arg-type]
        )
        assert p1 is not None and p1.channel_id == "personal_direct"
        assert len(whisper.calls) == 1
        assert "四号站日志" in backend.starts[-1][1]
        assert backend.starts[-1][2] == personal_ws

        # Follow-up unquoted message within TTL steers the same personal thread
        p2 = await engine.handle_owner_action(classify_owner_command("顺便看下磁盘空间", notifier="imessage"))  # type: ignore[arg-type]
        assert p2 is not None and p2.id == p1.id

        # personal_direct must not inflate channel metrics or fail poll_all_channels
        assert store.get_metrics()["channels"] == 1
        assert await engine.poll_all_channels() == 0

        # /reset then unquoted message starts a brand-new personal thread
        await engine.handle_owner_action(classify_owner_command("/reset", notifier="imessage"))  # type: ignore[arg-type]
        p3 = await engine.handle_owner_action(classify_owner_command("新的问题", notifier="imessage"))  # type: ignore[arg-type]
        assert p3 is not None and p3.id != p1.id

        # /new <body?> starts a brand-new personal thread immediately
        p4 = await engine.handle_owner_action(classify_owner_command("/new 检查分支", notifier="imessage"))  # type: ignore[arg-type]
        assert p4 is not None and p4.id != p3.id

        # Dismissing pending_go via owner command ("忽略") clears flag without invoking backend
        store.create_or_update_thread(
            created[0].model_copy(update={"pending_go": True, "status": ThreadStatus.awaiting_go})
        )
        assert store.get_metrics()["awaiting_go"] == 1
        steers_before = len(backend.steers)
        starts_before = len(backend.starts)
        dismissed = await engine.handle_owner_action(
            OwnerAction(notifier="imessage", kind="dismiss_go", text="忽略")
        )
        assert dismissed is not None and dismissed.id == created[0].id
        assert dismissed.pending_go is False
        assert dismissed.status == ThreadStatus.completed
        assert store.get_metrics()["awaiting_go"] == 0
        assert len(backend.steers) == steers_before
        assert len(backend.starts) == starts_before
        assert any("已关闭待确认（go）" in txt for txt in notifier.direct_replies)

    asyncio.run(_run())


def test_build_engine_from_config_fallback_and_doctor(tmp_path: Path) -> None:
    ext_script = tmp_path / "mock_plugin.py"
    ext_script.write_text("#!/usr/bin/env python3\nimport sys; print('[]')\n", encoding="utf-8")
    ext_script.chmod(ext_script.stat().st_mode | stat.S_IEXEC)

    cfg_path = tmp_path / "config.json"
    cfg = WatchpostConfig(
        db_path=str(tmp_path / "wp.db"),
        media_dir=str(tmp_path / "media"),
        workspace_dir=str(tmp_path),
        backend=BackendConfig(
            preset="codex",
            backup_preset="claude_code",
            codex_sandbox="read-only",
        ),
        feishu=FeishuConfig(enabled=True, auth_mode="lark_cli", lark_bin=sys.executable),
        imessage=IMessageConfig(
            enabled=True,
            owner_handle="+15550100",
            personal_workspace_dir=str(tmp_path),
            dry_run=True,
        ),
        external_adapters=[ExternalAdapterConfig(name="wechat", command=[str(ext_script)])],
        channels=[
            ChannelConfig(
                id="wx_1",
                adapter="wechat",
                external_chat_id="peer_1",
                name="Peer 1",
                chat_type=ChatType.p2p,
                trigger_mode=TriggerMode.all,
            )
        ],
    )
    save_config(cfg, cfg_path)
    resolved = resolve_config(cfg)
    engine = build_engine_from_config(resolved)
    assert isinstance(engine.backend, FallbackBackend)
    assert engine.backend.name == "codex+claude_code"
    assert isinstance(engine.backend.primary, CLIAgentBackend)
    assert engine.backend.primary._sandbox_mode == "read-only"
    assert "wechat" in engine.adapters and "feishu" in engine.adapters
    assert engine.config.personal_workspace_dir == tmp_path

    # Check run_doctor with mock backend so PATH availability doesn't fail on CI
    doc_cfg_path = tmp_path / "doc_config.json"
    save_config(
        cfg.model_copy(update={"backend": BackendConfig(preset="mock", backup_preset="")}),
        doc_cfg_path,
    )
    report = run_doctor(doc_cfg_path)
    assert report["ok"] is True
    assert report["checks"]["adapter:wechat"]["ok"] is True
    assert report["checks"]["feishu"]["ok"] is True


def test_imessage_photo_forwarding_quotes_and_echo_suppression(tmp_path: Path, monkeypatch: Any) -> None:
    from watchpost.bubbles import format_completion_bubbles
    from watchpost.models import MediaArtifact, MediaRef

    # 1. Lark-cli content strips raw `[Image: img_v3_...]` keys cleanly
    feishu = FeishuAdapter(FeishuConfig(enabled=True, auth_mode="lark_cli", owner_open_id="ou_owner"))
    ch = ChannelConfig(
        id="feishu_zhang",
        adapter="feishu",
        external_chat_id="oc_1",
        name="Peer X",
        chat_type=ChatType.p2p,
        trigger_mode=TriggerMode.all,
    )
    norm_img = feishu._normalize_item(
        ch,
        {
            "message_id": "om_img_1",
            "message_position": 10,
            "create_time": str(int(time.time())),
            "msg_type": "image",
            "content": "[Image: img_v3_0217m_218779d2]",
            "sender": {"id": "ou_peer", "name": "Peer X", "sender_type": "user"},
            "deleted": False,
        },
    )
    assert norm_img.clean_text == "[Image]"
    assert norm_img.media_refs and norm_img.media_refs[0].key == "img_v3_0217m_218779d2"

    # 2. Completion bubble header avoids `Name:` confusion & strips redundant colon tag only
    b1, _ = format_completion_bubbles(
        "飞书 · Peer X",
        "总结：\nPeer X: 现场换电正常进行中。\n\n需要注意：\n无",
        language="zh",
    )
    assert b1[0] == "【飞书 · Peer X】\n现场换电正常进行中。"
    b2, _ = format_completion_bubbles(
        "飞书 · Peer X",
        "总结：\nPeer X说先观察一下。\n\n需要注意：\n无",
        language="zh",
    )
    assert b2[0] == "【飞书 · Peer X】\nPeer X说先观察一下。"

    # 3. IMessageNotifier sends separate header + quoted original + real photo, suppressing wp_out_ echo
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    photo_path = tmp_path / "sample.png"
    photo_path.write_bytes(b"PNGDATA")

    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT, uti TEXT, transfer_name TEXT, is_sticker INTEGER DEFAULT 0);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB, is_from_me INTEGER DEFAULT 0, handle_id INTEGER, thread_originator_guid TEXT DEFAULT '', reply_to_guid TEXT DEFAULT '', associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0, is_audio_message INTEGER DEFAULT 0);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+15550100');
        INSERT INTO chat VALUES (1, '+15550100');
        INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (1, 'g0', 'seed', 0, 1);
        INSERT INTO chat_message_join VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    sent_texts: list[str] = []
    sent_files: list[str] = []

    def fake_send_txt(_h: str, body: str) -> bool:
        sent_texts.append(body)
        c = sqlite3.connect(db_path)
        rid = 10 + len(sent_texts) + len(sent_files)
        c.execute("INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (?, ?, ?, 1, 1)", (rid, f"g-txt-{rid}", body))
        c.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rid,))
        c.commit()
        c.close()
        return True

    def fake_send_file(_h: str, fpath: str) -> bool:
        sent_files.append(fpath)
        c = sqlite3.connect(db_path)
        rid = 10 + len(sent_texts) + len(sent_files)
        c.execute("INSERT INTO attachment (ROWID, filename, mime_type, uti, transfer_name, is_sticker) VALUES (?, ?, 'image/png', 'public.png', ?, 0)", (rid, fpath, Path(fpath).name))
        c.execute("INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (?, ?, '', 1, 1)", (rid, f"g-file-{rid}"))
        c.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rid,))
        c.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (rid, rid))
        c.commit()
        c.close()
        return True

    async def _run() -> None:
        ntf = IMessageNotifier(
            IMessageConfig(enabled=True, owner_handle="+15550100", chat_db_path=str(db_path)),
            send_fn=fake_send_txt,
            send_file_fn=fake_send_file,
        )
        assert await ntf.poll_owner_actions() == []

        class _FakeAdapter:
            name = "feishu"

            async def list_available_chats(self) -> list[dict[str, str]]:
                return []

            async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
                del channel, limit
                return []

            async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
                del channel, message_ids
                return []

            async def download_media(self, channel: ChannelConfig, message: NormalizedMessage, dest_dir: Path) -> list[MediaArtifact]:
                del channel, dest_dir
                return [MediaArtifact(kind="image", key=message.media_refs[0].key, path=str(photo_path))]

        store = WatchpostStore(tmp_path / "wp.db")
        backend = _RecordingBackend("antigravity")
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(workspace_dir=tmp_path, language="zh"),
            adapters={"feishu": _FakeAdapter()},
            notifiers=[ntf],
        )
        store.upsert_channel(ch)

        now_ms = int(time.time() * 1000)
        created = await engine.ingest_messages(
            ch.id,
            [
                NormalizedMessage(
                    message_id="m_img",
                    channel_id=ch.id,
                    position=1,
                    create_ms=now_ms,
                    sender_id="ou_peer",
                    sender_name="Peer X",
                    msg_type="image",
                    clean_text="[Image]",
                    media_refs=[MediaRef(kind="image", key="img_v3_xyz")],
                ),
                NormalizedMessage(
                    message_id="m_txt",
                    channel_id=ch.id,
                    position=2,
                    create_ms=now_ms + 100,
                    sender_id="ou_peer",
                    sender_name="Peer X",
                    msg_type="text",
                    clean_text="这个告警影响吗，现场正常在换电",
                ),
            ],
        )
        assert len(created) == 1
        assert created[0].title == "这个告警影响吗，现场正常在换电"
        assert any(t == "【飞书 · Peer X】发来新消息，正在查看" for t in sent_texts)
        assert any(t == '"这个告警影响吗，现场正常在换电"' for t in sent_texts)
        assert any(t.startswith("【飞书 · Peer X】\n") for t in sent_texts)
        assert len(sent_files) == 1 and "watchpost_outbox/wp_out_" in sent_files[0]

        # Outbound wp_out_ photo and outbound text must NOT re-trigger poll_owner_actions
        assert await ntf.poll_owner_actions() == []

        # Real photo sent by owner (NOT wp_out_) still triggers owner action
        owner_photo = tmp_path / "owner_shot.png"
        owner_photo.write_bytes(b"OWNPNG")
        c = sqlite3.connect(db_path)
        c.execute("INSERT INTO attachment (ROWID, filename, mime_type, uti, transfer_name, is_sticker) VALUES (99, ?, 'image/png', 'public.png', 'owner_shot.png', 0)", (str(owner_photo),))
        c.execute("INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (99, 'g-own-99', '', 1, 1)")
        c.execute("INSERT INTO chat_message_join VALUES (1, 99)")
        c.execute("INSERT INTO message_attachment_join VALUES (99, 99)")
        c.commit()
        c.close()

        owner_acts = await ntf.poll_owner_actions()
        assert len(owner_acts) == 1 and owner_acts[0].attachments[0]["name"] == "owner_shot.png"

    asyncio.run(_run())


def test_thread_persists_active_status_while_backend_runs(tmp_path: Path) -> None:
    store = WatchpostStore(tmp_path / "active_state.db")
    ch = ChannelConfig(
        id="ch_live",
        adapter="feishu",
        external_chat_id="oc_live",
        name="Peer Live",
        chat_type=ChatType.p2p,
        trigger_mode=TriggerMode.all,
        autonomy_mode=AutonomyMode.diagnose_only,
    )
    store.upsert_channel(ch)
    observed_mid_statuses: list[ThreadStatus] = []

    class _ObservingBackend(_RecordingBackend):
        async def start_thread(
            self,
            title: str,
            prompt: str,
            *,
            cwd: Path,
            model: str = "",
            channel: ChannelConfig | None = None,
        ) -> tuple[str, AgentResult]:
            for th in store.list_threads(limit=10):
                observed_mid_statuses.append(th.status)
            return await super().start_thread(title, prompt, cwd=cwd, model=model, channel=channel)

    async def _run() -> None:
        engine = WatchpostEngine(
            store,
            _ObservingBackend("obs"),
            config=EngineConfig(workspace_dir=tmp_path, language="zh"),
        )
        created = await engine.ingest_messages(
            "ch_live",
            [
                NormalizedMessage(
                    message_id="m_1",
                    channel_id="ch_live",
                    position=1,
                    create_ms=int(time.time() * 1000),
                    sender_id="ou_peer",
                    sender_name="Peer Live",
                    clean_text="帮忙看下站控状态",
                )
            ],
        )
        assert observed_mid_statuses == [ThreadStatus.active]
        assert len(created) == 1
        final = store.get_thread(created[0].id)
        assert final is not None and final.status == ThreadStatus.completed

    asyncio.run(_run())


def test_daemon_recovers_stale_active_threads_on_startup(tmp_path: Path) -> None:
    db_path = tmp_path / "recover.db"
    store = WatchpostStore(db_path)
    ch = ChannelConfig(
        id="ch_rec",
        adapter="feishu",
        external_chat_id="oc_rec",
        name="Peer Recover",
        chat_type=ChatType.p2p,
        trigger_mode=TriggerMode.all,
        autonomy_mode=AutonomyMode.diagnose_only,
    )
    store.upsert_channel(ch)

    old_ts = time.time() - 7200
    store.create_or_update_thread(
        ThreadRecord(
            id="th_orphan",
            channel_id=ch.id,
            backend="antigravity",
            backend_thread_ref="conv_orphan",
            sender_id="ou_1",
            sender_name="Peer Recover",
            title="Orphan active turn",
            status=ThreadStatus.active,
            pending_go=False,
            created_at=old_ts - 60,
            updated_at=old_ts,
        )
    )
    store.create_or_update_thread(
        ThreadRecord(
            id="th_done",
            channel_id=ch.id,
            backend="antigravity",
            backend_thread_ref="conv_done",
            sender_id="ou_1",
            sender_name="Peer Recover",
            title="Completed turn",
            status=ThreadStatus.completed,
            pending_go=False,
            created_at=old_ts - 120,
            updated_at=old_ts - 30,
        )
    )

    # Opening another WatchpostStore instance must NOT alter an active thread while a live daemon may own it
    concurrent_store = WatchpostStore(db_path)
    still_active = concurrent_store.get_thread("th_orphan")
    assert still_active is not None and still_active.status == ThreadStatus.active

    async def _run() -> None:
        cfg = WatchpostConfig(
            db_path=str(db_path),
            media_dir=str(tmp_path / "media"),
            workspace_dir=str(tmp_path),
            api_port=0,
        )
        engine = WatchpostEngine(store, _RecordingBackend("obs"), config=EngineConfig(workspace_dir=tmp_path))
        daemon = WatchpostDaemon(engine, cfg)
        stop_ev = asyncio.Event()
        stop_ev.set()
        await daemon.run_forever(stop_event=stop_ev, seed_on_start=False)

    asyncio.run(_run())

    orphan_after = store.get_thread("th_orphan")
    done_after = store.get_thread("th_done")
    assert orphan_after is not None
    assert orphan_after.status == ThreadStatus.error
    assert orphan_after.pending_go is False
    assert abs(orphan_after.updated_at - old_ts) < 0.001
    events = store.list_thread_events("th_orphan")
    assert any(ev.kind == "error" and "Interrupted" in ev.raw_text for ev in events)
    assert done_after is not None and done_after.status == ThreadStatus.completed


def test_summarize_batch_strips_markdown_image_keys() -> None:
    pure_preview, pure_imgs, pure_topic = WatchpostEngine._summarize_batch_for_notice(
        [
            NormalizedMessage(
                message_id="m_img_only",
                channel_id="ch_1",
                position=1,
                create_ms=1000,
                sender_id="ou_1",
                sender_name="Peer",
                msg_type="post",
                clean_text="![Image](img_v3_0217n_66540c18-cd99-4a00-8000-abcdef123456)",
            )
        ]
    )
    assert pure_topic == "[图片]"
    assert pure_preview == ""
    assert pure_imgs == []

    mixed_preview, mixed_imgs, mixed_topic = WatchpostEngine._summarize_batch_for_notice(
        [
            NormalizedMessage(
                message_id="m_img_mixed",
                channel_id="ch_1",
                position=2,
                create_ms=2000,
                sender_id="ou_1",
                sender_name="Peer",
                msg_type="post",
                clean_text="![Image](img_v3_0217n_66540c18-cd99-4a00-8000-abcdef123456) 站控报暂停",
            )
        ]
    )
    assert mixed_topic == "站控报暂停"
    assert "img_v3_" not in mixed_topic
    assert "img_v3_" not in mixed_preview
    assert mixed_imgs == []


def test_personal_direct_swipe_reply_stays_natural_without_bot_headers(tmp_path: Path) -> None:
    class _LabeledBackend(_RecordingBackend):
        async def start_thread(
            self,
            title: str,
            prompt: str,
            *,
            cwd: Path,
            model: str = "",
            channel: ChannelConfig | None = None,
        ) -> tuple[str, AgentResult]:
            del model, channel
            self.starts.append((title, prompt, Path(cwd)))
            return "conv_p_1", CLIAgentBackend._parse_output("总结：有权限，可以直接改。\n\n需要注意：无")

        async def steer_thread(
            self,
            backend_thread_ref: str,
            title: str,
            message: str,
            *,
            cwd: Path,
            channel: ChannelConfig | None = None,
        ) -> AgentResult:
            del channel
            self.steers.append((backend_thread_ref, title, message, Path(cwd)))
            return CLIAgentBackend._parse_output("总结：有权限，可以直接改。\n\n需要注意：无")

    async def _run() -> None:
        store = WatchpostStore(tmp_path / "personal_natural.db")
        backend = _LabeledBackend("ag")
        notifier = _RecordingNotifier()
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(
                workspace_dir=tmp_path,
                personal_workspace_dir=tmp_path,
                language="zh",
            ),
            notifiers=[notifier],
        )

        p1 = await engine.handle_owner_action(classify_owner_command("现在能改代码吗", notifier="imessage"))  # type: ignore[arg-type]
        assert p1 is not None and p1.channel_id == "personal_direct"
        tok = f"tok-{p1.id}-0"

        p2 = await engine.handle_owner_action(
            classify_owner_command("具体哪个目录？", notifier="imessage", reply_token=tok)  # type: ignore[arg-type]
        )
        assert p2 is not None and p2.id == p1.id

        assert len(backend.steers) == 1
        steer_prompt = backend.steers[0][2]
        assert "手机直聊跟进" in steer_prompt
        assert "使用纯文本标题（总结：、建议回复：、需要注意：）" not in steer_prompt

        assert len(notifier.completions) == 2
        for _tid, bubbles in notifier.completions:
            joined = "\n".join(bubbles)
            assert "有权限，可以直接改。" in joined
            assert "【个人直聊】" not in joined
            assert "总结：" not in joined
            assert "需要注意：" not in joined

    asyncio.run(_run())


def test_imessage_plain_message_ignores_reply_to_guid_and_routes_to_personal(tmp_path: Path) -> None:
    chat_db = tmp_path / "chat.db"
    conn = sqlite3.connect(chat_db)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            text TEXT,
            is_from_me INTEGER DEFAULT 0,
            handle_id INTEGER,
            thread_originator_guid TEXT DEFAULT '',
            reply_to_guid TEXT DEFAULT '',
            associated_message_type INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0,
            date_retracted INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        INSERT INTO handle VALUES (1, '+15550100');
        INSERT INTO chat VALUES (1, '+15550100');
        INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (1, 'guid-seed', 'seed', 1, 1);
        INSERT INTO chat_message_join VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    async def _run() -> None:
        ntf = IMessageNotifier(
            IMessageConfig(enabled=True, owner_handle="+15550100", chat_db_path=str(chat_db)),
            send_fn=lambda _h, _t: True,
        )
        assert await ntf.poll_owner_actions() == []

        c = sqlite3.connect(chat_db)
        c.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, handle_id, thread_originator_guid, reply_to_guid) "
            "VALUES (2, 'guid-plain', '查一下本地状态', 0, 1, '', 'guid-work-alert')"
        )
        c.execute(
            "INSERT INTO chat_message_join VALUES (1, 2)"
        )
        c.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, handle_id, thread_originator_guid, reply_to_guid) "
            "VALUES (3, 'guid-swipe', '再看下这个告警', 0, 1, 'guid-work-alert', 'guid-prev')"
        )
        c.execute(
            "INSERT INTO chat_message_join VALUES (1, 3)"
        )
        c.commit()
        c.close()

        actions = await ntf.poll_owner_actions()
        assert len(actions) == 2
        assert actions[0].text == "查一下本地状态"
        assert actions[0].reply_token == ""
        assert actions[1].text == "再看下这个告警"
        assert actions[1].reply_token == "guid-work-alert"

        work_ws = tmp_path / "work_ws"
        personal_ws = tmp_path / "personal_ws"
        work_ws.mkdir()
        personal_ws.mkdir()

        store = WatchpostStore(tmp_path / "wp.db")
        ch_work = ChannelConfig(
            id="ch_work",
            adapter="feishu",
            external_chat_id="oc_work",
            name="Peer Work",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
            workspace_dir=str(work_ws),
        )
        store.upsert_channel(ch_work)
        store.create_or_update_thread(
            ThreadRecord(
                id="th_work",
                channel_id="ch_work",
                backend="rec",
                backend_thread_ref="conv_work_1",
                sender_id="ou_work",
                sender_name="Peer Work",
                title="Work alert",
                status=ThreadStatus.completed,
            )
        )
        store.bind_outbound("imessage", "guid-work-alert", "th_work")

        backend = _RecordingBackend("rec")
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(
                workspace_dir=work_ws,
                personal_workspace_dir=personal_ws,
                language="zh",
            ),
            notifiers=[ntf],
        )

        plain_res = await engine.handle_owner_action(actions[0])
        assert plain_res is not None
        assert plain_res.channel_id == "personal_direct"
        assert plain_res.id != "th_work"
        assert backend.starts[-1][2] == personal_ws

        swipe_res = await engine.handle_owner_action(actions[1])
        assert swipe_res is not None
        assert swipe_res.id == "th_work"
        assert backend.steers[-1][3] == work_ws

    asyncio.run(_run())


def test_quiet_hours_briefing_urgent_and_mute_commands(tmp_path: Path, monkeypatch: Any) -> None:
    from datetime import datetime
    import watchpost.engine as engine_mod
    from watchpost.models import QuietHoursConfig

    ts_2330 = datetime(2026, 11, 23, 23, 30, 0).timestamp()
    ts_0200 = datetime(2026, 11, 24, 2, 0, 0).timestamp()
    ts_0805 = datetime(2026, 11, 24, 8, 5, 0).timestamp()
    clock = {"now": ts_2330}
    monkeypatch.setattr(engine_mod.time, "time", lambda: clock["now"])

    async def _run() -> None:
        store = WatchpostStore(tmp_path / "quiet.db")
        ch = ChannelConfig(
            id="ch_ops",
            adapter="feishu",
            external_chat_id="oc_ops",
            name="Field Support",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
        )
        store.upsert_channel(ch)

        backend = _RecordingBackend("ag")
        notifier = _RecordingNotifier()
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(
                workspace_dir=tmp_path,
                personal_workspace_dir=tmp_path,
                language="zh",
                quiet_hours=QuietHoursConfig(
                    enabled=True,
                    start="23:00",
                    end="08:00",
                    urgent_keywords=["URGENT", "停机"],
                    auto_brief_on_end=True,
                ),
            ),
            notifiers=[notifier],
        )

        # 1. During 23:00–08:00, normal inbound runs silently and queues into morning briefing
        th_quiet = await engine.ingest_messages(
            ch.id,
            [
                NormalizedMessage(
                    message_id="m_q1",
                    channel_id=ch.id,
                    position=1,
                    create_ms=int(ts_2330 * 1000),
                    sender_id="ou_1",
                    sender_name="Engineer",
                    clean_text="帮我查一下夜间报文日志",
                )
            ],
        )
        assert len(th_quiet) == 1
        assert len(notifier.starts) == 0
        assert len(notifier.completions) == 0
        assert engine.get_quiet_status(now_ts=ts_2330)["queued_count"] == 1

        # 2. Urgent keyword breaks through during 02:00
        clock["now"] = ts_0200
        await engine.ingest_messages(
            ch.id,
            [
                NormalizedMessage(
                    message_id="m_urg",
                    channel_id=ch.id,
                    position=2,
                    create_ms=int(ts_0200 * 1000),
                    sender_id="ou_1",
                    sender_name="Engineer",
                    clean_text="URGENT 现场服务异常退出",
                )
            ],
        )
        assert len(notifier.starts) == 1
        assert len(notifier.completions) == 1

        # 3. Personal Direct Chat still notifies immediately during Quiet Hours
        p_res = await engine.handle_owner_action(classify_owner_command("查一下分支", notifier="imessage"))  # type: ignore[arg-type]
        assert p_res is not None and p_res.channel_id == "personal_direct"
        assert len(notifier.completions) == 2

        # 4. At 08:05, morning briefing flushes once and clears queue (no duplicate on second call)
        clock["now"] = ts_0805
        flushed = await engine.flush_quiet_digest_if_due(now_ts=ts_0805)
        assert flushed == 1
        assert engine.get_quiet_status(now_ts=ts_0805)["queued_count"] == 0
        assert any("早安简报" in txt and "Field Support" in txt for txt in notifier.direct_replies)
        assert await engine.flush_quiet_digest_if_due(now_ts=ts_0805) == 0

        # 5. Daytime /mute 60m -> quiet active -> silent -> /brief flushes -> /unmute resumes
        await engine.handle_owner_action(classify_owner_command("/mute 60m", notifier="imessage"))  # type: ignore[arg-type]
        assert engine.get_quiet_status(now_ts=ts_0805)["reason"] == "muted"
        comp_before = len(notifier.completions)
        await engine.ingest_messages(
            ch.id,
            [
                NormalizedMessage(
                    message_id="m_m1",
                    channel_id=ch.id,
                    position=3,
                    create_ms=int(ts_0805 * 1000),
                    sender_id="ou_1",
                    sender_name="Engineer",
                    clean_text="顺便看看温度传感器",
                )
            ],
        )
        assert len(notifier.completions) == comp_before
        assert engine.get_quiet_status(now_ts=ts_0805)["queued_count"] == 1

        await engine.handle_owner_action(classify_owner_command("早报", notifier="imessage"))  # type: ignore[arg-type]
        assert engine.get_quiet_status(now_ts=ts_0805)["queued_count"] == 0

        await engine.handle_owner_action(classify_owner_command("/unmute", notifier="imessage"))  # type: ignore[arg-type]
        assert engine.get_quiet_status(now_ts=ts_0805)["active"] is False

        # 6. /unmute inside scheduled Quiet Hours overrides schedule until next 08:00
        clock["now"] = ts_2330
        assert engine.get_quiet_status(now_ts=ts_2330)["active"] is True
        await engine.handle_owner_action(classify_owner_command("/unmute", notifier="imessage"))  # type: ignore[arg-type]
        assert engine.get_quiet_status(now_ts=ts_2330)["active"] is False

    asyncio.run(_run())


def test_cross_channel_same_incident_memory_and_isolation(tmp_path: Path) -> None:
    ws_1 = tmp_path / "ws_one"
    ws_2 = tmp_path / "ws_two"
    ws_1.mkdir()
    ws_2.mkdir()

    async def _run() -> None:
        store = WatchpostStore(tmp_path / "cross.db")
        ch_a = ChannelConfig(
            id="ch_a",
            adapter="feishu",
            external_chat_id="oc_a",
            name="Peer Alpha",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
            workspace_dir=str(ws_1),
        )
        ch_b = ChannelConfig(
            id="ch_b",
            adapter="wechat",
            external_chat_id="wx_b",
            name="Peer Beta",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
            workspace_dir=str(ws_1),
        )
        ch_c = ChannelConfig(
            id="ch_c",
            adapter="feishu",
            external_chat_id="oc_c",
            name="Other Project",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
            workspace_dir=str(ws_2),
        )
        for ch in (ch_a, ch_b, ch_c):
            store.upsert_channel(ch)

        backend = _RecordingBackend("ag")
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(
                workspace_dir=ws_1,
                personal_workspace_dir=ws_1,
                language="zh",
                cross_channel_window_s=7200,
                cross_channel_max_items=5,
            ),
        )

        now_ms = int(time.time() * 1000)
        await engine.ingest_messages(
            ch_a.id,
            [
                NormalizedMessage(
                    message_id="m_a1",
                    channel_id=ch_a.id,
                    position=1,
                    create_ms=now_ms,
                    sender_id="ou_a",
                    sender_name="Peer Alpha",
                    clean_text="Controller Alpha heartbeat timeout",
                )
            ],
        )
        await engine.ingest_messages(
            ch_c.id,
            [
                NormalizedMessage(
                    message_id="m_c1",
                    channel_id=ch_c.id,
                    position=1,
                    create_ms=now_ms + 100,
                    sender_id="ou_c",
                    sender_name="Other Project",
                    clean_text="Secret ws2 unrelated message",
                )
            ],
        )
        await engine.handle_owner_action(classify_owner_command("Private personal query", notifier="imessage"))  # type: ignore[arg-type]

        await engine.ingest_messages(
            ch_b.id,
            [
                NormalizedMessage(
                    message_id="m_b1",
                    channel_id=ch_b.id,
                    position=1,
                    create_ms=now_ms + 200,
                    sender_id="wx_b",
                    sender_name="Peer Beta",
                    clean_text="Same controller timeout happening here too",
                )
            ],
        )

        prompt_b = backend.starts[-1][1]
        assert "## Recent Cross-Channel Activity (Last 2h — Same-Incident Reference Only)" in prompt_b
        assert "Peer Alpha (feishu)" in prompt_b
        assert "Other Project" not in prompt_b
        assert "Private personal query" not in prompt_b
        assert "个人直聊" not in prompt_b

    asyncio.run(_run())
