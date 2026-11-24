"""Watchpost orchestration engine connecting channels, filters, context, debouncer, backends, and notifiers."""

from __future__ import annotations

import inspect
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from watchpost.adapters.simulator import SimulatorAdapter, default_demo_channels
from watchpost.bubbles import format_completion_bubbles, strip_markdown
from watchpost.context import assemble_group_context, match_group_thread
from watchpost.debouncer import BurstDebouncer
from watchpost.filters import is_small_talk_only, should_trigger
from watchpost.models import (
    AutonomyMode,
    ChannelConfig,
    ChatType,
    EngineConfig,
    GroupContext,
    NormalizedMessage,
    OwnerAction,
    ThreadEvent,
    ThreadRecord,
    ThreadStatus,
    TriageMode,
    TriggerMode,
)
from watchpost.notifiers import format_platform_label
from watchpost.ports import AgentBackend, ChannelAdapter, MediaProcessor, NotifierAdapter
from watchpost.prompts import (
    build_new_thread_prompt,
    build_owner_steer_prompt,
    build_personal_direct_prompt,
    build_steer_prompt,
)
from watchpost.store import WatchpostStore

logger = logging.getLogger("watchpost.engine")


class WatchpostEngine:
    """Orchestrates Watchpost polling, debouncing, context assembly, agent execution, and notifications."""

    def __init__(
        self,
        store: WatchpostStore,
        backend: AgentBackend,
        *,
        config: EngineConfig | None = None,
        adapters: dict[str, ChannelAdapter] | None = None,
        notifiers: list[NotifierAdapter] | None = None,
        media_processor: MediaProcessor | None = None,
        media_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.config = config or EngineConfig()
        self.adapters: dict[str, ChannelAdapter] = dict(adapters or {})
        self.notifiers: list[NotifierAdapter] = list(notifiers or [])
        self.media_processor = media_processor
        self.media_dir = Path(media_dir) if media_dir else (self.store.db_path.parent / "media")
        self.debouncer: BurstDebouncer[NormalizedMessage] = BurstDebouncer(
            self._on_debounced_batch,
            debounce_s=self.config.debounce_s,
            max_debounce_s=self.config.max_debounce_s,
        )

    def register_adapter(self, adapter: ChannelAdapter) -> None:
        self.adapters[adapter.name] = adapter

    def register_notifier(self, notifier: NotifierAdapter) -> None:
        self.notifiers = [n for n in self.notifiers if n.name != notifier.name]
        self.notifiers.append(notifier)

    def _bind_tokens(self, thread_id: str, pairs: list[tuple[str, str]]) -> None:
        for ntf_name, tok in pairs:
            if tok:
                self.store.bind_outbound(ntf_name, tok, thread_id)

    async def _notify_start_all(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool,
        attachments: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for ntf in self.notifiers:
            try:
                sig = inspect.signature(ntf.notify_start)
                if "attachments" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    tokens = await ntf.notify_start(
                        channel,
                        thread,
                        preview,
                        is_steer=is_steer,
                        attachments=attachments,
                    )
                else:
                    tokens = await ntf.notify_start(channel, thread, preview, is_steer=is_steer)
                pairs.extend((ntf.name, tok) for tok in tokens if tok)
            except Exception as exc:
                logger.warning("Notifier %s notify_start failed: %s", ntf.name, exc)
        return pairs

    _MEDIA_ONLY_TEXT_RE = re.compile(
        r"^(?:\[(?:Image|Voice|Audio|File|Media|Video|Sticker|图片|语音|文件|Recalled)(?:\s*:[^\]]*)?\])$",
        re.IGNORECASE,
    )
    _MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)?")
    _DURATION_RE = re.compile(
        r"^\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|小时|m|min|mins|minute|minutes|分|分钟)?\s*$",
        re.IGNORECASE,
    )

    @staticmethod
    def _parse_hhmm(hhmm: str) -> int | None:
        parts = (hhmm or "").strip().split(":")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            return None
        hh, mm = int(parts[0]), int(parts[1])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm
        return None

    @classmethod
    def _parse_duration_minutes(cls, raw: str, *, default_minutes: int = 120) -> int:
        m = cls._DURATION_RE.fullmatch(raw or "")
        if m is None:
            return default_minutes
        val = float(m.group(1))
        unit = (m.group(2) or "m").lower()
        mins = int(round(val * 60)) if unit.startswith("h") or unit == "小时" else int(round(val))
        return max(1, min(mins, 24 * 60))

    def _next_quiet_end_ts(self, now_dt: datetime) -> float:
        qh = self.config.quiet_hours
        start_min = self._parse_hhmm(qh.start)
        end_min = self._parse_hhmm(qh.end)
        if not qh.enabled or start_min is None or end_min is None or start_min == end_min:
            return 0.0
        end_h, end_m = divmod(end_min, 60)
        today_end = now_dt.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        cur_min = now_dt.hour * 60 + now_dt.minute
        if start_min > end_min:
            end_dt = today_end + timedelta(days=1) if cur_min >= start_min else today_end
        else:
            end_dt = today_end if cur_min < end_min else (today_end + timedelta(days=1))
        return end_dt.timestamp()

    def get_quiet_status(self, *, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts) if now_ts is not None else time.time()
        mute_until = float(self.store.get_kv("quiet_mute_until", 0.0) or 0.0)
        override_off_until = float(self.store.get_kv("quiet_override_off_until", 0.0) or 0.0)
        raw_queue = self.store.get_kv("quiet_digest_queue", [])
        queued_count = len(raw_queue) if isinstance(raw_queue, list) else 0

        qh = self.config.quiet_hours
        start_min = self._parse_hhmm(qh.start)
        end_min = self._parse_hhmm(qh.end)
        in_schedule = False
        if qh.enabled and start_min is not None and end_min is not None and start_min != end_min:
            now_dt = datetime.fromtimestamp(now)
            cur_min = now_dt.hour * 60 + now_dt.minute
            if start_min > end_min:
                in_schedule = cur_min >= start_min or cur_min < end_min
            else:
                in_schedule = start_min <= cur_min < end_min

        if now < mute_until:
            active = True
            reason = "muted"
        elif now < override_off_until:
            active = False
            reason = ""
        elif in_schedule:
            active = True
            reason = "schedule"
        else:
            active = False
            reason = ""

        return {
            "active": active,
            "reason": reason,
            "enabled": bool(qh.enabled),
            "start": qh.start,
            "end": qh.end,
            "muted_until": mute_until,
            "override_off_until": override_off_until,
            "queued_count": queued_count,
        }

    def set_quiet_mute(self, *, minutes: int = 120, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts) if now_ts is not None else time.time()
        mins = max(1, min(int(minutes), 24 * 60))
        self.store.set_kv("quiet_mute_until", now + mins * 60)
        self.store.set_kv("quiet_override_off_until", 0.0)
        return self.get_quiet_status(now_ts=now)

    def clear_quiet_mute(self, *, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts) if now_ts is not None else time.time()
        self.store.set_kv("quiet_mute_until", 0.0)
        qh = self.config.quiet_hours
        start_min = self._parse_hhmm(qh.start)
        end_min = self._parse_hhmm(qh.end)
        override_until = 0.0
        if qh.enabled and start_min is not None and end_min is not None and start_min != end_min:
            now_dt = datetime.fromtimestamp(now)
            cur_min = now_dt.hour * 60 + now_dt.minute
            in_sched = (cur_min >= start_min or cur_min < end_min) if start_min > end_min else (start_min <= cur_min < end_min)
            if in_sched:
                override_until = self._next_quiet_end_ts(now_dt)
        self.store.set_kv("quiet_override_off_until", override_until)
        return self.get_quiet_status(now_ts=now)

    def _batch_matches_urgent_keywords(self, batch: list[NormalizedMessage]) -> bool:
        kws = [k.strip().lower() for k in self.config.quiet_hours.urgent_keywords if k and k.strip()]
        if not kws:
            return False
        parts: list[str] = []
        for m in batch:
            if m.clean_text:
                parts.append(m.clean_text)
            for art in m.artifacts:
                if art.transcript:
                    parts.append(art.transcript)
        hay = "\n".join(parts).lower()
        return any(kw in hay for kw in kws)

    def _queue_quiet_digest_item(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        summary_text: str,
        draft_reply: str,
        *,
        now_ts: float | None = None,
    ) -> None:
        raw_queue = self.store.get_kv("quiet_digest_queue", [])
        queue = [x for x in raw_queue if isinstance(x, dict)] if isinstance(raw_queue, list) else []
        clean_sum = " ".join((summary_text or "").split())
        if len(clean_sum) > 140:
            clean_sum = clean_sum[:137].rstrip() + "..."
        clean_draft = " ".join((draft_reply or "").split())
        if len(clean_draft) > 160:
            clean_draft = clean_draft[:157].rstrip() + "..."
        item = {
            "thread_id": thread.id,
            "channel_id": channel.id,
            "channel_name": channel.name,
            "sender_name": thread.sender_name,
            "title": thread.title,
            "pending_go": bool(thread.pending_go),
            "summary": clean_sum or thread.title,
            "draft_reply": clean_draft,
            "updated_at": float(now_ts if now_ts is not None else thread.updated_at or time.time()),
        }
        deduped = [x for x in queue if x.get("thread_id") != thread.id][-29:]
        deduped.append(item)
        self.store.set_kv("quiet_digest_queue", deduped)

    def format_briefing_bubbles(self, items: list[dict[str, Any]], *, is_quiet_flush: bool) -> list[str]:
        is_zh = self.config.language.lower().startswith("zh")
        count = len(items)
        if is_zh:
            header = (
                f"早安简报（夜间静默期共处理 {count} 条）："
                if is_quiet_flush
                else f"最近 12 小时频道简报（{count} 条）："
            )
        else:
            header = (
                f"Morning briefing ({count} overnight items):"
                if is_quiet_flush
                else f"Recent 12h briefing ({count} items):"
            )

        entries: list[str] = []
        for idx, item in enumerate(items, start=1):
            ch_name = str(item.get("channel_name") or item.get("channel_id") or "").strip()
            go_tag = (" [待确认 go]" if is_zh else " [needs go]") if item.get("pending_go") else ""
            summary = str(item.get("summary") or item.get("title") or "").strip()
            draft = str(item.get("draft_reply") or "").strip()
            if is_zh:
                line = f"{idx}. {ch_name}{go_tag}：{summary}"
                if draft:
                    line += f"\n   建议回复：{draft}"
            else:
                line = f"{idx}. {ch_name}{go_tag}: {summary}"
                if draft:
                    line += f"\n   Draft: {draft}"
            entries.append(line)

        bubbles: list[str] = []
        chunk_size = 3
        for pos in range(0, len(entries), chunk_size):
            part = entries[pos : pos + chunk_size]
            if pos == 0:
                bubbles.append(f"{header}\n\n" + "\n\n".join(part))
            else:
                bubbles.append("\n\n".join(part))
        return bubbles[:4] if bubbles else [header]

    async def send_briefing(self, *, notifier_name: str = "", now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts) if now_ts is not None else time.time()
        is_zh = self.config.language.lower().startswith("zh")
        raw_queue = self.store.get_kv("quiet_digest_queue", [])
        queue = [x for x in raw_queue if isinstance(x, dict)] if isinstance(raw_queue, list) else []

        if queue:
            self.store.set_kv("quiet_digest_queue", [])
            bubbles = self.format_briefing_bubbles(queue, is_quiet_flush=True)
            queued_flushed = len(queue)
        else:
            cutoff = now - 43200.0
            recent_items: list[dict[str, Any]] = []
            for th in self.store.list_threads(limit=30):
                if th.updated_at < cutoff:
                    continue
                ch = self.store.get_channel(th.channel_id)
                if ch is None or ch.adapter == "owner_direct":
                    continue
                events = self.store.list_thread_events(th.id)
                reply_evs = [ev for ev in events if ev.kind in {"agent_reply", "error"}]
                summary = ""
                draft = ""
                if reply_evs:
                    last_ev = reply_evs[-1]
                    summary = " ".join(last_ev.summary_bubbles[:2]).strip() or strip_markdown(last_ev.raw_text)
                    draft = last_ev.suggested_reply.strip()
                summary = " ".join(summary.split())
                if len(summary) > 140:
                    summary = summary[:137].rstrip() + "..."
                draft = " ".join(draft.split())
                if len(draft) > 160:
                    draft = draft[:157].rstrip() + "..."
                recent_items.append(
                    {
                        "thread_id": th.id,
                        "channel_id": th.channel_id,
                        "channel_name": ch.name,
                        "sender_name": th.sender_name,
                        "title": th.title,
                        "pending_go": th.pending_go,
                        "summary": summary or th.title,
                        "draft_reply": draft,
                        "updated_at": th.updated_at,
                    }
                )
            queued_flushed = 0
            if recent_items:
                bubbles = self.format_briefing_bubbles(recent_items[:12], is_quiet_flush=False)
            else:
                bubbles = [
                    "过去 12 小时没有新的群聊或私聊事件。"
                    if is_zh
                    else "No new channel activity in the last 12 hours."
                ]

        for bubble in bubbles:
            await self._send_owner_reply(notifier_name, bubble)
        self.store.set_kv("last_brief_sent_at", now)
        return {"sent": True, "queued_flushed": queued_flushed, "bubbles": bubbles}

    async def flush_quiet_digest_if_due(self, *, now_ts: float | None = None) -> int:
        if not self.config.quiet_hours.auto_brief_on_end:
            return 0
        if self.get_quiet_status(now_ts=now_ts)["active"]:
            return 0
        raw_queue = self.store.get_kv("quiet_digest_queue", [])
        if not isinstance(raw_queue, list) or not raw_queue:
            return 0
        res = await self.send_briefing(now_ts=now_ts)
        return int(res.get("queued_flushed") or 0)

    @classmethod
    def _summarize_batch_for_notice(cls, batch: list[NormalizedMessage]) -> tuple[str, list[str], str]:
        text_parts: list[str] = []
        image_paths: list[str] = []
        seen_imgs: set[str] = set()
        has_image = False
        for m in batch:
            raw_txt = (m.clean_text or "").strip()
            if (
                m.msg_type == "image"
                or bool(cls._MD_IMAGE_RE.search(raw_txt))
                or any(r.kind in {"image", "video_cover"} for r in m.media_refs)
            ):
                has_image = True
            txt = cls._MD_IMAGE_RE.sub("", raw_txt).strip()
            if txt and not cls._MEDIA_ONLY_TEXT_RE.fullmatch(txt):
                text_parts.append(txt)
            for art in m.artifacts:
                if art.kind == "audio" and art.transcript.strip():
                    text_parts.append(f"[语音] {art.transcript.strip()}")
                elif art.kind in {"image", "video_cover"} and art.path:
                    has_image = True
                    p = str(art.path).strip()
                    if p and p not in seen_imgs:
                        seen_imgs.add(p)
                        image_paths.append(p)
        original_text = "\n".join(text_parts).strip()
        first_line = original_text.splitlines()[0].strip() if original_text else ""
        topic = (first_line[:36] if first_line else ("[图片]" if has_image else "[Media]")).replace("\n", " ")
        return original_text, image_paths, topic

    def _completion_channel_label(self, channel: ChannelConfig, *, sender_name: str = "") -> str:
        is_zh = self.config.language.lower().startswith("zh")
        if channel.adapter == "owner_direct":
            return "个人直聊" if is_zh else "Personal"
        base = (
            f"{channel.name} · {sender_name}"
            if (channel.chat_type == ChatType.group and sender_name and sender_name != "Owner")
            else channel.name
        )
        plat = format_platform_label(channel.adapter, language=self.config.language)
        return f"{plat} · {base}" if plat else base

    async def _notify_completion_all(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for ntf in self.notifiers:
            try:
                tokens = await ntf.notify_completion(channel, thread, bubbles)
                for tok in tokens:
                    if tok:
                        self.store.bind_outbound(ntf.name, tok, thread.id)
                        pairs.append((ntf.name, tok))
            except Exception as exc:
                logger.warning("Notifier %s notify_completion failed: %s", ntf.name, exc)
        return pairs

    async def _send_owner_reply(self, notifier_name: str, text: str) -> None:
        targets = [n for n in self.notifiers if n.name == notifier_name] or self.notifiers
        for ntf in targets:
            try:
                sender_fn = getattr(ntf, "send_message", None)
                if callable(sender_fn):
                    await sender_fn(text)
            except Exception as exc:
                logger.warning("Notifier %s send_message failed: %s", ntf.name, exc)

    async def poll_channel(self, channel_id: str, *, debounced: bool = False) -> list[ThreadRecord]:
        channel = self.store.get_channel(channel_id)
        if channel is None or not channel.enabled:
            return []
        adapter = self.adapters.get(channel.adapter)
        if adapter is None:
            raise KeyError(f"No ChannelAdapter registered for {channel.adapter!r}")
        fetched = await adapter.fetch_recent(channel)
        return await self.ingest_messages(channel_id, fetched, debounced=debounced)

    async def ingest_messages(
        self,
        channel_id: str,
        messages: list[NormalizedMessage],
        *,
        seed_if_empty: bool = False,
        debounced: bool = False,
        now_ms: int | None = None,
    ) -> list[ThreadRecord]:
        channel = self.store.get_channel(channel_id)
        if channel is None or not messages:
            return []

        ordered = sorted(messages, key=lambda m: (m.position, m.create_ms))
        existing_recent = self.store.get_recent_messages(channel_id, limit=5)

        # First-seen seeding avoids flooding old backlog on initial startup
        if seed_if_empty and channel.cursor_pos == 0 and channel.cursor_ms == 0 and not existing_recent:
            non_deleted = [m for m in ordered if not m.deleted]
            self.store.record_messages(channel_id, non_deleted[-50:])
            max_pos = max((m.position for m in ordered), default=0)
            max_ms = max((m.create_ms for m in ordered), default=0)
            self.store.advance_channel_cursor(channel_id, max_pos, max_ms)
            return []

        newer = [
            m
            for m in ordered
            if (channel.cursor_pos > 0 and m.position > channel.cursor_pos)
            or (channel.cursor_pos == 0 and m.position > 0)
            or (m.position == 0 and m.create_ms > channel.cursor_ms)
        ]
        if not newer:
            return []

        max_pos = max((m.position for m in newer), default=channel.cursor_pos)
        max_ms = max((m.create_ms for m in newer), default=channel.cursor_ms)
        self.store.advance_channel_cursor(channel_id, max_pos, max_ms)

        non_deleted = [m for m in newer if not m.deleted and m.msg_type != "system"]
        self.store.record_messages(channel_id, non_deleted)

        ref_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        max_age_ms = channel.max_catchup_age_s * 1000
        triggers: list[NormalizedMessage] = []
        for msg in non_deleted:
            if not should_trigger(channel, msg):
                continue
            if max_age_ms > 0 and msg.create_ms > 0 and (ref_ms - msg.create_ms) > max_age_ms:
                continue
            triggers.append(msg)

        if not triggers:
            return []

        groups: dict[str, list[NormalizedMessage]] = {}
        if channel.chat_type == ChatType.group:
            for msg in triggers:
                groups.setdefault(f"{channel_id}::{msg.sender_id or 'unknown'}", []).append(msg)
        else:
            groups[f"{channel_id}::p2p"] = triggers

        if debounced:
            for key, items in groups.items():
                self.debouncer.add(key, items, arm_timer=True)
            return []

        created: list[ThreadRecord] = []
        for batch in groups.values():
            th = await self._process_trigger_batch(channel, batch)
            if th is not None:
                created.append(th)
        return created

    async def flush_all(self) -> int:
        return await self.debouncer.flush_all()

    async def _on_debounced_batch(self, key: str, items: list[NormalizedMessage]) -> None:
        channel_id = key.split("::", 1)[0]
        channel = self.store.get_channel(channel_id)
        if channel is not None and items:
            await self._process_trigger_batch(channel, items)

    async def _enrich_media(self, channel: ChannelConfig, msgs: list[NormalizedMessage]) -> None:
        adapter = self.adapters.get(channel.adapter)
        for msg in msgs:
            if not msg.media_refs or msg.artifacts:
                continue
            if adapter is not None:
                dest = self.media_dir / channel.id / msg.message_id
                artifacts = await adapter.download_media(channel, msg, dest)
                if self.media_processor is not None:
                    for art in artifacts:
                        if art.kind == "audio" and art.path and not art.transcript:
                            tr, err = self.media_processor.transcribe_audio(
                                Path(art.path),
                                prompt=channel.whisper_prompt,
                                language=self.config.language,
                            )
                            art.transcript = tr
                            art.error = err
                msg.artifacts = artifacts
                self.store.record_messages(channel.id, [msg])

    async def _process_trigger_batch(
        self,
        channel: ChannelConfig,
        batch: list[NormalizedMessage],
    ) -> ThreadRecord | None:
        recent_messages = self.store.get_recent_messages(channel.id, limit=50)
        adapter = self.adapters.get(channel.adapter)
        now_ts = time.time()

        group_ctx: GroupContext | None = None
        matched_thread: ThreadRecord | None = None

        if channel.chat_type == ChatType.group:
            async def _fetch_missing(ids: list[str]) -> list[NormalizedMessage]:
                found = self.store.get_messages_by_external_ids(channel.id, ids)
                found_ids = {m.message_id for m in found}
                still = [mid for mid in ids if mid not in found_ids]
                if still and adapter is not None:
                    remote = await adapter.fetch_by_ids(channel, still)
                    if remote:
                        self.store.record_messages(channel.id, remote)
                        found.extend(remote)
                return found

            group_ctx = await assemble_group_context(
                batch,
                recent_messages,
                fetch_by_ids=_fetch_missing,
                same_sender_window_s=channel.same_sender_window_s,
                handoff_window_s=channel.handoff_window_s,
                background_window_s=channel.background_window_s,
            )
            await self._enrich_media(channel, [*group_ctx.direct_messages, *batch])
            threads = self.store.list_channel_group_threads(channel.id, limit=20)
            matched_thread = match_group_thread(
                threads,
                batch,
                group_ctx.reply_chain_ids,
                now_ts,
                sender_ttl_s=channel.group_sender_ttl_s,
                reply_ttl_s=channel.group_reply_ttl_s,
            )
        else:
            await self._enrich_media(channel, batch)
            matched_thread = self.store.find_active_p2p_thread(
                channel.id,
                ttl_s=channel.session_ttl_s,
                now_ts=now_ts,
            )

        has_active = matched_thread is not None
        if is_small_talk_only(batch, recent_messages, has_active):
            return None

        sender = batch[0]
        display_name = (
            f"{channel.name} ({sender.sender_name})"
            if channel.chat_type == ChatType.group
            else channel.name
        )
        original_preview, image_paths, topic = self._summarize_batch_for_notice(batch)
        title = f"[{channel.adapter}] {display_name}: {topic}"
        is_steer = matched_thread is not None
        thread_id = matched_thread.id if matched_thread is not None else f"th_{uuid.uuid4().hex[:12]}"
        created_at = matched_thread.created_at if matched_thread is not None else now_ts
        extra_ids = group_ctx.reply_chain_ids if (group_ctx and matched_thread is None) else []
        mids = list(
            dict.fromkeys(
                [
                    *(matched_thread.message_ids if matched_thread is not None else []),
                    *(m.message_id for m in batch),
                    *extra_ids,
                ]
            )
        )
        active_record = self.store.create_or_update_thread(
            ThreadRecord(
                id=thread_id,
                channel_id=channel.id,
                backend=self.backend.name,
                backend_thread_ref=(
                    matched_thread.backend_thread_ref if matched_thread is not None else f"pending_{thread_id}"
                ),
                sender_id=(matched_thread.sender_id if matched_thread is not None else sender.sender_id),
                sender_name=(matched_thread.sender_name if matched_thread is not None else sender.sender_name),
                title=topic,
                status=ThreadStatus.active,
                root_msg_id=(matched_thread.root_msg_id if matched_thread is not None else sender.message_id),
                message_ids=mids,
                triage_mode=(matched_thread.triage_mode if matched_thread is not None else TriageMode.mode_1_qa),
                pending_go=False,
                created_at=created_at,
                updated_at=time.time(),
            )
        )
        quiet_state = self.get_quiet_status(now_ts=now_ts)
        suppress_notify = bool(quiet_state["active"] and not self._batch_matches_urgent_keywords(batch))
        if not suppress_notify:
            start_tokens = await self._notify_start_all(
                channel,
                active_record,
                original_preview,
                is_steer=is_steer,
                attachments=image_paths,
            )
            self._bind_tokens(thread_id, start_tokens)

        target_cwd = Path(channel.workspace_dir).expanduser() if channel.workspace_dir else self.config.workspace_dir
        try:
            if matched_thread is not None:
                steer_msg = build_steer_prompt(
                    channel,
                    recent_messages,
                    batch,
                    group_context=group_ctx,
                    language=self.config.language,
                )
                result = await self.backend.steer_thread(
                    matched_thread.backend_thread_ref,
                    title,
                    steer_msg,
                    cwd=target_cwd,
                    channel=channel,
                )
                backend_ref = matched_thread.backend_thread_ref
            else:
                cross_memory = (
                    self.store.get_recent_cross_channel_context(
                        exclude_channel_id=channel.id,
                        workspace_dir=str(target_cwd),
                        window_s=self.config.cross_channel_window_s,
                        limit=self.config.cross_channel_max_items,
                        now_ts=now_ts,
                    )
                    if self.config.cross_channel_window_s > 0 and self.config.cross_channel_max_items > 0
                    else []
                )
                prompt = build_new_thread_prompt(
                    channel,
                    channel.persona_text,
                    recent_messages,
                    batch,
                    group_context=group_ctx,
                    cross_channel_memory=cross_memory,
                    language=self.config.language,
                )
                backend_ref, result = await self.backend.start_thread(
                    title,
                    prompt,
                    cwd=target_cwd,
                    channel=channel,
                )
        except Exception:
            self.store.create_or_update_thread(
                active_record.model_copy(
                    update={"status": ThreadStatus.error, "pending_go": False, "updated_at": time.time()}
                )
            )
            raise

        completion_label = self._completion_channel_label(
            channel,
            sender_name=matched_thread.sender_name if matched_thread else sender.sender_name,
        )
        all_bubbles, draft_reply = format_completion_bubbles(
            completion_label,
            result.raw_text,
            error=result.error,
            language=self.config.language,
        )
        pending_go = (
            channel.autonomy_mode in {AutonomyMode.diagnose_only, AutonomyMode.auto_fix}
            and result.triage_mode == TriageMode.mode_4_bug
            and bool(result.needs_attention)
            and not result.error
        )
        status = (
            ThreadStatus.error
            if result.error
            else ThreadStatus.awaiting_go
            if pending_go
            else ThreadStatus.completed
        )

        record = active_record.model_copy(
            update={
                "backend_thread_ref": backend_ref or thread_id,
                "status": status,
                "triage_mode": result.triage_mode,
                "pending_go": pending_go,
                "updated_at": time.time(),
            }
        )
        record = self.store.create_or_update_thread(record)
        self.store.add_thread_event(
            ThreadEvent(
                thread_id=record.id,
                kind="inbound",
                raw_text="\n".join(m.clean_text or f"[{m.msg_type}]" for m in batch),
            )
        )
        self.store.add_thread_event(
            ThreadEvent(
                thread_id=record.id,
                kind="error" if result.error else "agent_reply",
                summary_bubbles=result.summary_bubbles or all_bubbles[:1],
                suggested_reply=draft_reply or result.suggested_reply,
                needs_attention=result.needs_attention,
                raw_text=result.raw_text,
            )
        )

        if suppress_notify:
            quiet_summary = (
                " ".join(result.summary_bubbles[:2]).strip()
                or strip_markdown(result.raw_text)
                or result.error
            )
            self._queue_quiet_digest_item(
                channel,
                record,
                quiet_summary,
                draft_reply or result.suggested_reply,
                now_ts=now_ts,
            )
        else:
            await self._notify_completion_all(channel, record, all_bubbles)

        del is_steer
        return record

    async def steer_from_owner(
        self,
        thread_id: str,
        owner_message: str,
        *,
        approved_go: bool = False,
    ) -> ThreadRecord:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread: {thread_id}")
        channel = self.store.get_channel(thread.channel_id)
        if channel is None:
            raise KeyError(f"Unknown channel: {thread.channel_id}")

        self.store.add_thread_event(
            ThreadEvent(
                thread_id=thread.id,
                kind="owner_approve" if approved_go else "owner_steer",
                raw_text=owner_message,
            )
        )
        is_personal = channel.adapter == "owner_direct" or channel.id == "personal_direct"
        target_cwd = Path(channel.workspace_dir).expanduser() if channel.workspace_dir else self.config.workspace_dir
        if is_personal:
            steer_prompt = build_personal_direct_prompt(
                owner_message,
                notifier=thread.sender_id or "imessage",
                workspace_dir=target_cwd,
                is_steer=True,
                language=self.config.language,
            )
            steer_title = f"[direct] {channel.name}: {thread.title}"
        else:
            steer_prompt = build_owner_steer_prompt(
                owner_message,
                approved_go=approved_go,
                language=self.config.language,
            )
            steer_title = f"[{channel.adapter}] {channel.name}: {thread.title}"
        active_thread = self.store.create_or_update_thread(
            thread.model_copy(update={"status": ThreadStatus.active, "updated_at": time.time()})
        )
        try:
            result = await self.backend.steer_thread(
                active_thread.backend_thread_ref,
                steer_title,
                steer_prompt,
                cwd=target_cwd,
                channel=channel,
            )
        except Exception:
            self.store.create_or_update_thread(
                active_thread.model_copy(update={"status": ThreadStatus.error, "updated_at": time.time()})
            )
            raise
        all_bubbles, draft_reply = format_completion_bubbles(
            self._completion_channel_label(channel, sender_name=thread.sender_name),
            result.raw_text,
            error=result.error,
            language=self.config.language,
            include_header=not is_personal,
        )
        next_status = ThreadStatus.error if result.error else ThreadStatus.completed
        updated = self.store.create_or_update_thread(
            thread.model_copy(
                update={
                    "status": next_status,
                    "pending_go": False if approved_go else thread.pending_go,
                    "updated_at": time.time(),
                }
            )
        )
        self.store.add_thread_event(
            ThreadEvent(
                thread_id=thread.id,
                kind="error" if result.error else "agent_reply",
                summary_bubbles=result.summary_bubbles or all_bubbles[:1],
                suggested_reply=draft_reply or result.suggested_reply,
                needs_attention=result.needs_attention,
                raw_text=result.raw_text,
            )
        )
        if is_personal:
            self.store.set_kv("personal_direct_active_thread_id", updated.id)
        await self._notify_completion_all(channel, updated, all_bubbles)
        return updated

    def dismiss_go(self, thread_id: str) -> ThreadRecord:
        updated = self.store.dismiss_thread_go(thread_id)
        if updated is None:
            raise KeyError(f"Unknown thread: {thread_id}")
        self.store.add_thread_event(
            ThreadEvent(thread_id=updated.id, kind="owner_dismiss", raw_text="dismiss")
        )
        return updated

    def _format_owner_action_message(
        self,
        action: OwnerAction,
        *,
        channel: ChannelConfig | None = None,
    ) -> str:
        parts: list[str] = [action.text.strip()] if action.text.strip() else []
        prompt_hint = channel.whisper_prompt if channel else ""
        for att in action.attachments:
            att_path = str(att.get("path") or "").strip()
            name = str(att.get("name") or (Path(att_path).name if att_path else "attachment"))
            is_audio = att.get("is_audio") == "true"
            if is_audio and att_path:
                if self.media_processor is not None and Path(att_path).is_file():
                    transcript, err = self.media_processor.transcribe_audio(
                        Path(att_path),
                        prompt=prompt_hint,
                        language=self.config.language,
                    )
                    if transcript.strip():
                        parts.append(f"[Voice transcript ({name})]: {transcript.strip()}")
                    else:
                        parts.append(f"[Audio attachment ({name}): {att_path} (transcription unavailable: {err})]")
                else:
                    parts.append(f"[Audio attachment ({name}): {att_path}]")
            elif att_path:
                parts.append(f"[Image/File attachment ({name}): {att_path}]")
        return "\n".join(p for p in parts if p).strip()

    def _ensure_personal_channel(self) -> ChannelConfig:
        ws = (
            str(self.config.personal_workspace_dir.expanduser())
            if self.config.personal_workspace_dir is not None
            else str(self.config.workspace_dir)
        )
        existing = self.store.get_channel("personal_direct")
        if existing is not None and existing.workspace_dir == ws:
            return existing
        is_zh = self.config.language.lower().startswith("zh")
        ch = ChannelConfig(
            id="personal_direct",
            adapter="owner_direct",
            external_chat_id="owner",
            name="个人直聊" if is_zh else "Personal Assistant",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.auto_fix,
            workspace_dir=ws,
            session_ttl_s=self.config.personal_session_ttl_s,
            enabled=False,
        )
        return self.store.upsert_channel(ch)

    async def _handle_personal_direct(
        self,
        action: OwnerAction,
        owner_message: str,
        *,
        force_new: bool = False,
    ) -> ThreadRecord | None:
        is_zh = self.config.language.lower().startswith("zh")
        ch = self._ensure_personal_channel()
        target_cwd = Path(ch.workspace_dir).expanduser() if ch.workspace_dir else self.config.workspace_dir
        ttl_s = max(60, int(self.config.personal_session_ttl_s))
        active_id = str(self.store.get_kv("personal_direct_active_thread_id", "") or "")
        reset_at = float(self.store.get_kv("personal_direct_reset_at", 0.0) or 0.0)
        now_ts = time.time()

        active_thread: ThreadRecord | None = None
        if not force_new and active_id:
            candidate = self.store.get_thread(active_id)
            if (
                candidate is not None
                and candidate.channel_id == ch.id
                and candidate.status != ThreadStatus.error
                and candidate.updated_at >= reset_at
                and (now_ts - candidate.updated_at) <= ttl_s
            ):
                active_thread = candidate

        topic = (owner_message.splitlines()[0] if owner_message else "Personal chat")[:36]
        title = f"[direct] {ch.name}: {topic}"

        if active_thread is not None:
            self.store.add_thread_event(
                ThreadEvent(thread_id=active_thread.id, kind="owner_steer", raw_text=owner_message)
            )
            prompt = build_personal_direct_prompt(
                owner_message,
                notifier=action.notifier,
                workspace_dir=target_cwd,
                is_steer=True,
                language=self.config.language,
            )
            active_thread = self.store.create_or_update_thread(
                active_thread.model_copy(
                    update={"status": ThreadStatus.active, "pending_go": False, "updated_at": time.time()}
                )
            )
            try:
                result = await self.backend.steer_thread(
                    active_thread.backend_thread_ref,
                    f"[direct] {ch.name}: {active_thread.title}",
                    prompt,
                    cwd=target_cwd,
                    channel=ch,
                )
            except Exception:
                self.store.create_or_update_thread(
                    active_thread.model_copy(update={"status": ThreadStatus.error, "updated_at": time.time()})
                )
                raise
            all_bubbles, draft_reply = format_completion_bubbles(
                self._completion_channel_label(ch),
                result.raw_text,
                error=result.error,
                language=self.config.language,
                include_header=False,
            )
            updated = self.store.create_or_update_thread(
                active_thread.model_copy(
                    update={
                        "status": ThreadStatus.error if result.error else ThreadStatus.completed,
                        "pending_go": False,
                        "updated_at": time.time(),
                    }
                )
            )
            self.store.add_thread_event(
                ThreadEvent(
                    thread_id=updated.id,
                    kind="error" if result.error else "agent_reply",
                    summary_bubbles=result.summary_bubbles or all_bubbles[:1],
                    suggested_reply=draft_reply or result.suggested_reply,
                    needs_attention=result.needs_attention,
                    raw_text=result.raw_text,
                )
            )
            self.store.set_kv("personal_direct_active_thread_id", updated.id)
            await self._notify_completion_all(ch, updated, all_bubbles)
            return updated

        thread_id = f"th_{uuid.uuid4().hex[:12]}"
        active_record = self.store.create_or_update_thread(
            ThreadRecord(
                id=thread_id,
                channel_id=ch.id,
                backend=self.backend.name,
                backend_thread_ref=f"personal_{thread_id}",
                sender_id=action.notifier,
                sender_name="Owner",
                title=topic,
                status=ThreadStatus.active,
                pending_go=False,
                created_at=now_ts,
                updated_at=time.time(),
            )
        )
        start_tokens: list[tuple[str, str]] = []
        if self.config.ack_on_receive:
            start_tokens = await self._notify_start_all(ch, active_record, topic, is_steer=False)
            self._bind_tokens(thread_id, start_tokens)

        prompt = build_personal_direct_prompt(
            owner_message,
            notifier=action.notifier,
            workspace_dir=target_cwd,
            is_steer=False,
            language=self.config.language,
        )
        try:
            backend_ref, result = await self.backend.start_thread(
                title,
                prompt,
                cwd=target_cwd,
                channel=ch,
            )
        except Exception:
            self.store.create_or_update_thread(
                active_record.model_copy(update={"status": ThreadStatus.error, "updated_at": time.time()})
            )
            raise
        all_bubbles, draft_reply = format_completion_bubbles(
            self._completion_channel_label(ch),
            result.raw_text,
            error=result.error,
            language=self.config.language,
            include_header=False,
        )
        record = active_record.model_copy(
            update={
                "backend_thread_ref": backend_ref or active_record.backend_thread_ref,
                "status": ThreadStatus.error if result.error else ThreadStatus.completed,
                "triage_mode": result.triage_mode,
                "pending_go": False,
                "updated_at": time.time(),
            }
        )
        record = self.store.create_or_update_thread(record)
        self.store.add_thread_event(ThreadEvent(thread_id=record.id, kind="inbound", raw_text=owner_message))
        self.store.add_thread_event(
            ThreadEvent(
                thread_id=record.id,
                kind="error" if result.error else "agent_reply",
                summary_bubbles=result.summary_bubbles or all_bubbles[:1],
                suggested_reply=draft_reply or result.suggested_reply,
                needs_attention=result.needs_attention,
                raw_text=result.raw_text,
            )
        )
        self.store.set_kv("personal_direct_active_thread_id", record.id)
        await self._notify_completion_all(ch, record, all_bubbles)
        return record

    async def handle_owner_action(self, action: OwnerAction) -> ThreadRecord | None:
        """Dispatch an owner command received from Telegram, iMessage, or another phone entrance."""
        is_zh = self.config.language.lower().startswith("zh")
        if action.kind == "help":
            msg = (
                "Watchpost 指令说明：\n"
                "- status / 状态：查看运行状态与待确认会话\n"
                "- list / 列表：查看最近 5 条会话（支持 #1 / #2 引用）\n"
                "- /brief / 早报：立即发送夜间或最近 12 小时简报\n"
                "- /mute 2h / /unmute：临时静音或恢复消息通知\n"
                "- #N 内容 或 #N go：追问或批准列表第 N 条会话\n"
                "- go / 执行：批准最近一条待确认会话执行修复\n"
                "- skip / 忽略（或 #N skip / 左滑回复 忽略）：清除待确认（go）标记但不执行\n"
                "- /new [内容]：开启新的个人直聊会话\n"
                "- /reset：重置个人直聊上下文\n"
                "- 直接发文字/语音/图片：自动进入个人直聊；左滑回复通知气泡可追问对应工作会话"
                if is_zh
                else "Watchpost commands:\n"
                "- status: view running status & pending approvals\n"
                "- list: show recent threads (#1..#5)\n"
                "- /brief: send overnight or recent 12h briefing now\n"
                "- /mute [2h] / /unmute: pause or resume notifications\n"
                "- #N <text> or #N go: steer or approve listed thread N\n"
                "- go / approve: approve latest pending thread\n"
                "- skip / dismiss (or #N skip): clear pending go without executing\n"
                "- /new [text]: start a fresh personal direct chat\n"
                "- /reset: clear personal direct chat context\n"
                "- direct text/voice/image: personal chat; swipe-reply on alert bubble to steer work thread"
            )
            await self._send_owner_reply(action.notifier, msg)
            return None

        if action.kind == "brief":
            await self.send_briefing(notifier_name=action.notifier)
            return None

        if action.kind == "mute":
            mins = self._parse_duration_minutes(action.text, default_minutes=120)
            st = self.set_quiet_mute(minutes=mins)
            msg = (
                f"已开启静音 {mins} 分钟（期间仅默默排查并写入简报，收到 /brief 或解除后汇总；紧急关键词仍会提醒）。"
                if is_zh
                else f"Notifications muted for {mins}m (silent diagnosis + digest queued until unmuted or /brief)."
            )
            if st.get("queued_count"):
                msg += f" 当前已攒 {st['queued_count']} 条。" if is_zh else f" ({st['queued_count']} queued.)"
            await self._send_owner_reply(action.notifier, msg)
            return None

        if action.kind == "unmute":
            st = self.clear_quiet_mute()
            msg = (
                "已恢复消息通知。"
                if is_zh
                else "Notifications resumed."
            )
            await self._send_owner_reply(action.notifier, msg)
            if int(st.get("queued_count") or 0) > 0:
                await self.send_briefing(notifier_name=action.notifier)
            return None

        if action.kind == "status":
            metrics = self.store.get_metrics()
            pending = [t for t in self.store.list_threads(limit=20) if t.pending_go]
            personal_id = str(self.store.get_kv("personal_direct_active_thread_id", "") or "")
            personal_th = self.store.get_thread(personal_id) if personal_id else None
            if is_zh:
                msg = (
                    f"Watchpost 运行中（{self.backend.name}）：监控 {metrics['channels']} 个通道，"
                    f"累计 {metrics['threads']} 个会话，{metrics['awaiting_go']} 个待确认执行（go）。"
                )
                if pending:
                    msg += "\n待确认：" + "；".join(t.title for t in pending[:3])
                if personal_th is not None:
                    msg += f"\n个人直聊：{personal_th.title} [{personal_th.status.value}]"
            else:
                msg = (
                    f"Watchpost running ({self.backend.name}): {metrics['channels']} channels, "
                    f"{metrics['threads']} threads, {metrics['awaiting_go']} awaiting go."
                )
                if pending:
                    msg += "\nPending go: " + "; ".join(t.title for t in pending[:3])
                if personal_th is not None:
                    msg += f"\nPersonal chat: {personal_th.title} [{personal_th.status.value}]"
            await self._send_owner_reply(action.notifier, msg)
            return None

        if action.kind == "list":
            threads = self.store.list_threads(limit=5)
            if not threads:
                await self._send_owner_reply(
                    action.notifier,
                    "当前暂无历史会话。" if is_zh else "No threads yet.",
                )
                return None
            self.store.set_kv("last_listed_thread_ids", [th.id for th in threads])
            lines = ["最近会话：" if is_zh else "Recent threads:"]
            for idx, th in enumerate(threads, start=1):
                tag = "待go" if th.pending_go else th.status.value
                lines.append(f"{idx}. [{tag}] {th.title}")
            await self._send_owner_reply(action.notifier, "\n".join(lines))
            return None

        if action.kind == "reset_personal":
            self.store.set_kv("personal_direct_active_thread_id", "")
            self.store.set_kv("personal_direct_reset_at", time.time())
            await self._send_owner_reply(
                action.notifier,
                "已重置个人直聊上下文，下条消息将开启新会话。"
                if is_zh
                else "Reset personal direct chat context; next message starts fresh.",
            )
            return None

        if action.kind == "new_personal":
            self.store.set_kv("personal_direct_active_thread_id", "")
            self.store.set_kv("personal_direct_reset_at", time.time())
            formatted = self._format_owner_action_message(action)
            if not formatted:
                await self._send_owner_reply(
                    action.notifier,
                    "已开启新的个人直聊会话，请发送问题。"
                    if is_zh
                    else "Started a new personal direct chat; send your prompt.",
                )
                return None
            return await self._handle_personal_direct(action, formatted, force_new=True)

        target_id = action.thread_id.strip()
        if not target_id and action.list_index > 0:
            listed = self.store.get_kv("last_listed_thread_ids", [])
            if isinstance(listed, list) and 1 <= action.list_index <= len(listed):
                target_id = str(listed[action.list_index - 1])
            elif not listed:
                threads = self.store.list_threads(limit=max(5, action.list_index))
                if 1 <= action.list_index <= len(threads):
                    target_id = threads[action.list_index - 1].id
            if not target_id:
                await self._send_owner_reply(
                    action.notifier,
                    (
                        f"未找到列表第 {action.list_index} 条会话，请先发送 list 查看。"
                        if is_zh
                        else f"No thread #{action.list_index} found; send list first."
                    ),
                )
                return None

        if not target_id and action.reply_token:
            target_id = self.store.resolve_outbound(action.notifier, action.reply_token)

        if not target_id and action.kind == "approve_go":
            for th in self.store.list_threads(limit=20):
                if th.pending_go:
                    target_id = th.id
                    break
            if not target_id:
                recent = self.store.list_threads(limit=1)
                target_id = recent[0].id if recent else ""

        if action.kind == "dismiss_go":
            if not target_id:
                for th in self.store.list_threads(limit=20):
                    if th.pending_go:
                        target_id = th.id
                        break
            if not target_id:
                await self._send_owner_reply(
                    action.notifier,
                    "当前没有待确认执行（go）的会话。" if is_zh else "No pending go approvals to dismiss.",
                )
                return None
            try:
                updated = self.dismiss_go(target_id)
            except KeyError:
                await self._send_owner_reply(
                    action.notifier,
                    "未找到对应会话，无法关闭待确认标记。" if is_zh else "Thread not found; cannot dismiss go.",
                )
                return None
            await self._send_owner_reply(
                action.notifier,
                (
                    f"已关闭待确认（go）：{updated.title}"
                    if is_zh
                    else f"Dismissed pending go: {updated.title}"
                ),
            )
            return updated

        if not target_id and action.kind == "steer":
            has_personal_ws = self.config.personal_workspace_dir is not None
            recent = self.store.list_threads(limit=1)
            if has_personal_ws or not recent:
                formatted = self._format_owner_action_message(action)
                if not formatted:
                    return None
                return await self._handle_personal_direct(action, formatted, force_new=False)
            target_id = recent[0].id

        if not target_id:
            await self._send_owner_reply(
                action.notifier,
                "当前没有可继续或批准的会话。" if is_zh else "No active thread to steer or approve.",
            )
            return None

        target_thread = self.store.get_thread(target_id)
        target_ch = self.store.get_channel(target_thread.channel_id) if target_thread else None
        approved_go = action.kind == "approve_go"
        steer_text = self._format_owner_action_message(action, channel=target_ch) or ("go" if approved_go else "")
        return await self.steer_from_owner(target_id, steer_text, approved_go=approved_go)

    async def poll_all_channels(self, *, seed_if_empty: bool = False, debounced: bool = True) -> int:
        """Poll every enabled channel whose adapter is registered; return channels polled cleanly."""
        polled = 0
        for ch in self.store.list_channels():
            if not ch.enabled or ch.adapter == "owner_direct":
                continue
            adapter = self.adapters.get(ch.adapter)
            if adapter is None:
                continue
            try:
                fetched = await adapter.fetch_recent(ch)
                await self.ingest_messages(
                    ch.id,
                    fetched,
                    seed_if_empty=seed_if_empty,
                    debounced=debounced,
                )
                polled += 1
            except Exception as exc:
                logger.warning("Channel poll failed (%s / %s): %s", ch.adapter, ch.id, exc)
        return polled

    async def poll_owner_actions_once(self) -> int:
        """Poll all registered phone/desktop notifiers and dispatch any owner actions."""
        handled = 0
        for ntf in self.notifiers:
            try:
                actions = await ntf.poll_owner_actions()
            except Exception as exc:
                logger.warning("Notifier %s poll_owner_actions failed: %s", ntf.name, exc)
                continue
            for action in actions:
                try:
                    await self.handle_owner_action(action)
                    handled += 1
                except Exception as exc:
                    logger.warning("Failed handling owner action %s: %s", action.kind, exc)
        return handled

    async def run_demo_scenario(self) -> dict[str, Any]:
        """Populate SQLite with the built-in Field-Ops simulator channels & sample scenario."""
        sim = self.adapters.get("simulator")
        if not isinstance(sim, SimulatorAdapter):
            sim = SimulatorAdapter()
            self.register_adapter(sim)

        channels = default_demo_channels()
        for ch in channels:
            if self.store.get_channel(ch.id) is None:
                self.store.upsert_channel(ch)

        # Stage 1: seed overnight messages in both channels (zero triggers)
        dm_msgs = await sim.fetch_recent(channels[0])
        grp_msgs = await sim.fetch_recent(channels[1])
        await self.ingest_messages(channels[0].id, dm_msgs[:1], seed_if_empty=True)
        await self.ingest_messages(channels[1].id, grp_msgs[:4], seed_if_empty=True)

        # Stage 2: replay new burst (recalled + small talk ignored, P2P Q&A + Group handoff triggered)
        th_dm = await self.ingest_messages(channels[0].id, dm_msgs[1:])
        th_grp = await self.ingest_messages(channels[1].id, grp_msgs[4:])
        threads = [*th_dm, *th_grp]
        return {
            "channels_seeded": len(channels),
            "threads_created": len(threads),
            "thread_ids": [t.id for t in threads],
            "downloaded_media_ids": list(sim.downloaded_ids),
        }
