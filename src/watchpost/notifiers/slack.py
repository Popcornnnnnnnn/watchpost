"""Slack DM NotifierAdapter (owner-only entrance with thread reply steering)."""

from __future__ import annotations

from collections import deque
from typing import Any

import httpx

from watchpost.config import SlackConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice


def _slack_ts_key(ts: str) -> tuple[float, str]:
    try:
        return float(ts), ts
    except ValueError:
        return 0.0, ts


class SlackNotifier:
    """Delivers Watchpost alerts to the owner via Slack DM and polls replies/threads."""

    name = "slack"

    def __init__(self, config: SlackConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._token = config.bot_token.strip()
        self._owner_user_id = config.owner_user_id.strip()
        self._dm_channel_id = config.owner_dm_channel_id.strip()
        self._base_url = (config.base_url or "https://slack.com/api").strip().rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._sent_ts: deque[str] = deque(maxlen=40)
        self._seen_ts: set[str] = set()
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
            raise RuntimeError("Slack bot_token must be configured")
        return {"Authorization": f"Bearer {self._token}"}

    def _remember_seen(self, ts: str) -> None:
        if not ts or ts in self._seen_ts:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order.popleft()
            self._seen_ts.discard(oldest)
        self._seen_order.append(ts)
        self._seen_ts.add(ts)

    async def _ensure_dm_channel(self) -> str:
        if self._dm_channel_id:
            return self._dm_channel_id
        if not self._owner_user_id:
            raise RuntimeError("Slack owner_dm_channel_id or owner_user_id must be configured")
        resp = await self._client().post(
            f"{self._base_url}/conversations.open",
            json={"users": self._owner_user_id},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Slack conversations.open failed: {payload}")
        cid = str((payload.get("channel") or {}).get("id") or "").strip()
        if not cid:
            raise RuntimeError("Slack conversations.open returned empty channel ID")
        self._dm_channel_id = cid
        return cid

    async def send_message(self, text: str) -> str:
        body = (text or "").strip()
        if not body:
            return ""
        dm_id = await self._ensure_dm_channel()
        resp = await self._client().post(
            f"{self._base_url}/chat.postMessage",
            json={
                "channel": dm_id,
                "text": body[:39000],
                "unfurl_links": False,
                "unfurl_media": False,
            },
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Slack chat.postMessage failed: {payload}")
        ts = str(payload.get("ts") or "").strip()
        if ts:
            self._sent_ts.append(ts)
            self._remember_seen(ts)
        return ts

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
        resp = await self._client().get(
            f"{self._base_url}/conversations.history",
            params={"channel": dm_id, "limit": 20},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Slack conversations.history failed: {payload}")

        raw_by_ts: dict[str, dict[str, Any]] = {}
        parents_to_check: list[str] = list(self._sent_ts)[-10:]

        for msg in payload.get("messages") or []:
            ts = str(msg.get("ts") or "").strip()
            if not ts:
                continue
            raw_by_ts[ts] = msg
            if int(msg.get("reply_count") or 0) > 0 and ts not in parents_to_check:
                parents_to_check.append(ts)

        for parent_ts in parents_to_check:
            try:
                rep = await self._client().get(
                    f"{self._base_url}/conversations.replies",
                    params={"channel": dm_id, "ts": parent_ts, "limit": 20},
                    headers=self._auth_headers(),
                )
                rep.raise_for_status()
                rep_data = rep.json()
                if not isinstance(rep_data, dict) or not rep_data.get("ok"):
                    continue
                for rmsg in rep_data.get("messages") or []:
                    rts = str(rmsg.get("ts") or "").strip()
                    if rts and rts not in raw_by_ts:
                        raw_by_ts[rts] = rmsg
            except Exception:
                continue

        actions: list[OwnerAction] = []
        for msg in sorted(raw_by_ts.values(), key=lambda m: _slack_ts_key(str(m.get("ts") or ""))):
            ts = str(msg.get("ts") or "").strip()
            if not ts or ts in self._seen_ts:
                continue
            self._remember_seen(ts)

            if msg.get("bot_id") or msg.get("subtype"):
                continue
            uid = str(msg.get("user") or "").strip()
            if self._owner_user_id and uid != self._owner_user_id:
                continue

            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            thread_ts = str(msg.get("thread_ts") or "").strip()
            reply_token = thread_ts if (thread_ts and thread_ts != ts) else ""
            action = classify_owner_command(text, notifier=self.name, reply_token=reply_token)
            if action is not None:
                actions.append(action)
        return actions
