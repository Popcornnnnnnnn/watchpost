"""Cross-platform Pushover NotifierAdapter (iOS / Android / Desktop)."""

from __future__ import annotations

from typing import Any

import httpx

from watchpost.config import PushoverConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import format_start_notice


def _chunk_text(text: str, *, limit: int = 1024) -> list[str]:
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= limit:
        return [clean]
    chunks: list[str] = []
    remaining = clean
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


class PushoverNotifier:
    """Delivers Watchpost summaries via the Pushover Messages API."""

    name = "pushover"

    def __init__(self, config: PushoverConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._app_token = config.app_token.strip()
        self._user_key = config.user_key.strip()
        self._base_url = (config.base_url or "https://api.pushover.net").strip().rstrip("/")
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

    async def send_message(self, text: str, *, title: str = "Watchpost") -> str:
        if not self._app_token or not self._user_key:
            raise RuntimeError("Pushover app_token and user_key must be configured")
        chunks = _chunk_text(text, limit=1024)
        if not chunks:
            return ""
        last_token = ""
        for idx, chunk in enumerate(chunks):
            part_title = title[:250] if len(chunks) == 1 else f"{title[:240]} ({idx + 1}/{len(chunks)})"
            form: dict[str, Any] = {
                "token": self._app_token,
                "user": self._user_key,
                "title": part_title or "Watchpost",
                "message": chunk,
                "priority": str(int(self._config.priority)),
            }
            if self._config.device.strip():
                form["device"] = self._config.device.strip()
            if self._config.sound.strip():
                form["sound"] = self._config.sound.strip()

            resp = await self._client().post(f"{self._base_url}/1/messages.json", data=form)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("status") != 1:
                raise RuntimeError(f"Pushover send failed: {payload}")
            if isinstance(payload, dict):
                last_token = str(payload.get("request") or "ok")
        return last_token or "ok"

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
        joined = "\n\n".join(b.strip() for b in bubbles if b and b.strip()).strip()
        if not joined:
            return []
        tok = await self.send_message(joined, title=channel.name or "Watchpost")
        return [tok] if tok else []

    async def poll_owner_actions(self) -> list[OwnerAction]:
        return []
