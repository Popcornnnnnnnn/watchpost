"""Mobile/desktop owner NotifierAdapters and shared command classification."""

from __future__ import annotations

from watchpost.models import OwnerAction

_APPROVE_TOKENS = {"go", "approve", "执行", "改吧", "批准", "同意", "可以改", "继续执行"}
_STATUS_TOKENS = {"/status", "status", "状态"}
_LIST_TOKENS = {"/list", "list", "列表"}


def classify_owner_command(
    text: str,
    *,
    notifier: str,
    reply_token: str = "",
    thread_id: str = "",
) -> OwnerAction | None:
    """Normalize a raw message from the owner's phone entrance into an OwnerAction."""
    clean = (text or "").strip()
    if not clean:
        return None
    lower = clean.lower()
    if lower in _APPROVE_TOKENS or clean in _APPROVE_TOKENS:
        kind = "approve_go"
    elif lower in _STATUS_TOKENS or clean in _STATUS_TOKENS:
        kind = "status"
    elif lower in _LIST_TOKENS or clean in _LIST_TOKENS:
        kind = "list"
    else:
        kind = "steer"
    return OwnerAction(
        notifier=notifier,
        kind=kind,
        thread_id=thread_id,
        text=clean,
        reply_token=reply_token,
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
