"""Mobile/desktop owner NotifierAdapters and shared command classification."""

from __future__ import annotations

from watchpost.models import OwnerAction

_APPROVE_TOKENS = {"go", "approve", "执行", "改吧", "批准", "同意", "可以改", "继续执行"}
_STATUS_TOKENS = {"/status", "status", "状态"}
_LIST_TOKENS = {"/list", "list", "列表"}
_HELP_TOKENS = {"/help", "help", "帮助"}
_RESET_TOKENS = {"/reset"}


def _parse_indexed_command(clean: str) -> tuple[int, str] | None:
    """Parse leading `#N` or `#N <body>` references from an owner message."""
    if not clean.startswith("#") or len(clean) < 2:
        return None
    idx = 1
    while idx < len(clean) and clean[idx].isdigit():
        idx += 1
    if idx == 1 or (idx < len(clean) and not clean[idx].isspace()):
        return None
    num = int(clean[1:idx])
    if num <= 0:
        return None
    return num, clean[idx:].strip()


def classify_owner_command(
    text: str,
    *,
    notifier: str,
    reply_token: str = "",
    thread_id: str = "",
    attachments: list[dict[str, str]] | None = None,
) -> OwnerAction | None:
    """Normalize a raw message from the owner's phone entrance into an OwnerAction."""
    clean = (text or "").strip()
    att_list = [dict(item) for item in (attachments or [])]
    if not clean and not att_list:
        return None
    if not clean:
        return OwnerAction(
            notifier=notifier,
            kind="steer",
            thread_id=thread_id,
            text="",
            reply_token=reply_token,
            attachments=att_list,
        )

    lower = clean.lower()
    if lower in _APPROVE_TOKENS or clean in _APPROVE_TOKENS:
        kind = "approve_go"
    elif lower in _STATUS_TOKENS or clean in _STATUS_TOKENS:
        kind = "status"
    elif lower in _LIST_TOKENS or clean in _LIST_TOKENS:
        kind = "list"
    elif lower in _HELP_TOKENS or clean in _HELP_TOKENS:
        kind = "help"
    elif lower in _RESET_TOKENS:
        kind = "reset_personal"
    elif lower == "/new" or (lower.startswith("/new") and len(clean) > 4 and clean[4].isspace()):
        return OwnerAction(
            notifier=notifier,
            kind="new_personal",
            thread_id=thread_id,
            text=clean[4:].strip(),
            reply_token=reply_token,
            attachments=att_list,
        )
    else:
        indexed = _parse_indexed_command(clean)
        if indexed is not None:
            list_index, rest = indexed
            rest_lower = rest.lower()
            cmd_kind = (
                "approve_go"
                if rest and (rest_lower in _APPROVE_TOKENS or rest in _APPROVE_TOKENS)
                else "steer"
            )
            return OwnerAction(
                notifier=notifier,
                kind=cmd_kind,
                thread_id=thread_id,
                text=rest,
                reply_token=reply_token,
                list_index=list_index,
                attachments=att_list,
            )
        kind = "steer"

    return OwnerAction(
        notifier=notifier,
        kind=kind,
        thread_id=thread_id,
        text=clean,
        reply_token=reply_token,
        attachments=att_list,
    )


def format_start_notice(channel_name: str, preview: str, *, is_steer: bool = False, language: str = "zh") -> str:
    """Format a brief non-intrusive phone status notice when Watchpost starts/steers a task."""
    snippet = (preview or "").strip().replace("\n", " ")[:120]
    if language.lower().startswith("zh"):
        if is_steer:
            return f"{channel_name} 有新补充消息，继续处理：{snippet}" if snippet else f"{channel_name} 有新补充消息。"
        return f"收到来自 {channel_name} 的新消息：{snippet}，正在查看。"
    if is_steer:
        return f"Follow-up in {channel_name}: {snippet}" if snippet else f"Follow-up in {channel_name}."
    return f"New message in {channel_name}: {snippet} — checking."


from watchpost.notifiers.console import ConsoleNotifier
from watchpost.notifiers.imessage import IMessageNotifier
from watchpost.notifiers.telegram import TelegramNotifier

__all__ = [
    "ConsoleNotifier",
    "IMessageNotifier",
    "TelegramNotifier",
    "classify_owner_command",
    "format_start_notice",
]
