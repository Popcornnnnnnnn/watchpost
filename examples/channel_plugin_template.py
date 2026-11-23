#!/usr/bin/env python3
"""Runnable Watchpost ChannelPlugin example backed by a local JSONL inbox (or built-in demo data)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from watchpost.sdk import ChannelPlugin, make_chat, make_message, run_channel_plugin


class JsonlChannelPlugin(ChannelPlugin):
    """Minimal external channel bridge reading normalized JSON lines from `WATCHPOST_EXAMPLE_INBOX`."""

    name = "jsonl_example"

    def _inbox_path(self) -> Path | None:
        raw = os.environ.get("WATCHPOST_EXAMPLE_INBOX", "").strip()
        return Path(raw).expanduser() if raw else None

    def list_chats(self) -> list[dict[str, str]]:
        inbox = self._inbox_path()
        if inbox and inbox.is_file():
            seen: dict[str, dict[str, str]] = {}
            for line in inbox.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                cid = str(row.get("chat_id") or "")
                if cid and cid not in seen:
                    seen[cid] = make_chat(
                        cid,
                        str(row.get("chat_name") or cid),
                        chat_type=str(row.get("chat_type") or "p2p"),
                    )
            if seen:
                return list(seen.values())
        return [
            make_chat("demo_support_dm", "Teammate (Example)", chat_type="p2p"),
        ]

    def fetch_recent(self, channel: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
        target_chat = str(channel.get("external_chat_id") or "")
        inbox = self._inbox_path()
        if inbox and inbox.is_file():
            out: list[dict[str, Any]] = []
            for idx, line in enumerate(
                inbox.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                row = json.loads(line)
                if target_chat and str(row.get("chat_id") or "") != target_chat:
                    continue
                out.append(
                    make_message(
                        message_id=str(row.get("message_id") or f"msg_{idx}"),
                        sender_id=str(row.get("sender_id") or "user_1"),
                        sender_name=str(row.get("sender_name") or "Teammate"),
                        clean_text=str(row.get("text") or ""),
                        position=int(row.get("position") or idx),
                        create_ms=int(row.get("create_ms") or 0),
                        reply_to_id=str(row.get("reply_to_id") or ""),
                        mentions_owner=bool(row.get("mentions_owner", False)),
                    )
                )
            return out[-max(1, int(limit)) :]

        return [
            make_message(
                message_id="demo_1",
                sender_id="peer_demo",
                sender_name="Teammate (Example)",
                clean_text="Could you check why worker health returned 503 after deploy?",
                position=1,
                create_ms=int(time.time() * 1000),
            )
        ]


if __name__ == "__main__":
    raise SystemExit(run_channel_plugin(JsonlChannelPlugin()))
