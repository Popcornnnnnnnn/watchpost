#!/usr/bin/env python3
"""Reference WhatsApp external ChannelAdapter plugin template via Watchpost SDK.

This clean-room `Apache-2.0` template reads WhatsApp Cloud API / external bridge
JSONL events from `WATCHPOST_WHATSAPP_INBOX` when set, or serves a sample chat
and message so `watchpost plugins check --kind channel --test-fetch` works offline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from watchpost.sdk import ChannelPlugin, make_chat, make_message, run_channel_plugin


class WhatsAppBridgePlugin(ChannelPlugin):
    """Out-of-process WhatsApp channel adapter using Watchpost's JSON stdio SDK."""

    name = "whatsapp"

    def _inbox_path(self) -> Path | None:
        raw = os.environ.get("WATCHPOST_WHATSAPP_INBOX", "").strip()
        return Path(raw).expanduser() if raw else None

    def _read_events(self) -> list[dict[str, Any]]:
        inbox = self._inbox_path()
        if inbox is None or not inbox.is_file():
            return []
        items: list[dict[str, Any]] = []
        for raw_line in inbox.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                items.append(obj)
        return items

    def list_chats(self) -> list[dict[str, str]]:
        events = self._read_events()
        if not events:
            return [
                make_chat("whatsapp_sample_chat", "Ops Team (WhatsApp Sample)", chat_type="group"),
            ]
        seen: dict[str, dict[str, str]] = {}
        for ev in events:
            cid = str(ev.get("chat_id") or ev.get("from") or "").strip()
            if not cid:
                continue
            seen[cid] = make_chat(
                cid,
                str(ev.get("chat_name") or ev.get("sender_name") or cid),
                chat_type=str(ev.get("chat_type") or "group"),
            )
        return list(seen.values())

    def fetch_recent(self, channel: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
        external_chat_id = str(channel.get("external_chat_id") or "").strip()
        events = self._read_events()
        if not events and external_chat_id == "whatsapp_sample_chat":
            return [
                make_message(
                    message_id="wamid_sample_001",
                    sender_id="15550199",
                    sender_name="Alex (WhatsApp)",
                    clean_text="@owner Production queue latency spiked to 850ms — can you check?",
                    mentions_owner=True,
                    create_ms=1700000000000,
                    position=1,
                )
            ]
        out: list[dict[str, Any]] = []
        for idx, ev in enumerate(events, start=1):
            cid = str(ev.get("chat_id") or ev.get("from") or "").strip()
            if cid != external_chat_id:
                continue
            mid = str(ev.get("id") or ev.get("message_id") or "").strip()
            if not mid:
                continue
            out.append(
                make_message(
                    message_id=mid,
                    sender_id=str(ev.get("sender_id") or ev.get("from") or ""),
                    sender_name=str(ev.get("sender_name") or ev.get("sender_id") or "WhatsApp User"),
                    clean_text=str(ev.get("text") or ev.get("body") or ""),
                    create_ms=int(ev.get("timestamp_ms") or ev.get("create_ms") or 0),
                    position=idx,
                    reply_to_id=str(ev.get("reply_to_id") or ""),
                    mentions_owner=bool(ev.get("mentions_owner")),
                )
            )
        return out[-max(1, int(limit)) :]


if __name__ == "__main__":
    raise SystemExit(run_channel_plugin(WhatsAppBridgePlugin()))
