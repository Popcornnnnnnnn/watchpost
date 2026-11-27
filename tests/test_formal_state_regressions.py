"""Regression tests for Watchpost thread lifecycle, locking, and go-claim invariants."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

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
    TriageMode,
    TriggerMode,
)
from watchpost.store import WatchpostStore


class _DummyNotifier:
    name = "dummy"

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify_start(self, channel, thread, preview, *, is_steer=False, attachments=None):
        return []

    async def notify_completion(self, channel, thread, bubbles):
        return []

    async def poll_owner_actions(self, *, defer_commit: bool = False):
        return []

    async def send_message(self, text: str) -> None:
        self.messages.append(text)


class _FailingMediaProcessor:
    def transcribe_audio(self, path: Path, *, prompt: str = "", language: str = "en"):
        raise RuntimeError("simulated audio failure")


class _ControlledBackend:
    name = "controlled"

    def __init__(self) -> None:
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.steer_ro_entered = asyncio.Event()
        self.release_steer_ro = asyncio.Event()
        self.hold_start = False
        self.hold_steer_ro = False
        self.fail_next_start = False
        self.fail_next_write = False
        self.start_count = 0
        self.steer_refs_seen: list[str] = []
        self.steer_calls: list[tuple[str, bool]] = []
        self.active_calls: set[tuple[int, bool]] = set()
        self.max_in_flight = 0
        self.saw_concurrent_rw = False
        self.approved_exec_count = 0
        self._seq = 0

    async def start_thread(self, title: str, prompt: str, *, cwd: Path, model: str = "", channel=None):
        self.start_count += 1
        if self.hold_start:
            self.start_entered.set()
            await self.release_start.wait()
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("simulated start failure")
        return f"real_ref_{self.start_count}", AgentResult(
            raw_text="Diagnosis: real bug\nNeeds attention: yes",
            summary_bubbles=["Real bug found"],
            needs_attention="Needs fix",
            triage_mode=TriageMode.mode_4_bug,
        )

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel=None,
        approved_go: bool = False,
    ):
        self._seq += 1
        call_id = (self._seq, approved_go)
        self.steer_refs_seen.append(backend_thread_ref)
        self.steer_calls.append((backend_thread_ref, approved_go))
        if approved_go:
            self.approved_exec_count += 1
        if any(flag != approved_go for _, flag in self.active_calls):
            self.saw_concurrent_rw = True
        self.active_calls.add(call_id)
        self.max_in_flight = max(self.max_in_flight, len(self.active_calls))
        try:
            if approved_go and self.fail_next_write:
                self.fail_next_write = False
                raise RuntimeError("simulated write failure")
            if self.hold_steer_ro and not approved_go:
                self.steer_ro_entered.set()
                await self.release_steer_ro.wait()
            else:
                await asyncio.sleep(0.03)
            return AgentResult(
                raw_text="Follow-up done",
                summary_bubbles=["Follow-up done"],
                triage_mode=TriageMode.mode_1_qa,
            )
        finally:
            self.active_calls.discard(call_id)


def _make_channel(ws: Path, channel_id: str = "ch1") -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        adapter="simulator",
        external_chat_id=f"chat_{channel_id}",
        name="Support Group",
        chat_type=ChatType.group,
        trigger_mode=TriggerMode.all,
        autonomy_mode=AutonomyMode.diagnose_only,
        workspace_dir=str(ws),
        enabled=True,
    )


def test_concurrent_inbound_batches_never_steer_or_overwrite_placeholder_ref(tmp_path: Path) -> None:
    async def _run() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        store = WatchpostStore(tmp_path / "wp.db")
        ch = store.upsert_channel(_make_channel(ws))
        backend = _ControlledBackend()
        backend.hold_start = True
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(workspace_dir=ws, language="en"),
            notifiers=[_DummyNotifier()],
        )
        now_ms = int(time.time() * 1000)
        m1 = NormalizedMessage(
            message_id="m1",
            channel_id=ch.id,
            position=1,
            create_ms=now_ms - 2000,
            sender_id="u1",
            sender_name="Alice",
            clean_text="prod API returns 500",
            mentions_owner=True,
        )
        m2 = NormalizedMessage(
            message_id="m2",
            channel_id=ch.id,
            position=2,
            create_ms=now_ms - 1000,
            sender_id="u1",
            sender_name="Alice",
            clean_text="attached full traceback",
            mentions_owner=True,
        )

        t1 = asyncio.create_task(engine.ingest_messages(ch.id, [m1]))
        await backend.start_entered.wait()
        t2 = asyncio.create_task(engine.ingest_messages(ch.id, [m2]))
        await asyncio.sleep(0.05)
        assert backend.steer_refs_seen == []

        backend.release_start.set()
        await asyncio.gather(t1, t2)

        assert len(backend.steer_refs_seen) == 1
        assert backend.steer_refs_seen[0] == "real_ref_1"
        stored = store.get_thread(store.list_threads()[0].id)
        assert stored is not None
        assert stored.backend_thread_ref == "real_ref_1"

        # Store guard also rejects stale placeholder overwrites directly.
        overwritten = store.create_or_update_thread(
            stored.model_copy(update={"backend_thread_ref": f"pending_{stored.id}"})
        )
        assert overwritten.backend_thread_ref == "real_ref_1"

    asyncio.run(_run())


def test_workspace_rw_lock_prevents_concurrent_write_and_read_turns(tmp_path: Path) -> None:
    async def _run() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        store = WatchpostStore(tmp_path / "wp.db")
        ch = store.upsert_channel(_make_channel(ws))
        store.create_or_update_thread(
            ThreadRecord(
                id="th_1",
                channel_id=ch.id,
                backend="controlled",
                backend_thread_ref="real_ref_1",
                sender_id="u1",
                sender_name="Alice",
                title="Bug 1",
                status=ThreadStatus.awaiting_go,
                triage_mode=TriageMode.mode_4_bug,
                pending_go=True,
            )
        )
        store.create_or_update_thread(
            ThreadRecord(
                id="th_2",
                channel_id=ch.id,
                backend="controlled",
                backend_thread_ref="real_ref_2",
                sender_id="u2",
                sender_name="Bob",
                title="Question 2",
                status=ThreadStatus.completed,
                triage_mode=TriageMode.mode_1_qa,
                pending_go=False,
            )
        )
        backend = _ControlledBackend()
        backend.hold_steer_ro = True
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(workspace_dir=ws, language="en"),
            notifiers=[_DummyNotifier()],
        )

        # Hold a read-only turn on th_2, then start an approved_go write on th_1 sharing the same workspace.
        t_ro = asyncio.create_task(engine.steer_from_owner("th_2", "explain more", approved_go=False))
        await backend.steer_ro_entered.wait()
        t_go = asyncio.create_task(engine.steer_from_owner("th_1", "go", approved_go=True))
        await asyncio.sleep(0.05)

        assert backend.max_in_flight == 1
        assert backend.saw_concurrent_rw is False
        assert backend.approved_exec_count == 0

        backend.release_steer_ro.set()
        await asyncio.gather(t_ro, t_go)
        assert backend.saw_concurrent_rw is False
        assert backend.approved_exec_count == 1

    asyncio.run(_run())


def test_go_claim_failure_and_recovery_never_leave_zombie_awaiting_go(tmp_path: Path) -> None:
    async def _run() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"fake-ogg")

        store = WatchpostStore(tmp_path / "wp.db")
        ch = store.upsert_channel(_make_channel(ws))
        store.create_or_update_thread(
            ThreadRecord(
                id="th_prep_fail",
                channel_id=ch.id,
                backend="controlled",
                backend_thread_ref="real_ref_1",
                sender_id="u1",
                sender_name="Alice",
                title="Voice go",
                status=ThreadStatus.awaiting_go,
                triage_mode=TriageMode.mode_4_bug,
                pending_go=True,
            )
        )
        notifier = _DummyNotifier()
        backend = _ControlledBackend()
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(workspace_dir=ws, language="en"),
            notifiers=[notifier],
            media_processor=_FailingMediaProcessor(),
        )

        # Prep failure happens BEFORE claim_pending_go, so pending_go remains True and retryable.
        with pytest.raises(RuntimeError, match="simulated audio failure"):
            await engine.handle_owner_action(
                OwnerAction(
                    notifier="dummy",
                    kind="approve_go",
                    thread_id="th_prep_fail",
                    attachments=[{"path": str(audio_file), "name": "voice.ogg", "is_audio": "true"}],
                )
            )
        after_prep_err = store.get_thread("th_prep_fail")
        assert after_prep_err is not None
        assert after_prep_err.status == ThreadStatus.awaiting_go
        assert after_prep_err.pending_go is True

        # Retry without bad attachment executes cleanly, and at most once on concurrent go.
        act = OwnerAction(notifier="dummy", kind="approve_go", thread_id="th_prep_fail")
        r1, r2 = await asyncio.gather(engine.handle_owner_action(act), engine.handle_owner_action(act))
        assert backend.approved_exec_count == 1
        assert len([r for r in (r1, r2) if r is not None]) == 1

        # Failure after claim_pending_go moves thread to error rather than zombie awaiting_go.
        store.create_or_update_thread(
            ThreadRecord(
                id="th_write_fail",
                channel_id=ch.id,
                backend="controlled",
                backend_thread_ref="real_ref_2",
                sender_id="u1",
                sender_name="Alice",
                title="Write fail",
                status=ThreadStatus.awaiting_go,
                triage_mode=TriageMode.mode_4_bug,
                pending_go=True,
            )
        )
        backend.fail_next_write = True
        with pytest.raises(RuntimeError, match="simulated write failure"):
            await engine.handle_owner_action(OwnerAction(notifier="dummy", kind="approve_go", thread_id="th_write_fail"))
        after_write_err = store.get_thread("th_write_fail")
        assert after_write_err is not None
        assert after_write_err.status == ThreadStatus.error
        assert after_write_err.pending_go is False

        # Legacy zombie rows (status='awaiting_go', pending_go=False) are healed by recovery & dismiss_all.
        store.create_or_update_thread(
            ThreadRecord(
                id="th_legacy_zombie",
                channel_id=ch.id,
                backend="controlled",
                backend_thread_ref="real_ref_3",
                sender_id="u1",
                sender_name="Alice",
                title="Legacy zombie",
                status=ThreadStatus.awaiting_go,
                triage_mode=TriageMode.mode_4_bug,
                pending_go=False,
            )
        )
        assert store.dismiss_all_pending_go() == 1
        assert store.get_thread("th_legacy_zombie").status == ThreadStatus.completed  # type: ignore[union-attr]

        store.create_or_update_thread(
            ThreadRecord(
                id="th_legacy_zombie_2",
                channel_id=ch.id,
                backend="controlled",
                backend_thread_ref="real_ref_4",
                sender_id="u1",
                sender_name="Alice",
                title="Legacy zombie 2",
                status=ThreadStatus.awaiting_go,
                triage_mode=TriageMode.mode_4_bug,
                pending_go=False,
            )
        )
        assert store.recover_stale_active_threads() == 1
        assert store.get_thread("th_legacy_zombie_2").status == ThreadStatus.error  # type: ignore[union-attr]

    asyncio.run(_run())


def test_errored_thread_with_placeholder_ref_starts_fresh_on_follow_up(tmp_path: Path) -> None:
    async def _run() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        store = WatchpostStore(tmp_path / "wp.db")
        ch = store.upsert_channel(_make_channel(ws))
        backend = _ControlledBackend()
        backend.fail_next_start = True
        engine = WatchpostEngine(
            store,
            backend,
            config=EngineConfig(workspace_dir=ws, language="en"),
            notifiers=[_DummyNotifier()],
        )
        now_ms = int(time.time() * 1000)
        m1 = NormalizedMessage(
            message_id="m1",
            channel_id=ch.id,
            position=1,
            create_ms=now_ms - 2000,
            sender_id="u1",
            sender_name="Alice",
            clean_text="service crashed on boot",
            mentions_owner=True,
        )
        with pytest.raises(RuntimeError, match="simulated start failure"):
            await engine.ingest_messages(ch.id, [m1])

        th = store.list_threads()[0]
        assert th.status == ThreadStatus.error
        assert th.backend_thread_ref.startswith("pending_")

        # Owner steering that errored thread must call start_thread, never steer_thread('pending_...').
        updated = await engine.steer_from_owner(th.id, "retry now", approved_go=False)
        assert backend.steer_refs_seen == []
        assert backend.start_count == 2
        assert updated.backend_thread_ref == "real_ref_2"

    asyncio.run(_run())
