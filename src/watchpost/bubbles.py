"""Markdown cleaner, bilingual section extractor, and CJK/EN short bubble splitter."""

from __future__ import annotations

import re

DEFAULT_MAX_BUBBLES = 5
DEFAULT_SOFT_BUBBLE_LEN = 80
DEFAULT_MAX_BUBBLE_LEN = 500

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+")
_CJK_OR_PUNCT_END_RE = re.compile(r"[\u4e00-\u9fff。！？，、；：“”‘’（）【】《》]$")
_CJK_OR_PUNCT_START_RE = re.compile(r"^[\u4e00-\u9fff“”‘’（【《]")
_LEADING_BULLET_RE = re.compile(r"^\s*[-•]\s+")

_SECTION_RE = re.compile(
    r"(?im)^(?:#{2,3}\s+|)(Summary|Suggested Reply|Needs Your Attention|Needs Attention|总结|建议回复|需关注|需要注意)\s*[:：]?\s*$"
)
_CODE_FENCE_RE = re.compile(r"(?m)^```[^\n]*\n?")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
_HR_RE = re.compile(r"(?m)^(?:-{3,}|\*{3,}|_{3,})\s*$")
_STAR_BULLET_RE = re.compile(r"(?m)^(\s*)\*\s+")
_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file://[^)]+\)")
_HTTP_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_OTHER_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_STAR_RE = re.compile(r"\*\*([^\n*]+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^\n_]+?)__")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^\s*\n][^*\n]*?[^\s*\n]|[^\s*\n])\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^\n~]+?)~~")
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """Convert Markdown-formatted LLM text into crisp plain text suitable for phone bubbles."""
    if not text:
        return ""
    out = text.replace("\r\n", "\n")
    out = _CODE_FENCE_RE.sub("", out)
    out = _FILE_LINK_RE.sub(r"\1", out)

    def _http_repl(m: re.Match[str]) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        return url if label == url else f"{label} ({url})"

    out = _HTTP_LINK_RE.sub(_http_repl, out)
    out = _OTHER_LINK_RE.sub(r"\1", out)
    out = _HEADING_RE.sub("", out)
    out = _HR_RE.sub("", out)
    out = _STAR_BULLET_RE.sub(r"\1- ", out)
    out = _INLINE_CODE_RE.sub(r"\1", out)
    out = _BOLD_STAR_RE.sub(r"\1", out)
    out = _BOLD_UNDER_RE.sub(r"\1", out)
    out = _ITALIC_STAR_RE.sub(r"\1", out)
    out = _STRIKE_RE.sub(r"\1", out)
    out = _EXCESS_NEWLINES_RE.sub("\n\n", out)
    return out.strip()


def extract_sections(text: str) -> dict[str, str]:
    """Extract summary, suggested_reply, and attention sections (ASCII or full-width colon)."""
    matches = list(_SECTION_RE.finditer(text or ""))
    sections: dict[str, str] = {}
    for idx, m in enumerate(matches):
        heading = m.group(1).strip().lower()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = strip_markdown(text[start:end])
        if "summary" in heading or "总结" in heading:
            sections.setdefault("summary", body)
        elif "suggested reply" in heading or "建议回复" in heading:
            sections.setdefault("suggested_reply", body)
        elif "attention" in heading or "需关注" in heading or "需要注意" in heading:
            sections.setdefault("attention", body)
    return sections


def _clean_block_bullets(block: str) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    cleaned = [_LEADING_BULLET_RE.sub("", line).strip() for line in lines]
    return "\n".join(line for line in cleaned if line).strip()


def _truncate_bubble(text: str, max_len: int) -> str:
    if max_len <= 0 or len(text) <= max_len:
        return text
    limit = max(10, max_len - 3)
    head = text[:limit]
    last_space = head.rfind(" ")
    if last_space >= int(limit * 0.6):
        head = head[:last_space]
    return head.rstrip() + "..."


