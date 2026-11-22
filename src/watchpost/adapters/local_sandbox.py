"""File-backed LocalSandboxAdapter for interactive two-way testing as external colleagues."""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from watchpost.models import (
    AutonomyMode,
    ChannelConfig,
    ChatType,
    MediaArtifact,
    MediaRef,
    NormalizedMessage,
    TriggerMode,
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def default_sandbox_channels(
    *,
    autonomy: AutonomyMode = AutonomyMode.diagnose_only,
    persona_text: str = "",
) -> list[ChannelConfig]:
    """Build default isolated DM and @me group channels for sandbox simulation."""
    dm_persona = persona_text or (
        "Role: Colleague / External Stakeholder (Simulated DM).\n"
        "Scope: Ad-hoc technical questions, status checks, or fault investigations."
    )
    grp_persona = persona_text or (
        "Role: Project / Ops Support Group (Simulated @me Group).\n"
        "Pattern: Background chatter and media handoffs followed by @owner escalation."
    )
    return [
        ChannelConfig(
            id="sandbox_dm",
            adapter="sandbox",
            external_chat_id="sandbox_dm",
            name="Alex (Simulated DM)",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
            autonomy_mode=autonomy,
            persona_text=dm_persona,
        ),
        ChannelConfig(
            id="sandbox_group",
            adapter="sandbox",
            external_chat_id="sandbox_group",
            name="Ops Support (Simulated @me Group)",
            chat_type=ChatType.group,
            trigger_mode=TriggerMode.mention_owner,
            autonomy_mode=autonomy,
            persona_text=grp_persona,
        ),
    ]


class LocalSandboxAdapter:
    """Append-only JSONL-backed ChannelAdapter simulating inbound colleagues/groups locally."""

    name = "sandbox"

    def __init__(
        self,
        sandbox_dir: Path | str,
        *,
        owner_id: str = "owner",
        owner_name: str = "You",
    ) -> None:
        self.sandbox_dir = Path(sandbox_dir).expanduser()
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_path = self.sandbox_dir / "sandbox_inbox.jsonl"
        self.owner_id = owner_id
        self.owner_name = owner_name

    def _read_all_records(self) -> list[dict[str, Any]]:
        if not self.inbox_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in self.inbox_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("message_id"):
                records.append(rec)
        return records

    def append_message(
        self,
        channel_id: str,
        *,
        sender_name: str,
        text: str = "",
        msg_type: str = "text",
        reply_to_id: str = "",
        mentions_owner: bool = False,
        mentions_all: bool = False,
        image_path: Path | str | None = None,
        audio_path: Path | str | None = None,
        file_path: Path | str | None = None,
        deleted: bool = False,
    ) -> NormalizedMessage:
        records = self._read_all_records()
        next_pos = max((int(r.get("position") or 0) for r in records), default=0) + 1
        last_ms = max((int(r.get("create_ms") or 0) for r in records), default=0)
        now_ms = max(int(time.time() * 1000), last_ms + 1)
        ts_str = datetime.fromtimestamp(now_ms / 1000.0).strftime("%H:%M:%S")

        refs: list[MediaRef] = []
        inferred_type = msg_type
        if image_path is not None:
            p = Path(image_path).expanduser()
            refs.append(
                MediaRef(
                    kind="image",
                    key=f"img_{next_pos}",
                    ext=p.suffix or ".png",
                    extra={"local_path": str(p), "file_name": p.name},
                )
            )
            if inferred_type == "text":
                inferred_type = "image"
        if audio_path is not None:
            p = Path(audio_path).expanduser()
            refs.append(
                MediaRef(
                    kind="audio",
                    key=f"aud_{next_pos}",
                    ext=p.suffix or ".opus",
                    extra={"local_path": str(p), "file_name": p.name},
                )
            )
            if inferred_type == "text":
                inferred_type = "audio"
        if file_path is not None:
            p = Path(file_path).expanduser()
            refs.append(
                MediaRef(
                    kind="file",
                    key=f"file_{next_pos}",
                    ext=p.suffix or ".bin",
                    extra={"local_path": str(p), "file_name": p.name},
                )
            )
            if inferred_type == "text":
                inferred_type = "file"

        clean = text.strip()
        if deleted:
            clean = "[Recalled]"
            refs = []
        elif not clean and refs:
            clean = f"[{inferred_type.capitalize()}]"

        sender_slug = _SAFE_NAME_RE.sub("_", sender_name.strip().lower()).strip("_") or "sender"
        msg = NormalizedMessage(
            message_id=f"sb_{next_pos}",
            channel_id=channel_id,
            position=next_pos,
            create_ms=now_ms,
            create_time=ts_str,
            sender_id=f"sb_user_{sender_slug}",
            sender_name=sender_name.strip() or "Colleague",
            sender_type="user",
            is_owner=False,
            is_bot=False,
            deleted=deleted,
            msg_type=inferred_type,
            raw_content=clean,
            clean_text=clean,
            reply_to_id=reply_to_id.strip(),
            mentions_owner=mentions_owner,
            mentions_all=mentions_all,
            media_refs=refs,
        )
        payload = msg.model_dump(mode="json")
        payload["chat_id"] = channel_id
        with self.inbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return msg

    def recall_message(
        self,
        channel_id: str,
        target_msg_id: str,
        *,
        sender_name: str = "System",
    ) -> NormalizedMessage:
        records = self._read_all_records()
        next_pos = max((int(r.get("position") or 0) for r in records), default=0) + 1
        last_ms = max((int(r.get("create_ms") or 0) for r in records), default=0)
        now_ms = max(int(time.time() * 1000), last_ms + 1)
        ts_str = datetime.fromtimestamp(now_ms / 1000.0).strftime("%H:%M:%S")
        msg = NormalizedMessage(
            message_id=f"sb_{next_pos}",
            channel_id=channel_id,
            position=next_pos,
            create_ms=now_ms,
            create_time=ts_str,
            sender_id="sb_system",
            sender_name=sender_name,
            sender_type="user",
            deleted=True,
            msg_type="text",
            clean_text="[Recalled]",
            reply_to_id=target_msg_id.strip(),
        )
        payload = msg.model_dump(mode="json")
        payload["chat_id"] = channel_id
        payload["recall_target_id"] = target_msg_id.strip()
        with self.inbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return msg

    def list_messages(self, channel_id: str, *, limit: int = 50) -> list[NormalizedMessage]:
        records = [r for r in self._read_all_records() if r.get("chat_id") == channel_id]
        recalled_targets = {str(r.get("recall_target_id") or "") for r in records if r.get("recall_target_id")}
        msgs: list[NormalizedMessage] = []
        for r in records[-limit:]:
            item = dict(r)
            item.pop("chat_id", None)
            item.pop("recall_target_id", None)
            msg = NormalizedMessage.model_validate(item)
            if msg.message_id in recalled_targets:
                msg = msg.model_copy(update={"deleted": True, "clean_text": "[Recalled]", "media_refs": []})
            msgs.append(msg)
        return sorted(msgs, key=lambda m: (m.position, m.create_ms))

    async def list_available_chats(self) -> list[dict[str, str]]:
        return [
            {"external_chat_id": ch.external_chat_id, "name": ch.name, "chat_type": ch.chat_type.value}
            for ch in default_sandbox_channels()
        ]

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        return self.list_messages(channel.external_chat_id, limit=limit)

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        wanted = set(message_ids)
        return [m for m in self.list_messages(channel.external_chat_id, limit=500) if m.message_id in wanted]

    async def download_media(
        self,
        channel: ChannelConfig,
        message: NormalizedMessage,
        dest_dir: Path,
    ) -> list[MediaArtifact]:
        del channel
        target_dir = Path(dest_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[MediaArtifact] = []
        for ref in message.media_refs:
            raw_src = str(ref.extra.get("local_path") or "").strip()
            if not raw_src:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error="Missing local_path"))
                continue
            src = Path(raw_src).expanduser()
            if not src.is_file():
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=f"File not found: {src}"))
                continue
            safe_name = _SAFE_NAME_RE.sub("_", Path(str(ref.extra.get("file_name") or src.name)).name).strip("._")
            if not safe_name:
                safe_name = f"{ref.key}{ref.ext or '.bin'}"
            dst = target_dir / safe_name
            shutil.copyfile(src, dst)
            artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(dst)))
        return artifacts
