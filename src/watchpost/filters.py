"""Trigger, recalled-message, and small-talk noise suppression filters."""

from __future__ import annotations

import re

from watchpost.models import ChannelConfig, ChatType, NormalizedMessage, TriggerMode

_PUNCT_STRIP_RE = re.compile(r"[\s,.!?，。！？~～、；;:\"']+")
_BRACKET_EMOJI_RE = re.compile(r"\[[^\]]{1,16}\]")

_ACK_PHRASES = {
    "好",
    "好的",
    "好滴",
    "好哒",
    "好嘞",
    "行",
    "行吧",
    "可以",
    "ok",
    "okok",
    "ok了",
    "收到",
    "收到谢谢",
    "谢谢",
    "谢了",
    "多谢",
    "感谢",
    "辛苦",
    "辛苦了",
    "嗯",
    "嗯嗯",
    "嗯好",
    "哦",
    "哦哦",
    "哈哈",
    "哈哈哈",
    "哈哈哈哈",
    "知道了",
    "明白",
    "了解了",
    "早安",
    "晚安",
    "拜拜",
    "没事",
    "没事没事",
    "thanks",
    "thank you",
    "got it",
    "noted",
    "roger",
    "facepalm",
}

_OWNER_QUESTION_HINTS = ("?", "？", "吗", "呢", "几", "哪", "是不是", "对不对", "确定", "好了吗")


def should_trigger(channel: ChannelConfig, msg: NormalizedMessage) -> bool:
    """Return True if a normalized inbound message is eligible to trigger triage."""
    if not channel.enabled:
        return False
    if msg.is_owner or msg.deleted or msg.msg_type == "system":
        return False
    if channel.ignore_bots and (msg.is_bot or msg.sender_type in {"app", "bot"}):
        return False
    if channel.chat_type == ChatType.p2p:
        return True
    if channel.trigger_mode == TriggerMode.mention_owner:
        return bool(msg.mentions_owner)
    return True


def _strip_mention_tokens(text: str, msg: NormalizedMessage) -> str:
    cleaned = text or ""
    for mt in msg.mentions:
        if mt.name:
            cleaned = cleaned.replace(f"@{mt.name}", " ")
        if mt.key:
            cleaned = cleaned.replace(mt.key, " ")
    cleaned = re.sub(r"@[A-Za-z0-9_.-]{1,32}\b", " ", cleaned)
    return cleaned.strip()


def _owner_recently_asked(recent_history: list[NormalizedMessage], batch_first_ms: int) -> bool:
    for prev in reversed(recent_history):
        if not prev.is_owner:
            continue
        if batch_first_ms and prev.create_ms and (batch_first_ms - prev.create_ms) > 600_000:
            return False
        text = prev.clean_text or prev.raw_content
        if any(hint in text for hint in _OWNER_QUESTION_HINTS):
            return True
    return False


def is_small_talk_only(
    messages: list[NormalizedMessage],
    recent_history: list[NormalizedMessage],
    has_active_session: bool,
) -> bool:
    """Return True when a batch is isolated small talk, sticker, emoji reaction, or closing ack."""
    if not messages:
        return True
    if has_active_session:
        return False

    batch_first_ms = min((m.create_ms for m in messages if m.create_ms > 0), default=0)
    if _owner_recently_asked(recent_history, batch_first_ms):
        return False

    for msg in messages:
        if msg.media_refs or msg.artifacts:
            return False
        if msg.msg_type == "sticker":
            continue
        if msg.msg_type not in {"text", "post"}:
            return False
        raw = msg.clean_text.strip()
        text = _strip_mention_tokens(raw, msg)
        if msg.mentions_owner and not _PUNCT_STRIP_RE.sub("", text):
            return False
        if "?" in text or "？" in text:
            return False
        no_emoji = _BRACKET_EMOJI_RE.sub(" ", text).strip()
        normalized = _PUNCT_STRIP_RE.sub("", no_emoji).lower()
        if not normalized and text:
            continue
        if normalized not in _ACK_PHRASES:
            return False
    return True