def split_into_bubbles(
    text: str,
    *,
    max_bubbles: int = DEFAULT_MAX_BUBBLES,
    soft_bubble_len: int = DEFAULT_SOFT_BUBBLE_LEN,
    max_bubble_len: int = DEFAULT_MAX_BUBBLE_LEN,
) -> list[str]:
    """Split plain text into short phone-sized sentence/paragraph bubbles."""
    clean = strip_markdown(text)
    if not clean:
        return []
    max_b = max(1, int(max_bubbles))

    raw_blocks = [b.strip() for b in re.split(r"\n{2,}", clean) if b.strip()]
    blocks = [_clean_block_bullets(b) for b in raw_blocks]
    blocks = [b for b in blocks if b]
    if not blocks:
        return []

    chunks: list[str] = []
    for block in blocks:
        if len(block) <= soft_bubble_len:
            chunks.append(block)
            continue
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(block) if s.strip()]
        if len(sentences) <= 1 and "\n" in block:
            sentences = [line.strip() for line in block.splitlines() if line.strip()]
        cur = ""
        for sent in sentences:
            if not cur:
                cur = sent
                continue
            no_space = bool(_CJK_OR_PUNCT_END_RE.search(cur) or _CJK_OR_PUNCT_START_RE.match(sent))
            sep = "" if no_space else " "
            candidate = f"{cur}{sep}{sent}"
            if len(candidate) <= soft_bubble_len:
                cur = candidate
            else:
                chunks.append(cur)
                cur = sent
        if cur:
            chunks.append(cur)

    if len(chunks) > max_b:
        head = chunks[: max_b - 1]
        tail = "\n\n".join(chunks[max_b - 1 :]).strip()
        chunks = [*head, tail] if tail else head

    return [_truncate_bubble(c, max_bubble_len) for c in chunks if c.strip()]


def format_completion_bubbles(
    channel_name: str,
    response_text: str,
    *,
    error: str = "",
    language: str = "zh",
    max_bubbles: int = DEFAULT_MAX_BUBBLES,
) -> tuple[list[str], str]:
    """Format agent output into short delivery bubbles plus the pure standalone reply draft."""
    is_zh = language.lower().startswith("zh")
    sep = "：" if is_zh else ": "
    err_tag = f"{channel_name}（出错）：" if is_zh else f"{channel_name} (error): "
    err_prefix = "处理出错：" if is_zh else "Error: "
    empty_msg = "已完成，暂无输出。" if is_zh else "Done, no output returned."
    lead_in = "下面这条可直接复制回复：" if is_zh else "Suggested reply to copy:"

    def _prefix(bubble: str) -> str:
        if bubble.startswith(channel_name):
            return bubble
        return f"{err_tag if error else f'{channel_name}{sep}'}{bubble}"

    sections = extract_sections(response_text)
    reply = sections.get("suggested_reply", "").strip()
    summary = sections.get("summary", "").strip()
    attention = sections.get("attention", "").strip()

    if not (summary or reply or attention):
        body = strip_markdown(response_text)
        if error and body:
            body = f"{err_prefix}{error.strip()[:180]}\n\n{body}"
        elif error:
            body = f"{err_prefix}{error.strip()[:180]}"
        elif not body:
            body = empty_msg
        bubbles = split_into_bubbles(body, max_bubbles=min(4, max(1, max_bubbles)))
        if bubbles:
            bubbles[0] = _prefix(bubbles[0])
        return bubbles or [_prefix(empty_msg)], ""

    eng_max = max(1, max_bubbles - (1 if reply else 0))
    eng_bubbles: list[str] = []
    if summary:
        eng_bubbles.extend(split_into_bubbles(summary, max_bubbles=min(3, eng_max)))
    if error:
        err_line = f"{err_prefix}{error.strip()[:180]}"
        eng_bubbles[0] = f"{err_line}\n\n{eng_bubbles[0]}" if eng_bubbles else err_line
    if attention and attention.lower() not in {"none", "n/a", "-", "无", "暂无", "无需处理", "不需要"}:
        eng_bubbles.extend(split_into_bubbles(attention, max_bubbles=1))

    if len(eng_bubbles) > eng_max:
        eng_bubbles = [*eng_bubbles[: eng_max - 1], "\n\n".join(eng_bubbles[eng_max - 1 :]).strip()]

    if reply:
        if eng_bubbles:
            eng_bubbles[0] = _prefix(eng_bubbles[0])
            eng_bubbles[-1] = f"{eng_bubbles[-1]}\n\n{lead_in}"
        else:
            eng_bubbles = [_prefix(lead_in)]
        return [b for b in [*eng_bubbles, reply] if b.strip()], reply

    if eng_bubbles:
        eng_bubbles[0] = _prefix(eng_bubbles[0])
    return [b for b in eng_bubbles if b.strip()] or [_prefix(empty_msg)], ""
