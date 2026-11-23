"""WeCom (Enterprise WeChat) application push NotifierAdapter."""

from __future__ import annotations

import time

import httpx

from watchpost.adapters.wecom import fetch_wecom_access_token
from watchpost.config import WeComConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import format_start_notice


class WeComNotifier:
    """Delivers Watchpost summaries to the owner's WeCom app via `message/send`."""

    name = "wecom"

    def __init__(self, config: WeComConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._corp_id = config.corp_id.strip()
        self._corp_secret = config.corp_secret.strip()
        self._agent_id = int(config.agent_id or 0)
        self._owner_user_id = config.owner_user_id.strip()
        self._base_url = (config.base_url or "https://qyapi.weixin.qq.com/cgi-bin").strip().rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._token_value: str = ""
        self._token_expires_at: float = 0.0

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

    async def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._token_value and now < self._token_expires_at - 60:
            return self._token_value
        token, ttl = await fetch_wecom_access_token(
            self._client(),
            self._base_url,
            self._corp_id,
            self._corp_secret,
        )
        self._token_value = token
        self._token_expires_at = now + ttl
        return token

    async def send_message(self, text: str) -> str:
        if not self._owner_user_id or self._agent_id <= 0:
            raise RuntimeError("WeCom owner_user_id and positive agent_id must be configured")
        body = (text or "").strip()
        if not body:
            return ""
        token = await self._get_access_token()
        resp = await self._client().post(
            f"{self._base_url}/message/send",
            params={"access_token": token},
            json={
                "touser": self._owner_user_id,
                "msgtype": "text",
                "agentid": self._agent_id,
                "text": {"content": body[:2048]},
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("errcode", 0) != 0:
            raise RuntimeError(f"WeCom message/send failed: {payload}")
        return str(payload.get("msgid") or "ok")

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
        return []
