"""Offline channel simulator adapter for testing and interactive demos."""

from __future__ import annotations

import time
from pathlib import Path

from watchpost.models import (
    AutonomyMode,
    ChannelConfig,
    ChatType,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
    TriggerMode,
)


def default_demo_channels() -> list[ChannelConfig]:
    return [
        ChannelConfig(
            id="sim_alex_dm",
            adapter="simulator",
            external_chat_id="sim_chat_alex",
            name="Alex (Release DM)",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=AutonomyMode.diagnose_only,
            persona_text=(
                "Role: Backend Release Engineer.\n"
                "Scope: API & Worker v2.1 deployments and rollout status checks.\n"
                "Style: Asks brief rollout and schedule questions alongside real fault escalations."
            ),
            whisper_prompt="Node 1 Node 2 v2.1 rollout worker api",
        ),
        ChannelConfig(
            id="sim_ops_alerts_group",
            adapter="simulator",
            external_chat_id="sim_chat_ops_group",
            name="Ops Alerts (@me Group)",
            chat_type=ChatType.group,
            trigger_mode=TriggerMode.mention_owner,
            autonomy_mode=AutonomyMode.diagnose_only,
            persona_text=(
                "Role: Backend Services Support Group.\n"
                "Members: Sam (On-call Eng), Alex (Support Engineer), Lead.\n"
                "Pattern: Overnight alert screenshots -> morning @Lead escalation."
            ),
            whisper_prompt="Ops Alerts Worker-02 API 503 timeout pool",
        ),
    ]


class SimulatorAdapter:
    """In-memory mock adapter modeling realistic engineering chat streams."""

    name = "simulator"

    def __init__(self, *, now_ms: int | None = None) -> None:
        self._now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.downloaded_ids: list[str] = []
        self._history: dict[str, list[NormalizedMessage]] = {}
        self._build_default_streams()

    def _build_default_streams(self) -> None:
        t = self._now_ms
        self._history["sim_chat_alex"] = [
            NormalizedMessage(
                message_id="dm_0",
                position=10,
                create_ms=t - 180_000,
                create_time="09:40:00",
                sender_id="user_alex",
                sender_name="Alex",
                clean_text="Morning! Starting Node 1 rollout checks.",
            ),
            NormalizedMessage(
                message_id="dm_1",
                position=11,
                create_ms=t - 120_000,
                create_time="09:42:00",
                sender_id="user_alex",
                sender_name="Alex",
                clean_text="1号节点这边 v2.1 更新完了吗？能继续测试了吗？",
            ),
            NormalizedMessage(
                message_id="dm_2_ack",
                position=12,
                create_ms=t - 60_000,
                create_time="09:43:00",
                sender_id="user_alex",
                sender_name="Alex",
                clean_text="OKOK",
            ),
        ]

        t_overnight = t - 9 * 3600 * 1000
        self._history["sim_chat_ops_group"] = [
            NormalizedMessage(
                message_id="grp_0_seed",
                position=100,
                create_ms=t - 10 * 3600 * 1000,
                create_time="23:30:00",
                sender_id="user_casey",
                sender_name="Casey",
                clean_text="Night shift starting",
            ),
            # Overnight Bucket-1 handoff: Sam sends screenshot + mentions @Alex
            NormalizedMessage(
                message_id="grp_1_photo",
                position=101,
                create_ms=t_overnight,
                create_time="01:02:00",
                sender_id="user_sam",
                sender_name="Sam",
                msg_type="image",
                media_refs=[MediaRef(kind="image", key="sim_trace_worker2", ext=".png")],
            ),
            NormalizedMessage(
                message_id="grp_2_handoff",
                position=102,
                create_ms=t_overnight + 15_000,
                create_time="01:02:15",
                sender_id="user_sam",
                sender_name="Sam",
                clean_text="Worker 2 pool returned 503 and timed out @Alex",
                mentions=[Mention(id="user_alex", name="Alex")],
            ),
            # Unrelated Bucket-2 background with undecoded image (must NOT be downloaded)
            NormalizedMessage(
                message_id="grp_3_noise_img",
                position=103,
                create_ms=t - 3 * 3600 * 1000,
                create_time="07:05:00",
                sender_id="user_casey",
                sender_name="Casey",
                msg_type="image",
                media_refs=[MediaRef(kind="image", key="sim_lunch_noise", ext=".png")],
            ),
            # Recalled message (must be ignored completely)
            NormalizedMessage(
                message_id="grp_4_recalled",
                position=104,
                create_ms=t - 120_000,
                create_time="09:41:00",
                sender_id="user_alex",
                sender_name="Alex",
                deleted=True,
                clean_text="[Recalled]",
            ),
            # Morning trigger: Alex @Lead referencing overnight complaint
            NormalizedMessage(
                message_id="grp_5_ping",
                position=105,
                create_ms=t - 30_000,
                create_time="09:44:00",
                sender_id="user_alex",
                sender_name="Alex",
                clean_text="@Lead 帮忙看一下昨晚 Worker 2 反馈的 503 超时问题",
                mentions=[Mention(id="user_owner", name="Lead")],
                mentions_owner=True,
            ),
        ]

    async def list_available_chats(self) -> list[dict[str, str]]:
        return [
            {"external_chat_id": ch.external_chat_id, "name": ch.name, "chat_type": ch.chat_type.value}
            for ch in default_demo_channels()
        ]

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        msgs = self._history.get(channel.external_chat_id, [])
        return [m.model_copy(deep=True) for m in msgs[-limit:]]

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        wanted = set(message_ids)
        return [
            m.model_copy(deep=True)
            for m in self._history.get(channel.external_chat_id, [])
            if m.message_id in wanted
        ]

    async def download_media(
        self,
        channel: ChannelConfig,
        message: NormalizedMessage,
        dest_dir: Path,
    ) -> list[MediaArtifact]:
        self.downloaded_ids.append(message.message_id)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[MediaArtifact] = []
        for ref in message.media_refs:
            out_path = dest_dir / f"{ref.key}{ref.ext or '.bin'}"
            out_path.write_bytes(b"SIM_MEDIA_PLACEHOLDER")
            artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(out_path)))
        return artifacts
