"""Watchpost orchestration engine connecting channels, filters, context, debouncer, backends, and notifiers."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from watchpost.adapters.simulator import SimulatorAdapter, default_demo_channels
from watchpost.bubbles import format_completion_bubbles
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
)
from watchpost.ports import AgentBackend, ChannelAdapter, MediaProcessor, NotifierAdapter
from watchpost.prompts import build_new_thread_prompt, build_owner_steer_prompt, build_steer_prompt
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
    ) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for ntf in self.notifiers:
            try:
                tokens = await ntf.notify_start(channel, thread, preview, is_steer=is_steer)
                pairs.extend((ntf.name, tok) for tok in tokens if tok)
            except Exception as exc:
                logger.warning("Notifier %s notify_start failed: %s", ntf.name, exc)
        return pairs

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
        topic = (sender.clean_text or "[Media]")[:36].replace("\n", " ")
        title = f"[{channel.adapter}] {display_name}: {topic}"
        preview_thread = matched_thread or ThreadRecord(
            id="pending",
            channel_id=channel.id,
            backend=self.backend.name,
            backend_thread_ref="",
            sender_id=sender.sender_id,
            sender_name=sender.sender_name,
            title=topic,
        )
        start_tokens = await self._notify_start_all(
            channel,
            preview_thread,
            sender.clean_text or f"[{sender.msg_type}]",
            is_steer=(matched_thread is not None),
        )

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
                cwd=self.config.workspace_dir,
                channel=channel,
            )
            thread_id = matched_thread.id
            backend_ref = matched_thread.backend_thread_ref
            mids = list(dict.fromkeys([*matched_thread.message_ids, *(m.message_id for m in batch)]))
            created_at = matched_thread.created_at
            is_steer = True
        else:
            prompt = build_new_thread_prompt(
                channel,
                channel.persona_text,
                recent_messages,
                batch,
                group_context=group_ctx,
                language=self.config.language,
            )
            backend_ref, result = await self.backend.start_thread(
                title,
                prompt,
                cwd=self.config.workspace_dir,
                channel=channel,
            )
            thread_id = f"th_{uuid.uuid4().hex[:12]}"
            extra_ids = group_ctx.reply_chain_ids if group_ctx else []
            mids = list(dict.fromkeys([*(m.message_id for m in batch), *extra_ids]))
            created_at = now_ts
            is_steer = False

        all_bubbles, draft_reply = format_completion_bubbles(
            display_name,
            result.raw_text,
            error=result.error,
            language=self.config.language,
        )
        pending_go = (
            channel.autonomy_mode == AutonomyMode.diagnose_only
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

        record = ThreadRecord(
            id=thread_id,
            channel_id=channel.id,
            backend=self.backend.name,
            backend_thread_ref=backend_ref or thread_id,
            sender_id=sender.sender_id,
            sender_name=sender.sender_name,
            title=topic,
            status=status,
            root_msg_id=matched_thread.root_msg_id if matched_thread else sender.message_id,
            message_ids=mids,
            triage_mode=result.triage_mode,
            pending_go=pending_go,
            created_at=created_at,
            updated_at=time.time(),
        )
        record = self.store.create_or_update_thread(record)
        self._bind_tokens(record.id, start_tokens)
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
        steer_prompt = build_owner_steer_prompt(
            owner_message,
            approved_go=approved_go,
            language=self.config.language,
        )
        result = await self.backend.steer_thread(
            thread.backend_thread_ref,
            f"[{channel.adapter}] {channel.name}: {thread.title}",
            steer_prompt,
            cwd=self.config.workspace_dir,
            channel=channel,
        )
        all_bubbles, draft_reply = format_completion_bubbles(
            channel.name,
            result.raw_text,
            error=result.error,
            language=self.config.language,
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
        await self._notify_completion_all(channel, updated, all_bubbles)
        return updated

    async def handle_owner_action(self, action: OwnerAction) -> ThreadRecord | None:
        """Dispatch an owner command received from Telegram, iMessage, or another phone entrance."""
        is_zh = self.config.language.lower().startswith("zh")
        if action.kind == "status":
            metrics = self.store.get_metrics()
            pending = [t for t in self.store.list_threads(limit=20) if t.pending_go]
            if is_zh:
                msg = (
                    f"Watchpost 运行中：监控 {metrics['channels']} 个通道，"
                    f"累计 {metrics['threads']} 个会话，{metrics['awaiting_go']} 个待确认执行（go）。"
                )
                if pending:
                    msg += f" 最新待确认：{pending[0].title}"
            else:
                msg = (
                    f"Watchpost running: {metrics['channels']} channels, "
                    f"{metrics['threads']} threads, {metrics['awaiting_go']} awaiting go."
                )
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
            lines = ["最近会话：" if is_zh else "Recent threads:"]
            for idx, th in enumerate(threads, start=1):
                tag = "待go" if th.pending_go else th.status.value
                lines.append(f"{idx}. [{tag}] {th.title}")
            await self._send_owner_reply(action.notifier, "\n".join(lines))
            return None

        target_id = action.thread_id.strip()
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

        if not target_id:
            await self._send_owner_reply(
                action.notifier,
                "当前没有可继续或批准的会话。" if is_zh else "No active thread to steer or approve.",
            )
            return None

        approved_go = action.kind == "approve_go"
        steer_text = action.text or ("go" if approved_go else "")
        return await self.steer_from_owner(target_id, steer_text, approved_go=approved_go)

    async def poll_all_channels(self, *, seed_if_empty: bool = False, debounced: bool = True) -> int:
        """Poll every enabled channel whose adapter is registered; return channels polled cleanly."""
        polled = 0
        for ch in self.store.list_channels():
            if not ch.enabled:
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
