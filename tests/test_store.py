"""Tests for Watchpost SQLite WAL store."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from watchpost.models import (
    AutonomyMode,
    ChannelConfig,
    ChatType,
    NormalizedMessage,
    ThreadEvent,
    ThreadRecord,
    ThreadStatus,
    TriageMode,
    TriggerMode,
)
from watchpost.store import WatchpostStore


def test_schema_wal_and_channel_thread_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "watchpost.db"
        store = WatchpostStore(db_path)

        with sqlite3.connect(db_path) as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            assert str(mode).lower() == "wal"
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        assert {"channels", "messages", "threads", "thread_events", "outbound_bindings"}.issubset(tables)

        ch = ChannelConfig(
            id="ch_1",
            adapter="feishu",
            external_chat_id="chat_1",
            name="Alex",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
            persona_text="Site Alpha ops engineer",
        )
        saved = store.upsert_channel(ch)
        assert saved.name == "Alex"

        patched = store.update_channel_settings("ch_1", enabled=False, autonomy_mode=AutonomyMode.auto_fix)
        assert patched is not None
        assert patched.enabled is False
        assert patched.autonomy_mode == AutonomyMode.auto_fix

        store.record_messages(
            "ch_1",
            [
                NormalizedMessage(message_id="m1", position=10, create_ms=1000, sender_id="u1", clean_text="hi"),
                NormalizedMessage(message_id="m2", position=11, create_ms=2000, sender_id="u1", clean_text="status?"),
            ],
        )
        recent = store.get_recent_messages("ch_1")
        assert [m.message_id for m in recent] == ["m1", "m2"]
        assert len(store.get_messages_by_external_ids("ch_1", ["m2"])) == 1

        now = time.time()
        th = store.create_or_update_thread(
            ThreadRecord(
                id="th_1",
                channel_id="ch_1",
                backend="mock",
                backend_thread_ref="ref_1",
                sender_id="u1",
                sender_name="Alex",
                title="Status check",
                status=ThreadStatus.awaiting_go,
                root_msg_id="m2",
                message_ids=["m2"],
                triage_mode=TriageMode.mode_4_bug,
                pending_go=True,
                created_at=now,
                updated_at=now,
            )
        )
        assert th.pending_go is True

        store.add_thread_event(
            ThreadEvent(
                thread_id="th_1",
                kind="agent_reply",
                summary_bubbles=["Found timeout in logs."],
                suggested_reply="已经定位问题，待确认后处理。",
                needs_attention="Approve go",
            )
        )
        assert store.mark_draft_copied("th_1") == 1
        store.bind_outbound("imessage", "guid-999", "th_1")
        assert store.resolve_outbound("imessage", "guid-999") == "th_1"

        metrics = store.get_metrics()
        assert metrics == {
            "channels": 1,
            "messages": 2,
            "threads": 1,
            "awaiting_go": 1,
            "drafts_copied": 1,
        }
