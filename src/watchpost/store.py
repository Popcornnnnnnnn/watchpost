"""Atomic SQLite WAL store for Watchpost channels, messages, threads, events, and bindings."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from watchpost.models import (
    AutonomyMode,
    ChannelConfig,
    ChatType,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
    ThreadEvent,
    ThreadRecord,
    ThreadStatus,
    TriageMode,
    TriggerMode,
)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    name TEXT NOT NULL,
    chat_type TEXT NOT NULL DEFAULT 'p2p',
    trigger_mode TEXT NOT NULL DEFAULT 'all',
    autonomy_mode TEXT NOT NULL DEFAULT 'diagnose_only',
    ignore_bots INTEGER NOT NULL DEFAULT 1,
    persona_text TEXT NOT NULL DEFAULT '',
    whisper_prompt TEXT NOT NULL DEFAULT '',
    same_sender_window_s INTEGER NOT NULL DEFAULT 300,
    handoff_window_s INTEGER NOT NULL DEFAULT 43200,
    background_window_s INTEGER NOT NULL DEFAULT 64800,
    group_sender_ttl_s INTEGER NOT NULL DEFAULT 900,
    group_reply_ttl_s INTEGER NOT NULL DEFAULT 86400,
    session_ttl_s INTEGER NOT NULL DEFAULT 3600,
    max_catchup_age_s INTEGER NOT NULL DEFAULT 7200,
    cursor_pos INTEGER NOT NULL DEFAULT 0,
    cursor_ms INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    external_msg_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    create_ms INTEGER NOT NULL DEFAULT 0,
    create_time TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    sender_type TEXT NOT NULL DEFAULT 'user',
    is_owner INTEGER NOT NULL DEFAULT 0,
    is_bot INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    msg_type TEXT NOT NULL DEFAULT 'text',
    raw_content TEXT NOT NULL DEFAULT '',
    clean_text TEXT NOT NULL DEFAULT '',
    reply_to_id TEXT NOT NULL DEFAULT '',
    reply_preview TEXT NOT NULL DEFAULT '',
    mentions_json TEXT NOT NULL DEFAULT '[]',
    mentions_owner INTEGER NOT NULL DEFAULT 0,
    mentions_all INTEGER NOT NULL DEFAULT 0,
    media_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(channel_id, external_msg_id),
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_pos ON messages(channel_id, position, create_ms);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    backend_thread_ref TEXT NOT NULL,
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    root_msg_id TEXT NOT NULL DEFAULT '',
    message_ids_json TEXT NOT NULL DEFAULT '[]',
    triage_mode TEXT NOT NULL DEFAULT 'mode_1_qa',
    pending_go INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_threads_channel_updated ON threads(channel_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS thread_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary_bubbles_json TEXT NOT NULL DEFAULT '[]',
    suggested_reply TEXT NOT NULL DEFAULT '',
    needs_attention TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    draft_copied INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbound_bindings (
    notifier TEXT NOT NULL,
    external_reply_token TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(notifier, external_reply_token),
    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
);
"""


def _row_to_channel(row: sqlite3.Row) -> ChannelConfig:
    return ChannelConfig(
        id=row["id"],
        adapter=row["adapter"],
        external_chat_id=row["external_chat_id"],
        name=row["name"],
        chat_type=ChatType(row["chat_type"]),
        trigger_mode=TriggerMode(row["trigger_mode"]),
        autonomy_mode=AutonomyMode(row["autonomy_mode"]),
        ignore_bots=bool(row["ignore_bots"]),
        persona_text=row["persona_text"],
        whisper_prompt=row["whisper_prompt"],
        same_sender_window_s=int(row["same_sender_window_s"]),
        handoff_window_s=int(row["handoff_window_s"]),
        background_window_s=int(row["background_window_s"]),
        group_sender_ttl_s=int(row["group_sender_ttl_s"]),
        group_reply_ttl_s=int(row["group_reply_ttl_s"]),
        session_ttl_s=int(row["session_ttl_s"]),
        max_catchup_age_s=int(row["max_catchup_age_s"]),
        cursor_pos=int(row["cursor_pos"]),
        cursor_ms=int(row["cursor_ms"]),
        enabled=bool(row["enabled"]),
    )


