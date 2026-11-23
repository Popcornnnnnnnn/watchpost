"""Python Plugin SDK for building out-of-process Watchpost channel adapters and notifiers."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from watchpost.models import MediaRef, Mention, NormalizedMessage
from watchpost.notifiers import classify_owner_command, format_start_bubbles

SDK_PROTOCOL_VERSION = "1.0"


def make_chat(
    external_chat_id: str,
    name: str,
    *,
    chat_type: str = "p2p",
) -> dict[str, str]:
    """Construct a Watchpost chat discovery record for `list-chats`."""
    ct = chat_type if chat_type in {"p2p", "group"} else "p2p"
    return {
        "external_chat_id": str(external_chat_id),
        "name": str(name or external_chat_id),
        "chat_type": ct,
    }


def make_message(
    *,
    message_id: str,
    sender_id: str = "",
    sender_name: str = "Unknown",
    clean_text: str = "",
    channel_id: str = "",
    create_ms: int = 0,
    position: int = 0,
    create_time: str = "",
    msg_type: str = "text",
    is_owner: bool = False,
    is_bot: bool = False,
    deleted: bool = False,
    reply_to_id: str = "",
    reply_preview: str = "",
    mentions: list[dict[str, str]] | None = None,
    mentions_owner: bool = False,
    mentions_all: bool = False,
    media_refs: list[dict[str, Any]] | None = None,
    raw_content: str = "",
) -> dict[str, Any]:
    """Build and validate a `NormalizedMessage` payload for external channel plugins."""
    msg = NormalizedMessage(
        message_id=str(message_id),
        channel_id=str(channel_id),
        position=int(position),
        create_ms=int(create_ms),
        create_time=str(create_time),
        sender_id=str(sender_id),
        sender_name=str(sender_name or "Unknown"),
        is_owner=bool(is_owner),
        is_bot=bool(is_bot),
        deleted=bool(deleted),
        msg_type=str(msg_type or "text"),
        raw_content=str(raw_content),
        clean_text=str(clean_text),
        reply_to_id=str(reply_to_id),
        reply_preview=str(reply_preview),
        mentions=[Mention(**m) for m in (mentions or [])],
        mentions_owner=bool(mentions_owner),
        mentions_all=bool(mentions_all),
        media_refs=[MediaRef(**r) for r in (media_refs or [])],
    )
    return msg.model_dump(mode="json")


def make_owner_action(
    text: str,
    *,
    notifier: str,
    reply_token: str = "",
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Classify raw owner text/media into a validated Watchpost `OwnerAction` dict."""
    act = classify_owner_command(
        text,
        notifier=notifier,
        reply_token=reply_token,
        attachments=attachments,
    )
    return act.model_dump(mode="json") if act is not None else None


def _resolve_maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _read_stdin_json(stdin_io: TextIO) -> dict[str, Any]:
    raw = stdin_io.read().strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Plugin stdin JSON must be an object")
    return data


def _write_stdout_json(stdout_io: TextIO, payload: Any) -> None:
    stdout_io.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout_io.flush()


