"""Telegram inbound ChannelAdapter and shared single-stream TelegramUpdateHub."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from pathlib import Path
import re
from typing import Any

import httpx

from watchpost.config import TelegramConfig
from watchpost.models import (
    ChannelConfig,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
)

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _format_ts_ms(create_ms: int) -> str:
    if create_ms <= 0:
        return ""
    return datetime.fromtimestamp(create_ms / 1000.0).strftime("%H:%M:%S")


def _sender_display_name(sender: dict[str, Any]) -> str:
    first = str(sender.get("first_name") or "").strip()
    last = str(sender.get("last_name") or "").strip()
    full = " ".join(p for p in (first, last) if p).strip()
    return full or str(sender.get("username") or sender.get("id") or "Unknown").strip()


def _chat_display_name(chat: dict[str, Any]) -> str:
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    first = str(chat.get("first_name") or "").strip()
    last = str(chat.get("last_name") or "").strip()
    full = " ".join(p for p in (first, last) if p).strip()
    return full or str(chat.get("username") or chat.get("id") or "Telegram").strip()


def _sanitize_filename(name: str, *, fallback: str, default_ext: str) -> str:
    base = Path(name or "").name.strip()
    base = _SAFE_STEM_RE.sub("_", base).strip("._")
    if not base:
        base = _SAFE_STEM_RE.sub("_", fallback) or "artifact"
    if "." not in base and default_ext:
        base = f"{base}.{default_ext.lstrip('.')}"
    return base[:120]


class TelegramUpdateHub:
    """Shares a single Telegram `getUpdates` cursor safely across notifier and inbound adapter."""

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://api.telegram.org",
        poll_timeout_s: int = 0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = bot_token.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._api_url = f"{self._base_url}/bot{self._token}"
        self._file_base = f"{self._base_url}/file/bot{self._token}"
        self._poll_timeout_s = max(0, int(poll_timeout_s))
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._offset: int | None = None
        self._owner_queues: dict[str, deque[dict[str, Any]]] = {}
        self._chat_messages: dict[str, list[dict[str, Any]]] = {}
        self._seen_msg_ids: dict[str, set[str]] = {}
        self._known_chats: dict[str, dict[str, str]] = {}

    @property
    def token(self) -> str:
        return self._token

    @property
    def base_url(self) -> str:
        return self._base_url

    def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=max(15.0, float(self._poll_timeout_s) + 10.0))
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    def _record_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        cid = str(chat.get("id") or "").strip()
        mid = str(msg.get("message_id") or "").strip()
        if not cid or not mid:
            return

        ctype = str(chat.get("type") or "").strip()
        self._known_chats[cid] = {
            "external_chat_id": cid,
            "name": _chat_display_name(chat),
            "chat_type": "p2p" if ctype == "private" else "group",
        }

        seen_set = self._seen_msg_ids.setdefault(cid, set())
        if mid in seen_set:
            return
        seen_set.add(mid)

        bucket = self._chat_messages.setdefault(cid, [])
        bucket.append(msg)
        if len(bucket) > 500:
            dropped = bucket.pop(0)
            dropped_id = str(dropped.get("message_id") or "").strip()
            seen_set.discard(dropped_id)

        queue = self._owner_queues.get(cid)
        if queue is None:
            queue = deque(maxlen=200)
            self._owner_queues[cid] = queue
        queue.append(msg)

    async def sync(self) -> int:
        if not self._token:
            return 0
        async with self._lock:
            params: dict[str, Any] = {
                "timeout": self._poll_timeout_s,
                "allowed_updates": '["message"]',
            }
            if self._offset is not None:
                params["offset"] = self._offset
            resp = await self._client().get(f"{self._api_url}/getUpdates", params=params)
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or not payload.get("ok"):
                raise RuntimeError(f"Telegram getUpdates failed: {payload}")
            count = 0
            for upd in payload.get("result") or []:
                upd_id = upd.get("update_id")
                if isinstance(upd_id, int):
                    self._offset = max(self._offset or 0, upd_id + 1)
                msg = upd.get("message") or upd.get("edited_message") or {}
                if isinstance(msg, dict) and msg:
                    self._record_message(msg)
                    count += 1
            return count

    async def poll_owner_messages(self, owner_chat_id: str) -> list[dict[str, Any]]:
        await self.sync()
        queue = self._owner_queues.get(owner_chat_id.strip())
        if not queue:
            return []
        drained = list(queue)
        queue.clear()
        return drained

    async def get_chat_messages(self, chat_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        await self.sync()
        msgs = self._chat_messages.get(chat_id.strip(), [])
        return msgs[-max(1, int(limit)) :]

    def get_message_by_id(self, chat_id: str, message_id: str) -> dict[str, Any] | None:
        target = message_id.strip()
        for msg in self._chat_messages.get(chat_id.strip(), []):
            if str(msg.get("message_id") or "").strip() == target:
                return msg
        return None

    async def list_chats(self, *, exclude_chat_id: str = "") -> list[dict[str, str]]:
        await self.sync()
        excl = exclude_chat_id.strip()
        return [
            dict(info)
            for cid, info in self._known_chats.items()
            if not excl or cid != excl
        ]

    async def download_file(self, file_id: str) -> tuple[bytes, str]:
        resp = await self._client().get(f"{self._api_url}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram getFile failed: {payload}")
        file_path = str((payload.get("result") or {}).get("file_path") or "").lstrip("/")
        if not file_path:
            raise RuntimeError(f"Telegram getFile returned empty file_path for {file_id}")
        dl = await self._client().get(f"{self._file_base}/{file_path}", follow_redirects=True)
        dl.raise_for_status()
        return dl.content, Path(file_path).name


class TelegramInboundAdapter:
    """Official Telegram Bot API inbound ChannelAdapter for monitored groups and DMs."""

    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        *,
        hub: TelegramUpdateHub | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owner_username = config.owner_username.strip().lstrip("@").lower()
        self._owner_user_id = str(config.owner_user_id or "").strip()
        effective_token = (config.inbound_bot_token or config.bot_token).strip()
        self._owns_hub = hub is None
        self._hub = hub or TelegramUpdateHub(
            effective_token,
            base_url=config.base_url,
            poll_timeout_s=config.poll_timeout_s,
            client=client,
        )

    @property
    def hub(self) -> TelegramUpdateHub:
        return self._hub

    async def aclose(self) -> None:
        if self._owns_hub:
            await self._hub.aclose()

    def _normalize_message(self, channel_id: str, raw: dict[str, Any]) -> NormalizedMessage:
        msg_id = str(raw.get("message_id") or "").strip()
        create_ms = int(raw.get("date") or 0) * 1000
        sender = raw.get("from") or {}
        sender_id = str(sender.get("id") or "").strip()
        sender_username = str(sender.get("username") or "").strip().lower()
        is_bot = bool(sender.get("is_bot"))
        is_owner = bool(
            (self._owner_user_id and sender_id == self._owner_user_id)
            or (self._owner_username and sender_username == self._owner_username)
        )

        text = str(raw.get("text") or raw.get("caption") or "").strip()
        entities = raw.get("entities") if raw.get("text") else raw.get("caption_entities")
        mentions: list[Mention] = []
        mentions_owner = False

        for ent in entities or []:
            if not isinstance(ent, dict):
                continue
            etype = str(ent.get("type") or "")
            if etype == "mention":
                off = int(ent.get("offset") or 0)
                length = int(ent.get("length") or 0)
                token = text[off : off + length] if length > 0 else ""
                uname = token.lstrip("@").strip()
                if uname:
                    mentions.append(Mention(id=uname, name=uname, key=token))
                    if self._owner_username and uname.lower() == self._owner_username:
                        mentions_owner = True
            elif etype == "text_mention":
                user_obj = ent.get("user") or {}
                uid = str(user_obj.get("id") or "").strip()
                uname = str(user_obj.get("username") or "").strip()
                disp = _sender_display_name(user_obj)
                mentions.append(Mention(id=uid or disp, name=disp, key=f"@{disp}"))
                if (self._owner_user_id and uid == self._owner_user_id) or (
                    self._owner_username and uname.lower() == self._owner_username
                ):
                    mentions_owner = True

        if self._owner_username and f"@{self._owner_username}" in text.lower() and not mentions_owner:
            mentions_owner = True
            mentions.append(Mention(id=self._owner_username, name=self._owner_username, key=f"@{self._owner_username}"))

        reply_to_id = str((raw.get("reply_to_message") or {}).get("message_id") or "").strip()

        media_refs: list[MediaRef] = []
        photos = raw.get("photo") or []
        if isinstance(photos, list) and photos:
            largest = photos[-1]
            if isinstance(largest, dict) and largest.get("file_id"):
                fid = str(largest["file_id"])
                media_refs.append(MediaRef(kind="image", key=fid, ext="jpg", extra={"name": f"{fid}.jpg"}))

        for voice_field in ("voice", "audio"):
            item = raw.get(voice_field)
            if isinstance(item, dict) and item.get("file_id"):
                fid = str(item["file_id"])
                ext = "ogg" if voice_field == "voice" else "mp3"
                fname = str(item.get("file_name") or f"{fid}.{ext}")
                media_refs.append(MediaRef(kind="audio", key=fid, ext=ext, extra={"name": fname}))

        doc = raw.get("document")
        if isinstance(doc, dict) and doc.get("file_id"):
            fid = str(doc["file_id"])
            fname = str(doc.get("file_name") or f"{fid}.bin")
            ext = Path(fname).suffix.lower().lstrip(".") or "bin"
            media_refs.append(MediaRef(kind="file", key=fid, ext=ext, extra={"name": fname}))

        msg_type = "text"
        if not text and media_refs:
            msg_type = media_refs[0].kind

        return NormalizedMessage(
            message_id=msg_id,
            channel_id=channel_id,
            create_ms=create_ms,
            create_time=_format_ts_ms(create_ms),
            sender_id=sender_id,
            sender_name=_sender_display_name(sender),
            sender_type="bot" if is_bot else "user",
            is_owner=is_owner,
            is_bot=is_bot,
            msg_type=msg_type,
            raw_content=text,
            clean_text=text,
            reply_to_id=reply_to_id,
            mentions=mentions,
            mentions_owner=mentions_owner,
            media_refs=media_refs,
        )

    async def list_available_chats(self) -> list[dict[str, str]]:
        using_same_token = not self.config.inbound_bot_token.strip()
        exclude = self.config.owner_chat_id.strip() if using_same_token else ""
        return await self._hub.list_chats(exclude_chat_id=exclude)

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        msgs = await self._hub.get_chat_messages(channel.external_chat_id, limit=limit)
        ordered = sorted(msgs, key=lambda m: (int(m.get("date") or 0), int(m.get("message_id") or 0)))
        return [self._normalize_message(channel.id, raw) for raw in ordered]

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        out: list[NormalizedMessage] = []
        for mid in message_ids:
            raw = self._hub.get_message_by_id(channel.external_chat_id, str(mid))
            if raw is not None:
                out.append(self._normalize_message(channel.id, raw))
        return out

    async def download_media(
        self,
        channel: ChannelConfig,
        message: NormalizedMessage,
        dest_dir: Path,
    ) -> list[MediaArtifact]:
        del channel
        if not message.media_refs:
            return []
        target_dir = Path(dest_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[MediaArtifact] = []

        for ref in message.media_refs:
            try:
                content, remote_name = await self._hub.download_file(ref.key)
                hint_name = str((ref.extra or {}).get("name") or remote_name or "")
                fname = _sanitize_filename(hint_name, fallback=ref.key, default_ext=ref.ext or "bin")
                dest_path = target_dir / f"{_SAFE_STEM_RE.sub('_', ref.key)}_{fname}"
                dest_path.write_bytes(content)
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(dest_path)))
            except Exception as exc:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=str(exc)))
        return artifacts