def _row_to_message(row: sqlite3.Row) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=row["external_msg_id"],
        channel_id=row["channel_id"],
        position=int(row["position"]),
        create_ms=int(row["create_ms"]),
        create_time=row["create_time"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        sender_type=row["sender_type"],
        is_owner=bool(row["is_owner"]),
        is_bot=bool(row["is_bot"]),
        deleted=bool(row["deleted"]),
        msg_type=row["msg_type"],
        raw_content=row["raw_content"],
        clean_text=row["clean_text"],
        reply_to_id=row["reply_to_id"],
        reply_preview=row["reply_preview"],
        mentions=[Mention(**x) for x in json.loads(row["mentions_json"] or "[]")],
        mentions_owner=bool(row["mentions_owner"]),
        mentions_all=bool(row["mentions_all"]),
        media_refs=[MediaRef(**x) for x in json.loads(row["media_json"] or "[]")],
        artifacts=[MediaArtifact(**x) for x in json.loads(row["artifacts_json"] or "[]")],
    )


def _row_to_thread(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(
        id=row["id"],
        channel_id=row["channel_id"],
        backend=row["backend"],
        backend_thread_ref=row["backend_thread_ref"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        title=row["title"],
        status=ThreadStatus(row["status"]),
        root_msg_id=row["root_msg_id"],
        message_ids=list(json.loads(row["message_ids_json"] or "[]")),
        triage_mode=TriageMode(row["triage_mode"]),
        pending_go=bool(row["pending_go"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class WatchpostStore:
    """Thread-safe SQLite WAL persistence backend."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_channel(self, channel: ChannelConfig) -> ChannelConfig:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, adapter, external_chat_id, name, chat_type, trigger_mode, autonomy_mode,
                    ignore_bots, persona_text, whisper_prompt, same_sender_window_s, handoff_window_s,
                    background_window_s, group_sender_ttl_s, group_reply_ttl_s, session_ttl_s,
                    max_catchup_age_s, cursor_pos, cursor_ms, enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    adapter=excluded.adapter,
                    external_chat_id=excluded.external_chat_id,
                    name=excluded.name,
                    chat_type=excluded.chat_type,
                    trigger_mode=excluded.trigger_mode,
                    autonomy_mode=excluded.autonomy_mode,
                    ignore_bots=excluded.ignore_bots,
                    persona_text=excluded.persona_text,
                    whisper_prompt=excluded.whisper_prompt,
                    same_sender_window_s=excluded.same_sender_window_s,
                    handoff_window_s=excluded.handoff_window_s,
                    background_window_s=excluded.background_window_s,
                    group_sender_ttl_s=excluded.group_sender_ttl_s,
                    group_reply_ttl_s=excluded.group_reply_ttl_s,
                    session_ttl_s=excluded.session_ttl_s,
                    max_catchup_age_s=excluded.max_catchup_age_s,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    channel.id,
                    channel.adapter,
                    channel.external_chat_id,
                    channel.name,
                    channel.chat_type.value,
                    channel.trigger_mode.value,
                    channel.autonomy_mode.value,
                    int(channel.ignore_bots),
                    channel.persona_text,
                    channel.whisper_prompt,
                    channel.same_sender_window_s,
                    channel.handoff_window_s,
                    channel.background_window_s,
                    channel.group_sender_ttl_s,
                    channel.group_reply_ttl_s,
                    channel.session_ttl_s,
                    channel.max_catchup_age_s,
                    channel.cursor_pos,
                    channel.cursor_ms,
                    int(channel.enabled),
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel.id,)).fetchone()
        return _row_to_channel(row)

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
            return _row_to_channel(row) if row else None

    def list_channels(self) -> list[ChannelConfig]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM channels ORDER BY name ASC").fetchall()
            return [_row_to_channel(r) for r in rows]

    def update_channel_settings(
        self,
        channel_id: str,
        *,
        enabled: bool | None = None,
        autonomy_mode: AutonomyMode | None = None,
        trigger_mode: TriggerMode | None = None,
        persona_text: str | None = None,
        whisper_prompt: str | None = None,
    ) -> ChannelConfig | None:
        updates: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(int(enabled))
        if autonomy_mode is not None:
            updates.append("autonomy_mode = ?")
            params.append(autonomy_mode.value)
        if trigger_mode is not None:
            updates.append("trigger_mode = ?")
            params.append(trigger_mode.value)
        if persona_text is not None:
            updates.append("persona_text = ?")
            params.append(persona_text)
        if whisper_prompt is not None:
            updates.append("whisper_prompt = ?")
            params.append(whisper_prompt)
        if not updates:
            return self.get_channel(channel_id)
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(channel_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE channels SET {', '.join(updates)} WHERE id = ?", params)
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
            return _row_to_channel(row) if row else None

    def advance_channel_cursor(self, channel_id: str, cursor_pos: int, cursor_ms: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channels
                SET cursor_pos = MAX(cursor_pos, ?),
                    cursor_ms = MAX(cursor_ms, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (cursor_pos, cursor_ms, time.time(), channel_id),
            )

    def record_messages(self, channel_id: str, msgs: list[NormalizedMessage]) -> None:
        if not msgs:
            return
        with self._connect() as conn:
            for msg in msgs:
                uid = f"{channel_id}:{msg.message_id}"
                conn.execute(
                    """
                    INSERT INTO messages (
                        id, channel_id, external_msg_id, position, create_ms, create_time,
                        sender_id, sender_name, sender_type, is_owner, is_bot, deleted, msg_type,
                        raw_content, clean_text, reply_to_id, reply_preview,
                        mentions_json, mentions_owner, mentions_all, media_json, artifacts_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, external_msg_id) DO UPDATE SET
                        artifacts_json=excluded.artifacts_json,
                        deleted=excluded.deleted
                    """,
                    (
                        uid,
                        channel_id,
                        msg.message_id,
                        msg.position,
                        msg.create_ms,
                        msg.create_time,
                        msg.sender_id,
                        msg.sender_name,
                        msg.sender_type,
                        int(msg.is_owner),
                        int(msg.is_bot),
                        int(msg.deleted),
                        msg.msg_type,
                        msg.raw_content,
                        msg.clean_text,
                        msg.reply_to_id,
                        msg.reply_preview,
                        json.dumps([m.model_dump() for m in msg.mentions], ensure_ascii=False),
                        int(msg.mentions_owner),
                        int(msg.mentions_all),
                        json.dumps([r.model_dump() for r in msg.media_refs], ensure_ascii=False),
                        json.dumps([a.model_dump() for a in msg.artifacts], ensure_ascii=False),
                    ),
                )

    def get_recent_messages(self, channel_id: str, *, limit: int = 50) -> list[NormalizedMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE channel_id = ? AND deleted = 0 AND msg_type != 'system'
                ORDER BY position DESC, create_ms DESC
                LIMIT ?
                """,
                (channel_id, int(limit)),
            ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    def get_messages_by_external_ids(self, channel_id: str, message_ids: list[str]) -> list[NormalizedMessage]:
        clean_ids = [mid for mid in message_ids if mid]
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE channel_id = ? AND external_msg_id IN ({placeholders})",
                [channel_id, *clean_ids],
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def create_or_update_thread(self, thread: ThreadRecord) -> ThreadRecord:
        now = time.time()
        created = thread.created_at or now
        updated = thread.updated_at or now
        mids = thread.message_ids[-50:]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads (
                    id, channel_id, backend, backend_thread_ref, sender_id, sender_name, title,
                    status, root_msg_id, message_ids_json, triage_mode, pending_go, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    backend_thread_ref=excluded.backend_thread_ref,
                    title=excluded.title,
                    status=excluded.status,
                    message_ids_json=excluded.message_ids_json,
                    triage_mode=excluded.triage_mode,
                    pending_go=excluded.pending_go,
                    updated_at=excluded.updated_at
                """,
                (
                    thread.id,
                    thread.channel_id,
                    thread.backend,
                    thread.backend_thread_ref,
                    thread.sender_id,
                    thread.sender_name,
                    thread.title,
                    thread.status.value,
                    thread.root_msg_id,
                    json.dumps(mids, ensure_ascii=False),
                    thread.triage_mode.value,
                    int(thread.pending_go),
                    created,
                    updated,
                ),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread.id,)).fetchone()
        return _row_to_thread(row)

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            return _row_to_thread(row) if row else None

    def list_threads(self, *, limit: int = 50) -> list[ThreadRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_thread(r) for r in rows]

    def list_channel_group_threads(self, channel_id: str, *, limit: int = 20) -> list[ThreadRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE channel_id = ? ORDER BY updated_at DESC LIMIT ?",
                (channel_id, int(limit)),
            ).fetchall()
        return [_row_to_thread(r) for r in rows]

    def find_active_p2p_thread(self, channel_id: str, *, ttl_s: float, now_ts: float | None = None) -> ThreadRecord | None:
        now = now_ts if now_ts is not None else time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE channel_id = ? ORDER BY updated_at DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
        if not row:
            return None
        th = _row_to_thread(row)
        if (now - th.updated_at) <= ttl_s:
            return th
        return None

    def add_thread_event(self, event: ThreadEvent) -> ThreadEvent:
        ts = event.created_at or time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO thread_events (
                    thread_id, kind, summary_bubbles_json, suggested_reply, needs_attention,
                    raw_text, draft_copied, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.thread_id,
                    event.kind,
                    json.dumps(event.summary_bubbles, ensure_ascii=False),
                    event.suggested_reply,
                    event.needs_attention,
                    event.raw_text,
                    int(event.draft_copied),
                    ts,
                ),
            )
            ev_id = int(cur.lastrowid or 0)
        return event.model_copy(update={"id": ev_id, "created_at": ts})

    def list_thread_events(self, thread_id: str) -> list[ThreadEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM thread_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
        return [
            ThreadEvent(
                id=int(r["id"]),
                thread_id=r["thread_id"],
                kind=r["kind"],
                summary_bubbles=list(json.loads(r["summary_bubbles_json"] or "[]")),
                suggested_reply=r["suggested_reply"],
                needs_attention=r["needs_attention"],
                raw_text=r["raw_text"],
                draft_copied=bool(r["draft_copied"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    def mark_draft_copied(self, thread_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE thread_events
                SET draft_copied = 1
                WHERE thread_id = ? AND suggested_reply != ''
                """,
                (thread_id,),
            )
            return int(cur.rowcount)

    def bind_outbound(self, notifier: str, token: str, thread_id: str) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbound_bindings (notifier, external_reply_token, thread_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(notifier, external_reply_token) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at
                """,
                (notifier, token, thread_id, time.time()),
            )

    def resolve_outbound(self, notifier: str, token: str) -> str:
        if not token:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM outbound_bindings WHERE notifier = ? AND external_reply_token = ?",
                (notifier, token),
            ).fetchone()
        return str(row["thread_id"]) if row else ""

    def get_metrics(self) -> dict[str, int]:
        with self._connect() as conn:
            ch_count = int(conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0])
            msg_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            th_count = int(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            go_count = int(conn.execute("SELECT COUNT(*) FROM threads WHERE pending_go = 1").fetchone()[0])
            copied_count = int(conn.execute("SELECT COUNT(*) FROM thread_events WHERE draft_copied = 1").fetchone()[0])
        return {
            "channels": ch_count,
            "messages": msg_count,
            "threads": th_count,
            "awaiting_go": go_count,
            "drafts_copied": copied_count,
        }