class ChannelPlugin:
    """Base class for out-of-process Watchpost channel plugins (`external_adapters`)."""

    name: str = "custom"

    def health(self) -> dict[str, Any]:
        return {"ok": True, "name": self.name, "sdk_version": SDK_PROTOCOL_VERSION}

    def list_chats(self) -> list[dict[str, str]]:
        raise NotImplementedError("ChannelPlugin subclasses must implement list_chats()")

    def fetch_recent(self, channel: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError("ChannelPlugin subclasses must implement fetch_recent()")

    def fetch_by_ids(self, channel: dict[str, Any], message_ids: list[str]) -> list[dict[str, Any]]:
        del channel, message_ids
        return []

    def download_media(self, channel: dict[str, Any], message: dict[str, Any], dest_dir: Path) -> list[dict[str, Any]]:
        del channel, message, dest_dir
        return []


class NotifierPlugin:
    """Base class for out-of-process Watchpost owner notifier plugins (`external_notifiers`)."""

    name: str = "custom_notifier"

    def health(self) -> dict[str, Any]:
        return {"ok": True, "name": self.name, "sdk_version": SDK_PROTOCOL_VERSION}

    def send_message(self, text: str) -> str:
        raise NotImplementedError("NotifierPlugin subclasses must implement send_message()")

    def notify_start(
        self,
        channel: dict[str, Any],
        thread: dict[str, Any],
        preview: str,
        *,
        is_steer: bool = False,
        attachments: list[str] | None = None,
    ) -> list[str]:
        ch_name = str(channel.get("name") or channel.get("id") or "")
        sender_name = str(thread.get("sender_name") or "")
        if channel.get("chat_type") == "group" and sender_name and sender_name != "Owner":
            label = f"{ch_name} · {sender_name}"
        else:
            label = ch_name
        bubbles = format_start_bubbles(
            label,
            preview,
            adapter=str(channel.get("adapter") or ""),
            is_steer=is_steer,
            has_images=bool(attachments),
        )
        tokens: list[str] = []
        for bubble in bubbles:
            tok = _resolve_maybe_await(self.send_message(bubble))
            if tok:
                tokens.append(str(tok))
        return tokens

    def notify_completion(
        self,
        channel: dict[str, Any],
        thread: dict[str, Any],
        bubbles: list[str],
    ) -> list[str]:
        del channel, thread
        tokens: list[str] = []
        for bubble in bubbles:
            tok = _resolve_maybe_await(self.send_message(bubble))
            if tok:
                tokens.append(str(tok))
        return tokens

    def poll_actions(self) -> list[dict[str, Any]]:
        return []


def run_channel_plugin(
    plugin: ChannelPlugin,
    *,
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the standard Watchpost JSON-over-stdio dispatch loop for a `ChannelPlugin`."""
    args = list(sys.argv[1:] if argv is None else argv)
    in_io = stdin if stdin is not None else sys.stdin
    out_io = stdout if stdout is not None else sys.stdout
    err_io = stderr if stderr is not None else sys.stderr
    if not args:
        err_io.write("Missing channel plugin action argument\n")
        return 2
    action = args[0].strip()

    try:
        payload = _read_stdin_json(in_io)
        if action == "health":
            res = _resolve_maybe_await(plugin.health())
        elif action == "list-chats":
            chats = _resolve_maybe_await(plugin.list_chats())
            res = {"chats": list(chats or [])}
        elif action == "fetch-recent":
            ch = dict(payload.get("channel") or {})
            limit = int(payload.get("limit") or 50)
            msgs = _resolve_maybe_await(plugin.fetch_recent(ch, limit=limit))
            res = {"messages": list(msgs or [])}
        elif action == "fetch-by-ids":
            ch = dict(payload.get("channel") or {})
            mids = [str(m) for m in (payload.get("message_ids") or [])]
            msgs = _resolve_maybe_await(plugin.fetch_by_ids(ch, mids))
            res = {"messages": list(msgs or [])}
        elif action == "download-media":
            ch = dict(payload.get("channel") or {})
            msg = dict(payload.get("message") or {})
            dest = Path(str(payload.get("dest_dir") or ".")).expanduser()
            arts = _resolve_maybe_await(plugin.download_media(ch, msg, dest))
            res = {"artifacts": list(arts or [])}
        else:
            err_io.write(f"Unknown channel plugin action: {action}\n")
            return 2
        _write_stdout_json(out_io, res)
        return 0
    except Exception as exc:
        err_io.write(f"{exc}\n")
        return 1


def run_notifier_plugin(
    plugin: NotifierPlugin,
    *,
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the standard Watchpost JSON-over-stdio dispatch loop for a `NotifierPlugin`."""
    args = list(sys.argv[1:] if argv is None else argv)
    in_io = stdin if stdin is not None else sys.stdin
    out_io = stdout if stdout is not None else sys.stdout
    err_io = stderr if stderr is not None else sys.stderr
    if not args:
        err_io.write("Missing notifier plugin action argument\n")
        return 2
    action = args[0].strip()

    try:
        payload = _read_stdin_json(in_io)
        if action == "health":
            res = _resolve_maybe_await(plugin.health())
        elif action == "notify-start":
            ch = dict(payload.get("channel") or {})
            th = dict(payload.get("thread") or {})
            preview = str(payload.get("preview") or "")
            is_steer = bool(payload.get("is_steer", False))
            atts = [str(a) for a in (payload.get("attachments") or []) if a]
            toks = _resolve_maybe_await(
                plugin.notify_start(ch, th, preview, is_steer=is_steer, attachments=atts)
            )
            res = {"tokens": [str(t) for t in (toks or []) if t]}
        elif action == "notify-completion":
            ch = dict(payload.get("channel") or {})
            th = dict(payload.get("thread") or {})
            bubbles = [str(b) for b in (payload.get("bubbles") or [])]
            toks = _resolve_maybe_await(plugin.notify_completion(ch, th, bubbles))
            res = {"tokens": [str(t) for t in (toks or []) if t]}
        elif action == "send-message":
            text = str(payload.get("text") or "")
            tok = _resolve_maybe_await(plugin.send_message(text))
            res = {"token": str(tok or "")}
        elif action == "poll-actions":
            acts = _resolve_maybe_await(plugin.poll_actions())
            res = {"actions": [dict(a) for a in (acts or []) if isinstance(a, dict)]}
        else:
            err_io.write(f"Unknown notifier plugin action: {action}\n")
            return 2
        _write_stdout_json(out_io, res)
        return 0
    except Exception as exc:
        err_io.write(f"{exc}\n")
        return 1
