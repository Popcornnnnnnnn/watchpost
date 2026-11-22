"""Cross-platform Telegram Bot NotifierAdapter (owner-only entrance)."""

from __future__ import annotations

from typing import Any

import httpx

from watchpost.config import TelegramConfig
from watchpost.models import ChatType, ChannelConfig, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice


async def detect_owner_chat_id(
    bot_token: str,
    *,
    base_url: str = "https://api.telegram.org",
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """Inspect recent Telegram getUpdates messages to discover the owner's numeric chat_id."""
    if not bot_token.strip():
        raise RuntimeError("Telegram bot_token is required")
    url = f"{base_url.rstrip('/')}/bot{bot_token.strip()}/getUpdates"
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http.get(url, params={"limit": 20, "timeout": 0})
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        seen: dict[str, dict[str, str]] = {}
        for upd in payload.get("result") or []:
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = str(chat.get("id") or "").strip()
            if not cid or cid in seen:
                continue
            seen[cid] = {
                "chat_id": cid,
                "type": str(chat.get("type") or ""),
                "username": str(chat.get("username") or ""),
                "name": " ".join(
                    p for p in [str(chat.get("first_name") or "").strip(), str(chat.get("last_name") or "").strip()] if p
                )
                or str(chat.get("title") or ""),
            }
        return list(seen.values())
    finally:
        if owns_client:
            await http.aclose()


class TelegramNotifier:
    """Delivers Watchpost bubbles to the owner via Telegram Bot API and polls replies."""

    name = "telegram"

    def __init__(self, config: TelegramConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._token = config.bot_token.strip()
        self._owner_chat_id = str(config.owner_chat_id or "").strip()
        self._base_url = f"{config.base_url.rstrip('/')}/bot{self._token}"
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._offset: int | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=max(15.0, float(self._config.poll_timeout_s) + 10.0))
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def send_message(self, text: str) -> str:
        if not self._token or not self._owner_chat_id:
            raise RuntimeError("Telegram bot_token and owner_chat_id must be configured")
        body = (text or "").strip()
        if not body:
            return ""
        resp = await self._client().post(
            f"{self._base_url}/sendMessage",
            json={
                "chat_id": self._owner_chat_id,
                "text": body[:4096],
                "disable_web_page_preview": True,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {payload}")
        msg_id = (payload.get("result") or {}).get("message_id")
        return str(msg_id) if msg_id is not None else ""

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
        if not self._token or not self._owner_chat_id:
            return []
        params: dict[str, Any] = {
            "timeout": max(0, int(self._config.poll_timeout_s)),
            "allowed_updates": '["message"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        resp = await self._client().get(f"{self._base_url}/getUpdates", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")

        actions: list[OwnerAction] = []
        for upd in payload.get("result") or []:
            upd_id = upd.get("update_id")
            if isinstance(upd_id, int):
                self._offset = max(self._offset or 0, upd_id + 1)

            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
            sender = msg.get("from") or {}
            if sender.get("is_bot"):
                continue
            # Strictly reject messages from any chat other than the configured owner
            if chat_id != self._owner_chat_id:
                continue

            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            reply_msg = msg.get("reply_to_message") or {}
            reply_token = str(reply_msg.get("message_id") or "")
            action = classify_owner_command(text, notifier=self.name, reply_token=reply_token)
            if action is not None:
                actions.append(action)
        return actions
