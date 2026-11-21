"""2-Bucket Group Context Engine and session splitting rules."""

from __future__ import annotations

from typing import Awaitable, Callable

from watchpost.models import GroupContext, NormalizedMessage, ThreadRecord

_LINK_PRIORITY = {
    "reply_to": 0,
    "same_sender_burst": 1,
    "handoff_mention": 2,
}


async def assemble_group_context(
    batch: list[NormalizedMessage],
    recent_messages: list[NormalizedMessage],
    *,
    fetch_by_ids: Callable[[list[str]], Awaitable[list[NormalizedMessage]]] | None = None,
    same_sender_window_s: int = 300,
    handoff_window_s: int = 43200,
    background_window_s: int = 64800,
    background_limit: int = 20,
    max_direct_linked: int = 10,
) -> GroupContext:
    """Separate Bucket 1 (direct reply chains, bursts, A @B -> B @owner handoffs) from Bucket 2 background."""
    by_id: dict[str, NormalizedMessage] = {}
    for m in [*recent_messages, *batch]:
        if m.message_id:
            by_id[m.message_id] = m

    batch_ids = {m.message_id for m in batch if m.message_id}
    trigger_senders = {m.sender_id for m in batch if m.sender_id}
    batch_times = [m.create_ms for m in batch if m.create_ms > 0]
    batch_min_ms = min(batch_times, default=0)
    batch_max_ms = max(batch_times, default=0)

    async def _fetch_into_lookup(missing_ids: list[str]) -> None:
        if not missing_ids or fetch_by_ids is None:
            return
        try:
            fetched = await fetch_by_ids(missing_ids)
        except Exception:
            return
        for item in fetched:
            if item.deleted or item.msg_type == "system":
                continue
            if item.message_id and item.message_id not in by_id:
                by_id[item.message_id] = item

    async def _walk_reply_chain(seeds: list[str], max_depth: int) -> list[str]:
        chain_ids: list[str] = []
        visited: set[str] = set()
        frontier: list[str] = []
        for s in seeds:
            val = s.strip()
            if val and val not in visited:
                visited.add(val)
                frontier.append(val)
        for _ in range(max_depth):
            if not frontier:
                break
            chain_ids.extend(frontier)
            missing = [mid for mid in frontier if mid not in by_id]
            if missing:
                await _fetch_into_lookup(missing)
            next_frontier: list[str] = []
            for mid in frontier:
                parent_msg = by_id.get(mid)
                if parent_msg and parent_msg.reply_to_id and parent_msg.reply_to_id not in visited:
                    visited.add(parent_msg.reply_to_id)
                    next_frontier.append(parent_msg.reply_to_id)
            frontier = next_frontier
        return chain_ids

    candidates: dict[str, tuple[str, NormalizedMessage]] = {}

    def _add(msg: NormalizedMessage, reason: str) -> None:
        if not msg.message_id or msg.message_id in batch_ids or msg.deleted or msg.msg_type == "system":
            return
        prev = candidates.get(msg.message_id)
        if prev is None or _LINK_PRIORITY[reason] < _LINK_PRIORITY[prev[0]]:
            candidates[msg.message_id] = (reason, msg)

    # Link 1: recursive reply_to chain (depth up to 6)
    seeds = [m.reply_to_id for m in batch if m.reply_to_id]
    reply_chain_ids = await _walk_reply_chain(seeds, max_depth=6)
    for mid in reply_chain_ids:
        if mid in by_id:
            _add(by_id[mid], "reply_to")

    # Link 2: same-sender burst within +/- same_sender_window_s
    same_ms = same_sender_window_s * 1000
    for m in recent_messages:
        if m.sender_id not in trigger_senders:
            continue
        if batch_times and m.create_ms > 0:
            if not ((batch_min_ms - same_ms) <= m.create_ms <= (batch_max_ms + same_ms)):
                continue
        _add(m, "same_sender_burst")

    # Link 3: handoff mentions (A @B + burst/quote -> B @owner within handoff_window_s)
    handoff_ms = handoff_window_s * 1000
    handoffs: list[NormalizedMessage] = []
    for m in recent_messages:
        if not m.message_id or m.message_id in batch_ids or m.deleted or m.msg_type == "system":
            continue
        if m.sender_id in trigger_senders:
            continue
        if batch_min_ms > 0 and m.create_ms > 0:
            if not ((batch_min_ms - handoff_ms) <= m.create_ms <= batch_max_ms):
                continue
        if any(mt.id in trigger_senders for mt in m.mentions):
            handoffs.append(m)

    handoffs.sort(key=lambda m: (m.position, m.create_ms))
    for h in handoffs[-4:]:
        _add(h, "handoff_mention")
        if h.sender_id:
            for cand in recent_messages:
                if cand.sender_id != h.sender_id:
                    continue
                if h.create_ms > 0 and cand.create_ms > 0:
                    if not ((h.create_ms - same_ms) <= cand.create_ms <= (h.create_ms + same_ms)):
                        continue
                _add(cand, "handoff_mention")
        if h.reply_to_id:
            for mid in await _walk_reply_chain([h.reply_to_id], max_depth=2):
                if mid in by_id:
                    _add(by_id[mid], "handoff_mention")

    kept = list(candidates.items())
    if len(kept) > max_direct_linked:
        kept.sort(key=lambda kv: (_LINK_PRIORITY[kv[1][0]], -kv[1][1].create_ms, -kv[1][1].position))
        kept = kept[:max_direct_linked]

    kept.sort(key=lambda kv: (kv[1][1].position, kv[1][1].create_ms))
    direct_messages = [msg for _mid, (_reason, msg) in kept]
    link_reasons = {mid: reason for mid, (reason, _msg) in kept}
    direct_ids = {mid for mid, _ in kept}

    bg_ms = background_window_s * 1000
    bg_by_id: dict[str, NormalizedMessage] = {}
    for m in recent_messages:
        if not m.message_id or m.message_id in batch_ids or m.message_id in direct_ids:
            continue
        if m.deleted or m.msg_type == "system":
            continue
        if batch_max_ms > 0 and m.create_ms > 0:
            if not ((batch_max_ms - bg_ms) <= m.create_ms <= batch_max_ms):
                continue
        bg_by_id[m.message_id] = m

    background_messages = sorted(bg_by_id.values(), key=lambda m: (m.position, m.create_ms))[-background_limit:]
    return GroupContext(
        direct_messages=direct_messages,
        background_messages=background_messages,
        reply_chain_ids=reply_chain_ids,
        link_reasons=link_reasons,
        all_lookup=by_id,
    )


def match_group_thread(
    threads: list[ThreadRecord],
    batch: list[NormalizedMessage],
    reply_chain_ids: list[str],
    now_ts: float,
    *,
    sender_ttl_s: float = 900.0,
    reply_ttl_s: float = 86400.0,
) -> ThreadRecord | None:
    """Match an inbound group mention batch to an active reply chain or recent sender thread."""
    if not threads:
        return None

    reply_targets = {mid for mid in reply_chain_ids if mid}
    for m in batch:
        if m.reply_to_id:
            reply_targets.add(m.reply_to_id)

    if reply_targets:
        reply_matches = [
            th
            for th in threads
            if (now_ts - th.updated_at) <= reply_ttl_s and (reply_targets & set(th.message_ids))
        ]
        if reply_matches:
            reply_matches.sort(key=lambda t: t.updated_at, reverse=True)
            return reply_matches[0]

    sender_id = batch[0].sender_id if batch else ""
    if sender_id:
        sender_matches = [
            th
            for th in threads
            if (now_ts - th.updated_at) <= sender_ttl_s and th.sender_id == sender_id
        ]
        if sender_matches:
            sender_matches.sort(key=lambda t: t.updated_at, reverse=True)
            return sender_matches[0]
    return None
