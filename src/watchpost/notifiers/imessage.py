"""macOS iMessage NotifierAdapter via safe argv osascript and read-only chat.db polling."""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable

from watchpost.config import IMessageConfig
from watchpost.models import ChannelConfig, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice

_APPLESCRIPT_SEND = """on run argv
    set targetHandle to item 1 of argv
    set targetText to item 2 of argv
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant targetHandle of targetService
        send targetText to targetBuddy
    end tell
end run
"""

_PRINTABLE_CHUNK_RE = re.compile(r"[^\x00-\x08\x0b\x0c\x0e-\x1f]{1,4096}")


def extract_attributed_body_text(blob: bytes | None) -> str:
    """Best-effort extract plain UTF-8 message text from Apple typedstream attributedBody blobs."""
    if not blob:
        return ""
    marker = b"NSString"
    idx = blob.find(marker)
    if idx >= 0:
        tail = blob[idx + len(marker) :]
        plus_idx = tail.find(b"+")
        if 0 <= plus_idx <= 16:
            after_plus = tail[plus_idx + 1 :]
            if after_plus:
                len_byte = after_plus[0]
                if len_byte < 0x80:
                    raw = after_plus[1 : 1 + len_byte]
                    decoded = raw.decode("utf-8", errors="ignore").strip()
                    if decoded:
                        return decoded
                elif len_byte == 0x81 and len(after_plus) >= 3:
                    n = int.from_bytes(after_plus[1:3], "little")
                    raw = after_plus[3 : 3 + n]
                    decoded = raw.decode("utf-8", errors="ignore").strip()
                    if decoded:
                        return decoded
                elif len_byte == 0x82 and len(after_plus) >= 5:
                    n = int.from_bytes(after_plus[1:5], "little")
                    raw = after_plus[5 : 5 + n]
                    decoded = raw.decode("utf-8", errors="ignore").strip()
                    if decoded:
                        return decoded
        end_marker = tail.find(b"\x86\x84")
        chunk = tail[:end_marker] if end_marker > 0 else tail
        text = chunk.decode("utf-8", errors="ignore")
        matches = _PRINTABLE_CHUNK_RE.findall(text)
        if matches:
            return max(matches, key=len).lstrip("+ ").strip()
    decoded = blob.decode("utf-8", errors="ignore")
    matches = _PRINTABLE_CHUNK_RE.findall(decoded)
    return max(matches, key=len).strip() if matches else ""


