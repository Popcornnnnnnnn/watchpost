"""Mobile/desktop owner NotifierAdapters and shared command classification."""

from __future__ import annotations

import re

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


_PLATFORM_ZH = {
    "feishu": "飞书",
    "wechat": "微信",
    "simulator": "模拟",
    "local_sandbox": "沙盒",
    "owner_direct": "",
}
_PLATFORM_EN = {
    "feishu": "Feishu",
    "wechat": "WeChat",
    "simulator": "Sim",
    "local_sandbox": "Sandbox",
    "owner_direct": "",
}
_PURE_MEDIA_LINE_RE = re.compile(
    r"^(?:\[(?:Image|Voice|Audio|File|Media|Video|Sticker|图片|语音|文件)(?:\s*:[^\]]*)?\])$",
    re.IGNORECASE,
)


def format_platform_label(adapter: str, *, language: str = "zh") -> str:
    """Return a concise human-readable platform badge for phone notifications."""
    key = (adapter or "").strip().lower()
    if language.lower().startswith("zh"):
        return _PLATFORM_ZH.get(key, adapter or "")
    return _PLATFORM_EN.get(key, adapter.title() if adapter else "")


def _clean_quote_preview(preview: str) -> str:
    lines = []
    for raw_line in (preview or "").splitlines():
        line = raw_line.strip()
        if not line or _PURE_MEDIA_LINE_RE.fullmatch(line):
            continue
        lines.append(line)
    return " ".join(lines).strip()


def format_start_bubbles(
    channel_name: str,
    preview: str,
    *,
    adapter: str = "",
    is_steer: bool = False,
    has_images: bool = False,
    language: str = "zh",
) -> list[str]:
    """Return 1–2 concise bubbles: status header plus quoted original message (never `Name: msg`)."""
    is_zh = language.lower().startswith("zh")
    plat = format_platform_label(adapter, language=language)
    tag = f"{plat} · {channel_name}" if plat else channel_name
    quote = _clean_quote_preview(preview)

    if is_zh:
        if is_steer:
            header = f"【{tag}】发来补充消息，继续处理"
        elif has_images and not quote:
            header = f"【{tag}】发来图片，正在查看"
        elif quote:
            header = f"【{tag}】发来新消息，正在查看"
        else:
            header = f"【{tag}】发来新消息，正在查看"
    else:
        if is_steer:
            header = f"[{tag}] Follow-up — checking"
        elif has_images and not quote:
            header = f"[{tag}] Photo received — checking"
        else:
            header = f"[{tag}] New message — checking"

    bubbles = [header]
    if quote:
        clipped = quote[:357].rstrip() + "..." if len(quote) > 360 else quote
        bubbles.append(f'"{clipped}"')
    return bubbles


def format_start_notice(
    channel_name: str,
    preview: str,
    *,
    adapter: str = "",
    is_steer: bool = False,
    has_images: bool = False,
    language: str = "zh",
) -> str:
    """Single-string start notice helper for plain consoles and compact previews."""
    return "\n".join(
        format_start_bubbles(
            channel_name,
            preview,
            adapter=adapter,
            is_steer=is_steer,
            has_images=has_images,
            language=language,
        )
    )


from watchpost.notifiers.console import ConsoleNotifier
from watchpost.notifiers.imessage import IMessageNotifier
from watchpost.notifiers.telegram import TelegramNotifier

__all__ = [
    "ConsoleNotifier",
    "IMessageNotifier",
    "TelegramNotifier",
    "classify_owner_command",
    "format_platform_label",
    "format_start_bubbles",
    "format_start_notice",
]
