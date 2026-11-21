"""Watchpost orchestration engine connecting channels, filters, context, debouncer, backends, and notifiers."""

from __future__ import annotations

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
    ThreadEvent,
    ThreadRecord,
    ThreadStatus,
    TriageMode,
)
from watchpost.ports import AgentBackend, ChannelAdapter, MediaProcessor, NotifierAdapter
from watchpost.prompts import build_new_thread_prompt, build_owner_steer_prompt, build_steer_prompt
from watchpost.store import WatchpostStore


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

        for ntf in self.notifiers:
            tokens = await ntf.notify_completion(channel, record, all_bubbles)
            for tok in tokens:
                self.store.bind_outbound(ntf.name, tok, record.id)

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
        return updated

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
