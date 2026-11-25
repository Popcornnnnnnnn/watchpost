"""Tests for Watchpost noise filters and 2-bucket group context engine."""

from __future__ import annotations

import asyncio

from watchpost.context import assemble_group_context, match_group_thread
from watchpost.filters import is_small_talk_only, should_trigger
from watchpost.models import (
    ChannelConfig,
    ChatType,
    MediaRef,
    Mention,
    NormalizedMessage,
    ThreadRecord,
    TriggerMode,
)


def test_trigger_and_small_talk_filters() -> None:
    grp = ChannelConfig(
        id="g1",
        adapter="slack",
        external_chat_id="C1",
        name="Backend Ops Group",
        chat_type=ChatType.group,
        trigger_mode=TriggerMode.mention_owner,
    )
    assert not should_trigger(grp, NormalizedMessage(message_id="m1", is_owner=True, mentions_owner=True))
    assert not should_trigger(grp, NormalizedMessage(message_id="m2", deleted=True, mentions_owner=True))
    assert not should_trigger(grp, NormalizedMessage(message_id="m3", msg_type="system", mentions_owner=True))
    assert not should_trigger(grp, NormalizedMessage(message_id="m4", is_bot=True, mentions_owner=True))
    assert not should_trigger(grp, NormalizedMessage(message_id="m5", mentions_all=True, mentions_owner=False))
    assert should_trigger(grp, NormalizedMessage(message_id="m6", mentions_owner=True, clean_text="@Lead help"))

    assert is_small_talk_only([NormalizedMessage(message_id="a1", clean_text="OKOK")], [], False)
    assert is_small_talk_only([NormalizedMessage(message_id="a2", clean_text="[Facepalm]")], [], False)
    assert is_small_talk_only([NormalizedMessage(message_id="a3", msg_type="sticker")], [], False)
    assert is_small_talk_only(
        [NormalizedMessage(message_id="a4", clean_text="@Lead 收到", mentions_owner=True, mentions=[Mention(id="o", name="Lead")])],
        [],
        False,
    )
    assert is_small_talk_only([NormalizedMessage(message_id="a5", clean_text="？")], [], False)

    # Keep questions, bare @Lead pings, media, active sessions, or answers to recent owner question
    assert not is_small_talk_only([NormalizedMessage(message_id="q1", clean_text="Node 1 updated?")], [], False)
    assert not is_small_talk_only(
        [NormalizedMessage(message_id="q2", clean_text="@Lead", mentions_owner=True, mentions=[Mention(id="o", name="Lead")])],
        [],
        False,
    )
    assert not is_small_talk_only(
        [NormalizedMessage(message_id="q3", msg_type="image", media_refs=[MediaRef(kind="image", key="k1")])],
        [],
        False,
    )
    assert not is_small_talk_only([NormalizedMessage(message_id="q4", clean_text="好的")], [], True)
    assert not is_small_talk_only(
        [NormalizedMessage(message_id="q5", clean_text="好的", create_ms=200_000)],
        [NormalizedMessage(message_id="o1", is_owner=True, create_ms=150_000, clean_text="好了吗？")],
        False,
    )


def test_overnight_handoff_two_bucket_and_depth_chain() -> None:
    now_ms = 2_000_000_000_000
    remote_root = NormalizedMessage(
        message_id="r0",
        position=50,
        create_ms=now_ms - 20 * 3600 * 1000,
        sender_id="u_lead",
        sender_name="Lead",
        clean_text="Watch Worker 2 pool latency overnight",
    )
    recent = [
        NormalizedMessage(
            message_id="h_img",
            position=101,
            create_ms=now_ms - 9 * 3600 * 1000,
            sender_id="u_sam",
            sender_name="Sam",
            msg_type="image",
            media_refs=[MediaRef(kind="image", key="worker2_img")],
        ),
        NormalizedMessage(
            message_id="h_txt",
            position=102,
            create_ms=now_ms - 9 * 3600 * 1000 + 20_000,
            sender_id="u_sam",
            sender_name="Sam",
            clean_text="Worker 2 stalled @Alex",
            reply_to_id="r0",
            mentions=[Mention(id="u_alex", name="Alex")],
        ),
        NormalizedMessage(
            message_id="bg_noise",
            position=103,
            create_ms=now_ms - 2 * 3600 * 1000,
            sender_id="u_casey",
            sender_name="Casey",
            msg_type="image",
            media_refs=[MediaRef(kind="image", key="lunch_img")],
        ),
        NormalizedMessage(
            message_id="burst_1",
            position=104,
            create_ms=now_ms - 40_000,
            sender_id="u_alex",
            sender_name="Alex",
            clean_text="Same symptom this morning",
        ),
    ]
    batch = [
        NormalizedMessage(
            message_id="trig",
            position=105,
            create_ms=now_ms,
            sender_id="u_alex",
            sender_name="Alex",
            clean_text="@Lead please check the overnight issue",
            mentions_owner=True,
        )
    ]

    async def _fetch(ids: list[str]) -> list[NormalizedMessage]:
        return [remote_root] if "r0" in ids else []

    ctx = asyncio.run(assemble_group_context(batch, recent, fetch_by_ids=_fetch))
    assert [m.message_id for m in ctx.direct_messages] == ["r0", "h_img", "h_txt", "burst_1"]
    assert ctx.link_reasons["r0"] == "handoff_mention"
    assert ctx.link_reasons["h_img"] == "handoff_mention"
    assert ctx.link_reasons["burst_1"] == "same_sender_burst"
    assert [m.message_id for m in ctx.background_messages] == ["bg_noise"]


def test_match_group_thread_rules() -> None:
    now = 5_000_000.0
    t_old_reply = ThreadRecord(
        id="th_reply",
        channel_id="g1",
        backend="mock",
        backend_thread_ref="r1",
        sender_id="u_sam",
        message_ids=["m10", "m11"],
        updated_at=now - 8 * 3600,
    )
    t_recent_sender = ThreadRecord(
        id="th_sender",
        channel_id="g1",
        backend="mock",
        backend_thread_ref="r2",
        sender_id="u_alex",
        message_ids=["m20"],
        updated_at=now - 120,
    )

    # Reply chain within 24h wins even for a different sender
    matched = match_group_thread(
        [t_old_reply, t_recent_sender],
        [NormalizedMessage(message_id="m99", sender_id="u_casey", reply_to_id="m11")],
        [],
        now,
    )
    assert matched is not None and matched.id == "th_reply"

    # Same sender within 15m reuses thread
    matched_sender = match_group_thread(
        [t_old_reply, t_recent_sender],
        [NormalizedMessage(message_id="m100", sender_id="u_alex")],
        [],
        now,
    )
    assert matched_sender is not None and matched_sender.id == "th_sender"

    # Unrelated sender starts a new thread
    assert (
        match_group_thread(
            [t_old_reply, t_recent_sender],
            [NormalizedMessage(message_id="m101", sender_id="u_new")],
            [],
            now,
        )
        is None
    )
