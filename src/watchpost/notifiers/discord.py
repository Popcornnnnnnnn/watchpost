"""Discord DM NotifierAdapter (owner-only entrance with Discord reply binding)."""

from __future__ import annotations

from collections import deque
from typing import Any

import httpx

from watchpost.config import DiscordConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice


def _snowflake_key(msg_id: str) -> tuple[int, str]:
    clean = (msg_id or "").strip()
    return (int(clean) if clean.isdigit() else 0), clean


class DiscordNotifier:
    """Delivers Watchpost bubbles to the owner via Discord DM and polls replies."""

    name = "discord"

    def __init__(self, config: DiscordConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._token = config.bot_token.strip()
        self._owner_user_id = config.owner_user_id.strip()
        self._dm_channel_id = config.owner_dm_channel_id.strip()
        self._base_url = (config.base_url or "https://discord.com/api/v10").strip().rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._last_msg_id: str = ""
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=500)

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

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            raise RuntimeError("Discord bot_token must be configured")
        return {"Authorization": f"Bot {self._token}"}

    def _remember_seen(self, msg_id: str) -> None:
        if not msg_id or msg_id in self._seen_ids:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order.popleft()
            self._seen_ids.discard(oldest)
        self._seen_order.append(msg_id)
        self._seen_ids.add(msg_id)

    async def _ensure_dm_channel(self) -> str:
        if self._dm_channel_id:
            return self._dm_channel_id
        if not self._owner_user_id:
            raise RuntimeError("Discord owner_dm_channel_id or owner_user_id must be configured")
        resp = await self._client().post(
            f"{self._base_url}/users/@me/channels",
            json={"recipient_id": self._owner_user_id},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
        cid = str((payload or {}).get("id") or "").strip()
        if not cid:
            raise RuntimeError("Discord DM channel creation returned empty channel ID")
        self._dm_channel_id = cid
        return cid

    async def send_message(self, text: str) -> str:
        body = (text or "").strip()
        if not body:
            return ""
        dm_id = await self._ensure_dm_channel()
        resp = await self._client().post(
            f"{self._base_url}/channels/{dm_id}/messages",
            json={"content": body[:2000]},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
        msg_id = str((payload or {}).get("id") or "").strip()
        if msg_id:
            self._remember_seen(msg_id)
            if not self._last_msg_id or _snowflake_key(msg_id) > _snowflake_key(self._last_msg_id):
                self._last_msg_id = msg_id
        return msg_id

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
        attachments: list[str] | None = None,
    ) -> list[str]:
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
        tok = await self.send_message(msg)
        return [tok] if tok else []

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        del channel, thread
        tokens: list[str] = []
        for bubble in bubbles:
            tok = await self.send_message(bubble)
            if tok:
                tokens.append(tok)
        return tokens

    async def poll_owner_actions(self) -> list[OwnerAction]:
        if not self._token or not (self._dm_channel_id or self._owner_user_id):
            return []
        dm_id = await self._ensure_dm_channel()
        params: dict[str, Any] = {"limit": 20}
        if self._last_msg_id:
            params["after"] = self._last_msg_id
        resp = await self._client().get(
            f"{self._base_url}/channels/{dm_id}/messages",
            params=params,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        raw_items = resp.json() or []
        ordered = sorted(
            [m for m in raw_items if isinstance(m, dict) and str(m.get("id") or "").strip()],
            key=lambda m: _snowflake_key(str(m.get("id") or "")),
        )

        actions: list[OwnerAction] = []
        for msg in ordered:
            msg_id = str(msg.get("id") or "").strip()
            if not msg_id:
                continue
            self._last_msg_id = msg_id
            if msg_id in self._seen_ids:
                continue
            self._remember_seen(msg_id)

            author = msg.get("author") or {}
            if author.get("bot"):
                continue
            uid = str(author.get("id") or "").strip()
            if self._owner_user_id and uid != self._owner_user_id:
                continue

            text = str(msg.get("content") or "").strip()
            if not text:
                continue
            reply_token = str((msg.get("message_reference") or {}).get("message_id") or "").strip()
            action = classify_owner_command(text, notifier=self.name, reply_token=reply_token)
            if action is not None:
                actions.append(action)
        return actions
