"""Cross-platform ntfy.sh NotifierAdapter (1-way push + optional 2-way reply_topic)."""

from __future__ import annotations

from collections import deque
import json
from typing import Any

import httpx

from watchpost.config import NtfyConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice

_NTFY_PRIORITY_MAP = {
    "min": 1,
    "low": 2,
    "default": 3,
    "high": 4,
    "urgent": 5,
    "max": 5,
}


def _parse_ntfy_priority(raw: str) -> int | None:
    clean = (raw or "").strip().lower()
    if not clean or clean == "default":
        return None
    if clean in _NTFY_PRIORITY_MAP:
        return _NTFY_PRIORITY_MAP[clean]
    if clean.isdigit():
        val = int(clean)
        return max(1, min(val, 5))
    return None


class NtfyNotifier:
    """Delivers Watchpost bubbles to phone/desktop via ntfy and optionally polls reply_topic."""

    name = "ntfy"

    def __init__(self, config: NtfyConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._server_url = (config.server_url or "https://ntfy.sh").strip().rstrip("/")
        self._topic = config.topic.strip().strip("/")
        self._reply_topic = config.reply_topic.strip().strip("/")
        self._token = config.token.strip()
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._own_sent_ids: deque[str] = deque(maxlen=200)
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=500)
        self._since: str = "10m"

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

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _remember_seen(self, msg_id: str) -> None:
        if not msg_id or msg_id in self._seen_ids:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order.popleft()
            self._seen_ids.discard(oldest)
        self._seen_order.append(msg_id)
        self._seen_ids.add(msg_id)

    async def send_message(self, text: str, *, title: str = "Watchpost") -> str:
        if not self._topic:
            raise RuntimeError("ntfy topic must be configured")
        body = (text or "").strip()
        if not body:
            return ""
        # Use ntfy JSON publish so UTF-8 titles never trigger HTTP latin-1 header errors.
        payload: dict[str, Any] = {
            "topic": self._topic,
            "title": (title or "Watchpost")[:180],
            "message": body[:4096],
        }
        prio = _parse_ntfy_priority(self._config.priority)
        if prio is not None:
            payload["priority"] = prio
        if self._config.tags:
            payload["tags"] = [str(t) for t in self._config.tags if str(t).strip()]

        resp = await self._client().post(
            self._server_url,
            json=payload,
            headers=self._headers(),
        )
        resp.raise_for_status()
        msg_id = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                msg_id = str(data.get("id") or "")
        except ValueError:
            msg_id = ""
        if msg_id:
            self._own_sent_ids.append(msg_id)
        return msg_id or "ok"

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
        if not self._reply_topic:
            return []
        resp = await self._client().get(
            f"{self._server_url}/{self._reply_topic}/json",
            params={"poll": "1", "since": self._since},
            headers=self._headers(),
        )
        resp.raise_for_status()

        actions: list[OwnerAction] = []
        last_seen_id = ""
        for raw_line in resp.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("event") != "message":
                continue
            msg_id = str(obj.get("id") or "").strip()
            if msg_id:
                last_seen_id = msg_id
                if msg_id in self._seen_ids or msg_id in self._own_sent_ids:
                    continue
                self._remember_seen(msg_id)
            text = str(obj.get("message") or "").strip()
            if not text:
                continue
            action = classify_owner_command(text, notifier=self.name)
            if action is not None:
                actions.append(action)

        if last_seen_id:
            self._since = last_seen_id
        return actions