async def _default_send_applescript(handle: str, text: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        _APPLESCRIPT_SEND,
        "--",
        handle,
        text,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await asyncio.wait_for(proc.communicate(), timeout=20.0)
    if proc.returncode != 0:
        raise RuntimeError(f"osascript send failed (rc={proc.returncode}): {err.decode('utf-8', errors='replace').strip()}")
    return True


class IMessageNotifier:
    """macOS iMessage entrance using safe argv AppleScript and read-only chat.db polling."""

    name = "imessage"

    @staticmethod
    def _handle_variants(handle: str) -> set[str]:
        raw = (handle or "").strip()
        if not raw:
            return set()
        variants = {raw.lower()}
        if "@" in raw:
            return variants
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            variants.add(digits)
            variants.add(f"+{digits}")
            if len(digits) == 13 and digits.startswith("86"):
                variants.add(digits[2:])
                variants.add(f"+{digits[2:]}")
        return variants

    @classmethod
    def _build_allowed_handles(cls, config: IMessageConfig) -> set[str]:
        allowed: set[str] = set()
        for item in [config.owner_handle, *getattr(config, "allowed_handles", [])]:
            allowed.update(cls._handle_variants(item))
        return allowed

    def _row_matches_allowed(self, handle_id: str | None, chat_identifier: str | None) -> bool:
        if not self._allowed_handles:
            return False
        candidates = self._handle_variants(handle_id or "") | self._handle_variants(chat_identifier or "")
        return bool(candidates & self._allowed_handles)

    def __init__(
        self,
        config: IMessageConfig,
        *,
        send_fn: Callable[[str, str], Awaitable[bool] | bool] | None = None,
    ) -> None:
        self._config = config
        self._handle = config.owner_handle.strip()
        self._allowed_handles = self._build_allowed_handles(config)
        self._db_path = Path(config.chat_db_path).expanduser()
        self._send_fn = send_fn or _default_send_applescript
        self._last_rowid: int | None = None
        self._recent_sent: list[tuple[str, float]] = []
        self._dry_seq: int = 0

    def _record_sent(self, text: str) -> None:
        now = time.monotonic()
        self._recent_sent = [(t, exp) for (t, exp) in self._recent_sent if exp > now]
        clean = (text or "").strip()
        if clean:
            self._recent_sent.append((clean, now + 90.0))

    def _consume_echo(self, text: str) -> bool:
        now = time.monotonic()
        clean = (text or "").strip()
        kept: list[tuple[str, float]] = []
        matched = False
        for t, exp in self._recent_sent:
            if exp <= now:
                continue
            if not matched and t == clean:
                matched = True
                continue
            kept.append((t, exp))
        self._recent_sent = kept
        return matched

    def _open_chat_db_ro(self) -> sqlite3.Connection | None:
        if not self._db_path.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def _message_columns(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("PRAGMA table_info(message)").fetchall()
        return {str(r["name"]) for r in rows}

    def _lookup_recent_outbound_guid(self, text: str, *, after_rowid: int) -> str:
        conn = self._open_chat_db_ro()
        if conn is None or not self._handle:
            return ""
        try:
            cols = self._message_columns(conn)
            has_attr = "attributedBody" in cols
            attr_expr = "m.attributedBody" if has_attr else "NULL AS attributedBody"
            rows = conn.execute(
                f"""
                SELECT m.ROWID, m.guid, m.text, {attr_expr}
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE m.ROWID > ? AND m.is_from_me = 1
                  AND (LOWER(COALESCE(h.id, '')) = LOWER(?) OR LOWER(COALESCE(c.chat_identifier, '')) = LOWER(?))
                ORDER BY m.ROWID DESC
                LIMIT 10
                """,
                (after_rowid, self._handle, self._handle),
            ).fetchall()
            target = (text or "").strip()
            for r in rows:
                body = (r["text"] or "").strip() or extract_attributed_body_text(r["attributedBody"])
                if body == target and r["guid"]:
                    return str(r["guid"])
            if rows and rows[0]["guid"]:
                return str(rows[0]["guid"])
            return ""
        except sqlite3.Error:
            return ""
        finally:
            conn.close()

    def _current_max_rowid(self) -> int:
        conn = self._open_chat_db_ro()
        if conn is None:
            return 0
        try:
            row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) AS max_id FROM message").fetchone()
            return int(row["max_id"]) if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    async def send_message(self, text: str) -> str:
        clean = (text or "").strip()
        if not clean or not self._handle:
            return ""
        before_max = self._current_max_rowid()
        self._record_sent(clean)
        if self._config.dry_run:
            self._dry_seq += 1
            return f"imsg-dry-{self._dry_seq}"
        res = self._send_fn(self._handle, clean)
        if inspect.isawaitable(res):
            await res
        return self._lookup_recent_outbound_guid(clean, after_rowid=before_max)

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
    ) -> list[str]:
        del thread
        tok = await self.send_message(format_start_notice(channel.name, preview, is_steer=is_steer))
        return [tok] if tok else []

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        del channel, thread
        tokens: list[str] = []
        for bubble in bubbles:
            tok = await self.send_message(bubble)
            if tok:
                tokens.append(tok)
        return tokens

    @staticmethod
    def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(r[1]) for r in rows}

    def _fetch_message_attachments(
        self,
        conn: sqlite3.Connection,
        message_rowid: int,
        *,
        is_audio_message: bool = False,
        att_cols: set[str] | None = None,
    ) -> list[dict[str, str]]:
        if not att_cols or "filename" not in att_cols:
            return []
        mime_expr = "a.mime_type" if "mime_type" in att_cols else "'' AS mime_type"
        uti_expr = "a.uti" if "uti" in att_cols else "'' AS uti"
        name_expr = "a.transfer_name" if "transfer_name" in att_cols else "'' AS transfer_name"
        sticker_expr = "a.is_sticker" if "is_sticker" in att_cols else "0 AS is_sticker"
        rows = conn.execute(
            f"""
            SELECT a.filename, {mime_expr}, {uti_expr}, {name_expr}, {sticker_expr}
            FROM message_attachment_join maj
            JOIN attachment a ON a.ROWID = maj.attachment_id
            WHERE maj.message_id = ?
            ORDER BY a.ROWID ASC
            """,
            (message_rowid,),
        ).fetchall()
        out: list[dict[str, str]] = []
        audio_exts = {".caf", ".m4a", ".aac", ".amr", ".wav", ".mp3", ".ogg"}
        for r in rows:
            if int(r["is_sticker"] or 0) != 0:
                continue
            raw_path = str(r["filename"] or "").strip()
            if not raw_path:
                continue
            resolved = str(Path(raw_path).expanduser()) if raw_path.startswith("~") else raw_path
            mime_type = str(r["mime_type"] or "").strip()
            uti = str(r["uti"] or "").strip()
            name = str(r["transfer_name"] or "").strip() or Path(resolved).name
            is_audio = (
                is_audio_message
                or mime_type.startswith("audio/")
                or "audio" in uti.lower()
                or Path(resolved).suffix.lower() in audio_exts
            )
            out.append(
                {
                    "path": resolved,
                    "mime_type": mime_type,
                    "uti": uti,
                    "name": name,
                    "is_audio": "true" if is_audio else "false",
                }
            )
        return out

    async def poll_owner_actions(self) -> list[OwnerAction]:
        if not self._allowed_handles:
            return []
        conn = self._open_chat_db_ro()
        if conn is None:
            return []
        try:
            if self._last_rowid is None:
                row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) AS max_id FROM message").fetchone()
                self._last_rowid = int(row["max_id"]) if row else 0
                return []

            cols = self._message_columns(conn)
            has_attr = "attributedBody" in cols
            has_thread_guid = "thread_originator_guid" in cols
            has_reply_to = "reply_to_guid" in cols
            has_assoc = "associated_message_type" in cols
            has_item_type = "item_type" in cols
            has_retracted = "date_retracted" in cols
            has_is_audio = "is_audio_message" in cols

            attr_expr = "m.attributedBody" if has_attr else "NULL AS attributedBody"
            thread_expr = "m.thread_originator_guid" if has_thread_guid else "'' AS thread_originator_guid"
            reply_expr = "m.reply_to_guid" if has_reply_to else "'' AS reply_to_guid"
            audio_expr = "m.is_audio_message" if has_is_audio else "0 AS is_audio_message"

            filters = ["m.ROWID > ?"]
            if has_assoc:
                filters.append("COALESCE(m.associated_message_type, 0) = 0")
            if has_item_type:
                filters.append("COALESCE(m.item_type, 0) = 0")
            if has_retracted:
                filters.append("COALESCE(m.date_retracted, 0) = 0")
            where_sql = " AND ".join(filters)

            has_attachments = (
                self._has_table(conn, "message_attachment_join")
                and self._has_table(conn, "attachment")
            )
            att_cols = self._table_columns(conn, "attachment") if has_attachments else set()

            rows = conn.execute(
                f"""
                SELECT m.ROWID, m.text, m.is_from_me, {attr_expr}, {thread_expr}, {reply_expr},
                       {audio_expr}, h.id AS handle_id, c.chat_identifier AS chat_identifier
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {where_sql}
                ORDER BY m.ROWID ASC
                """,
                (self._last_rowid,),
            ).fetchall()

            actions: list[OwnerAction] = []
            max_seen = self._last_rowid
            seen_rowids: set[int] = set()
            for r in rows:
                rowid = int(r["ROWID"])
                if rowid > max_seen:
                    max_seen = rowid
                if rowid in seen_rowids:
                    continue
                if not self._row_matches_allowed(r["handle_id"], r["chat_identifier"]):
                    continue
                seen_rowids.add(rowid)

                raw_body = (r["text"] or "").strip() or extract_attributed_body_text(r["attributedBody"])
                body = raw_body.replace("\ufffc", "").strip()
                attachments = (
                    self._fetch_message_attachments(
                        conn,
                        rowid,
                        is_audio_message=bool(r["is_audio_message"]),
                        att_cols=att_cols,
                    )
                    if has_attachments
                    else []
                )
                if not body and not attachments:
                    continue
                if body and not attachments and bool(r["is_from_me"]) and self._consume_echo(body):
                    continue
                reply_token = (
                    str(r["thread_originator_guid"] or "").strip()
                    or str(r["reply_to_guid"] or "").strip()
                )
                action = classify_owner_command(
                    body,
                    notifier=self.name,
                    reply_token=reply_token,
                    attachments=attachments,
                )
                if action is not None:
                    actions.append(action)
            self._last_rowid = max_seen
            return actions
        except sqlite3.Error:
            return []
        finally:
            conn.close()
