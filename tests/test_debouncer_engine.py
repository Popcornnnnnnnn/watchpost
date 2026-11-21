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
from watchpost.models import EngineConfig, NormalizedMessage, ThreadStatus, TriageMode
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
