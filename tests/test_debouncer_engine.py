"""Tests for BurstDebouncer, WatchpostEngine, MockAgentBackend, CLIAgentBackend, and LocalWhisperProcessor."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest import mock

from watchpost.adapters.simulator import SimulatorAdapter
from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.backends.mock import MockAgentBackend
from watchpost.debouncer import BurstDebouncer
from watchpost.engine import WatchpostEngine
from watchpost.media import LocalWhisperProcessor, clean_whisper_output
from watchpost.models import (
    AgentResult,
    AutonomyMode,
    ChannelConfig,
    ChatType,
    EngineConfig,
    NormalizedMessage,
    ThreadStatus,
    TriageMode,
    TriggerMode,
)
from watchpost.store import WatchpostStore


def test_async_burst_debouncer_coalesces_messages() -> None:
    flushed: list[tuple[str, list[str]]] = []

    async def _run() -> None:
        async def _cb(key: str, items: list[str]) -> None:
            flushed.append((key, items))

        deb = BurstDebouncer(_cb, debounce_s=0.02, max_debounce_s=0.05)
        deb.add("k1", ["a"])
        await asyncio.sleep(0.005)
        deb.add("k1", ["b"])
        await asyncio.sleep(0.05)
        assert deb.pending_count() == 0

    asyncio.run(_run())
    assert flushed == [("k1", ["a", "b"])]


def test_engine_simulator_handoff_filters_and_owner_go() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = WatchpostStore(root / "wp.db")
        sim = SimulatorAdapter()
        backend = MockAgentBackend(language="zh")
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(workspace_dir=root, language="zh"),
            adapters={"simulator": sim},
            media_dir=root / "media",
        )

        summary = asyncio.run(engine.run_demo_scenario())
        assert summary["channels_seeded"] == 2
        assert summary["threads_created"] == 2
        # Only Bucket-1 overnight photo downloaded; unrelated lunch image NOT downloaded
        assert summary["downloaded_media_ids"] == ["grp_1_photo"]

        threads = {t.channel_id: t for t in store.list_threads()}
        dm_thread = threads["sim_alex_dm"]
        grp_thread = threads["sim_site_alpha_group"]
        assert dm_thread.triage_mode == TriageMode.mode_1_qa
        assert dm_thread.status == ThreadStatus.completed
        assert dm_thread.pending_go is False

        assert grp_thread.triage_mode == TriageMode.mode_4_bug
        assert grp_thread.status == ThreadStatus.awaiting_go
        assert grp_thread.pending_go is True

        # Follow-up pure small talk / recalled message in same group produces 0 triggers
        ignored = asyncio.run(
            engine.ingest_messages(
                "sim_site_alpha_group",
                [
                    NormalizedMessage(
                        message_id="grp_del_2",
                        position=200,
                        sender_id="user_alex",
                        sender_name="Alex",
                        deleted=True,
                        clean_text="[Recalled]",
                    ),
                    NormalizedMessage(
                        message_id="grp_ack_2",
                        position=201,
                        sender_id="user_casey",
                        sender_name="Casey",
                        clean_text="OKOK",
                    ),
                ],
            )
        )
        assert ignored == []

        # Owner approves "go" -> backend steers thread & clears pending_go
        approved = asyncio.run(engine.steer_from_owner(grp_thread.id, "go", approved_go=True))
        assert approved.pending_go is False
        assert approved.status == ThreadStatus.completed
        assert store.get_metrics()["awaiting_go"] == 0


def test_cli_agent_backend_and_whisper_cleanup() -> None:
    assert clean_whisper_output("[BLANK_AUDIO]\n Node 1 OK \n(SILENCE)\n") == "Node 1 OK"

    async def _run_cli() -> None:
        cli = CLIAgentBackend("claude_code")
        fake_proc = mock.AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = mock.AsyncMock(
            return_value=(b'{"session_id":"abc-123"}\nSummary:\nAll good.\n\nSuggested Reply:\nNode OK.\n\nNeeds Attention:\nNone', b"")
        )
        with mock.patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            ref, res = await cli.start_thread("Test", "Prompt", cwd=Path("."))
            assert ref == "abc-123"
            assert res.suggested_reply == "Node OK."

    asyncio.run(_run_cli())

    proc = LocalWhisperProcessor(model_path=None)
    txt, err = proc.transcribe_audio(Path("/nonexistent/audio.opus"))
    assert txt == "" and "missing" in err.lower()


def test_auto_fix_deploy_requires_go_and_codex_sandbox_selection() -> None:
    class _DeployGateBackend:
        name = "stub"

        async def start_thread(
            self,
            title: str,
            prompt: str,
            *,
            cwd: Path,
            model: str = "",
            channel: ChannelConfig | None = None,
        ) -> tuple[str, AgentResult]:
            del title, prompt, cwd, model, channel
            raw = (
                "Summary:\nFixed parser locally and ran unit tests.\n\n"
                "Suggested Reply:\nFixed locally; awaiting go to deploy.\n\n"
                "Needs Attention:\nReply go to deploy to field IPC"
            )
            return "ref-autofix-1", AgentResult(
                raw_text=raw,
                suggested_reply="Fixed locally; awaiting go to deploy.",
                needs_attention="Reply go to deploy to field IPC",
                triage_mode=TriageMode.mode_4_bug,
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
            del backend_thread_ref, title, message, cwd, channel
            raw = "Summary:\nDeployed fix.\n\nSuggested Reply:\nDeployed.\n\nNeeds Attention:\nNone"
            return AgentResult(raw_text=raw, suggested_reply="Deployed.", triage_mode=TriageMode.mode_4_bug)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = WatchpostStore(root / "wp.db")
        ch = ChannelConfig(
            id="feishu_autofix",
            adapter="simulator",
            external_chat_id="chat_1",
            name="Field Ops",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.auto_fix,
        )
        store.upsert_channel(ch)
        engine = WatchpostEngine(
            store,
            _DeployGateBackend(),
            config=EngineConfig(workspace_dir=root, language="en"),
            adapters={"simulator": SimulatorAdapter()},
            media_dir=root / "media",
        )
        msg = NormalizedMessage(
            message_id="m_bug_1",
            position=1,
            sender_id="u_1",
            sender_name="Operator",
            create_time="10:00:00",
            clean_text="Bay 2 fault after reboot",
        )
        created = asyncio.run(engine.ingest_messages(ch.id, [msg]))
        assert len(created) == 1
        assert created[0].pending_go is True
        assert created[0].status == ThreadStatus.awaiting_go
        assert store.get_metrics()["awaiting_go"] == 1

        approved = asyncio.run(engine.steer_from_owner(created[0].id, "go", approved_go=True))
        assert approved.pending_go is False
        assert approved.status == ThreadStatus.completed
        assert store.get_metrics()["awaiting_go"] == 0

    async def _run_codex_sandbox_checks() -> None:
        recorded_cmds: list[list[str]] = []

        def _make_proc(*args: object, **_kwargs: object) -> mock.AsyncMock:
            recorded_cmds.append([str(a) for a in args])
            proc = mock.AsyncMock()
            proc.returncode = 0
            proc.communicate = mock.AsyncMock(
                return_value=(b'{"thread_id":"th-1"}\nSummary:\nOK.\n\nSuggested Reply:\nOK.\n\nNeeds Attention:\nNone', b"")
            )
            return proc

        auto_ch = ChannelConfig(
            id="c_auto",
            adapter="feishu",
            external_chat_id="x1",
            name="Auto",
            autonomy_mode=AutonomyMode.auto_fix,
        )
        diag_ch = ChannelConfig(
            id="c_diag",
            adapter="feishu",
            external_chat_id="x2",
            name="Diag",
            autonomy_mode=AutonomyMode.diagnose_only,
        )

        with mock.patch("asyncio.create_subprocess_exec", side_effect=_make_proc):
            codex_auto = CLIAgentBackend("codex", sandbox_mode="")
            await codex_auto.start_thread("T1", "P1", cwd=Path("."), channel=auto_ch)
            await codex_auto.start_thread("T2", "P2", cwd=Path("."), channel=diag_ch)

            codex_pinned = CLIAgentBackend("codex", sandbox_mode="read-only")
            await codex_pinned.start_thread("T3", "P3", cwd=Path("."), channel=auto_ch)

        assert ["--sandbox", "workspace-write"] == [recorded_cmds[0][-3], recorded_cmds[0][-2]]
        assert ["--sandbox", "read-only"] == [recorded_cmds[1][-3], recorded_cmds[1][-2]]
        assert ["--sandbox", "read-only"] == [recorded_cmds[2][-3], recorded_cmds[2][-2]]

    asyncio.run(_run_codex_sandbox_checks())
