"""iOS Bark push NotifierAdapter with 1-tap clipboard draft copy."""

from __future__ import annotations

from typing import Any

import httpx

from watchpost.config import BarkConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import format_start_notice

_DRAFT_LEAD_INS = ("下面这条可直接复制回复：", "Suggested reply to copy:")


def _split_summary_and_draft(bubbles: list[str]) -> tuple[str, str]:
    clean_bubbles = [b.strip() for b in bubbles if b and b.strip()]
    if len(clean_bubbles) >= 2 and clean_bubbles[-2].rstrip().endswith(_DRAFT_LEAD_INS):
        return "\n\n".join(clean_bubbles[:-1]).strip(), clean_bubbles[-1]
    return "\n\n".join(clean_bubbles).strip(), ""


class BarkNotifier:
    """Delivers Watchpost alerts to iPhone via Bark and attaches clean drafts to `copy`."""

    name = "bark"

    def __init__(self, config: BarkConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._server_url = (config.server_url or "https://api.day.app").strip().rstrip("/")
        self._device_key = config.device_key.strip().strip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=15.0)
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def send_message(
        self,
        text: str,
        *,
        title: str = "Watchpost",
        copy_text: str = "",
    ) -> str:
        if not self._device_key:
            raise RuntimeError("Bark device_key must be configured")
        body = (text or "").strip()
        if not body:
            return ""

        payload: dict[str, Any] = {
            "title": (title or "Watchpost")[:180],
            "body": body[:4000],
            "group": self._config.group.strip() or "Watchpost",
        }
        if self._config.sound.strip():
            payload["sound"] = self._config.sound.strip()
        if self._config.icon.strip():
            payload["icon"] = self._config.icon.strip()
        if self._config.level.strip():
            payload["level"] = self._config.level.strip()
        if copy_text.strip():
            payload["copy"] = copy_text.strip()
            if self._config.auto_copy:
                payload["autoCopy"] = "1"

        resp = await self._client().post(
            f"{self._server_url}/{self._device_key}",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("code", 200) != 200:
            raise RuntimeError(f"Bark push failed: {data}")
        if isinstance(data, dict) and data.get("timestamp") is not None:
            return str(data["timestamp"])
        return "ok"

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
        attachments: list[str] | None = None,
    ) -> list[str]:
        if not self._config.notify_on_start:
            return []
        label = (
            f"{channel.name} · {thread.sender_name}"
            if (channel.chat_type == ChatType.group and thread.sender_name and thread.sender_name != "Owner")
            else channel.name
        )
        msg = format_start_notice(
            label,
            preview,
            adapter=channel.adapter,
            is_steer=is_steer,
            has_images=bool(attachments),
        )
        tok = await self.send_message(msg, title=channel.name or "Watchpost")
        return [tok] if tok else []

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        del thread
        summary, draft = _split_summary_and_draft(bubbles)
        if draft:
            body = f"{summary}\n\n{draft}".strip() if summary else draft
            tok = await self.send_message(
                body,
                title=channel.name or "Watchpost",
                copy_text=draft,
            )
            return [tok] if tok else []
        if not summary:
            return []
        tok = await self.send_message(summary, title=channel.name or "Watchpost")
        return [tok] if tok else []

    async def poll_owner_actions(self) -> list[OwnerAction]:
        return []
