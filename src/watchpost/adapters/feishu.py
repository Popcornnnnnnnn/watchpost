"""Read-only official Feishu (Lark) ChannelAdapter with zero read-receipt leaks."""

from __future__ import annotations

import json
import mimetypes
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from watchpost.config import FeishuConfig
from watchpost.models import (
    ChannelConfig,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
)

_MENTION_KEY_RE = re.compile(r"@_user_\d+")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _format_ts_ms(create_ms: int) -> str:
    if create_ms <= 0:
        return ""
    return datetime.fromtimestamp(create_ms / 1000.0).strftime("%H:%M:%S")


def _sanitize_filename(name: str, *, fallback: str, default_ext: str) -> str:
    base = Path(name or "").name.strip()
    base = _SAFE_STEM_RE.sub("_", base).strip("._")
    if not base:
        base = _SAFE_STEM_RE.sub("_", fallback) or "artifact"
    if "." not in base and default_ext:
        base = f"{base}{default_ext}"
    return base[:120]


def _extract_post_payload(raw_obj: dict[str, Any]) -> dict[str, Any]:
    if "content" in raw_obj and isinstance(raw_obj["content"], list):
        return raw_obj
    for locale_key in ("zh_cn", "en_us", "ja_jp"):
        sub = raw_obj.get(locale_key)
        if isinstance(sub, dict) and isinstance(sub.get("content"), list):
            return sub
    for val in raw_obj.values():
        if isinstance(val, dict) and isinstance(val.get("content"), list):
            return val
    return raw_obj


def _parse_post_content(raw_obj: dict[str, Any]) -> tuple[str, list[MediaRef]]:
    payload = _extract_post_payload(raw_obj)
    lines: list[str] = []
    refs: list[MediaRef] = []
    title = str(payload.get("title") or "").strip()
    if title:
        lines.append(title)
    for row in payload.get("content") or []:
        if not isinstance(row, list):
            continue
        parts: list[str] = []
        for elem in row:
            if not isinstance(elem, dict):
                continue
            tag = str(elem.get("tag") or "")
            if tag == "text":
                parts.append(str(elem.get("text") or ""))
            elif tag == "a":
                label = str(elem.get("text") or "").strip()
                href = str(elem.get("href") or "").strip()
                parts.append(f"{label} ({href})" if label and href and label != href else (href or label))
            elif tag == "at":
                name = str(elem.get("user_name") or elem.get("user_id") or "").strip()
                if name:
                    parts.append(f"@{name}")
            elif tag == "img":
                img_key = str(elem.get("image_key") or "").strip()
                if img_key:
                    refs.append(MediaRef(kind="image", key=img_key, ext=".png"))
                    parts.append("[Image]")
            elif elem.get("text"):
                parts.append(str(elem["text"]))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip(), refs


class FeishuAdapter:
    """Strictly read-only Feishu Open API adapter (never marks messages read or sends)."""

    name = "feishu"

    def __init__(self, config: FeishuConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._tenant_token: str = ""
        self._tenant_token_expires_at: float = 0.0
        self._sender_names: dict[str, str] = {}
        if config.owner_open_id and config.owner_name:
            self._sender_names[config.owner_open_id] = config.owner_name

    def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=20.0)
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def _get_access_token(self) -> str:
        if self._config.auth_mode == "user" or (
            self._config.user_access_token and not (self._config.app_id and self._config.app_secret)
        ):
            tok = self._config.user_access_token.strip()
            if not tok:
                raise RuntimeError("Feishu user_access_token is not configured")
            return tok

        if not self._config.app_id or not self._config.app_secret:
            raise RuntimeError("Feishu app_id and app_secret are required for tenant auth")

        now = time.monotonic()
        if self._tenant_token and now < self._tenant_token_expires_at - 60.0:
            return self._tenant_token

        resp = await self._client().post(
            f"{self._base_url}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
        )
        resp.raise_for_status()
        payload = resp.json()
        if int(payload.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu tenant auth error {payload.get('code')}: {payload.get('msg', '')}")
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise RuntimeError("Feishu tenant auth response missing tenant_access_token")
        expire_s = float(payload.get("expire") or 7200)
        self._tenant_token = token
        self._tenant_token_expires_at = now + expire_s
        return token

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_access_token()}"}

    async def list_available_chats(self) -> list[dict[str, str]]:
        headers = await self._auth_headers()
        chats: list[dict[str, str]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            resp = await self._client().get(
                f"{self._base_url}/im/v1/chats",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
            if int(payload.get("code", 0)) != 0:
                raise RuntimeError(f"Feishu list_chats error {payload.get('code')}: {payload.get('msg', '')}")
            data = payload.get("data") or {}
            for item in data.get("items") or []:
                chat_id = str(item.get("chat_id") or "").strip()
                if not chat_id:
                    continue
                raw_mode = str(item.get("chat_mode") or "group").lower()
                chats.append(
                    {
                        "external_chat_id": chat_id,
                        "name": str(item.get("name") or chat_id),
                        "chat_type": "p2p" if raw_mode in {"p2p", "single"} else "group",
                    }
                )
            if not data.get("has_more") or not data.get("page_token"):
                break
            page_token = str(data["page_token"])
        return chats

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        headers = await self._auth_headers()
        page_size = max(1, min(int(limit), 50))
        resp = await self._client().get(
            f"{self._base_url}/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": channel.external_chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": page_size,
            },
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
        if int(payload.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu fetch_recent error {payload.get('code')}: {payload.get('msg', '')}")
        items = (payload.get("data") or {}).get("items") or []
        normalized = [self._normalize_item(channel, item) for item in reversed(items)]
        return sorted(normalized, key=lambda m: (m.create_ms, m.message_id))

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        headers = await self._auth_headers()
        results: list[NormalizedMessage] = []
        for mid in dict.fromkeys(mid for mid in message_ids if mid):
            resp = await self._client().get(
                f"{self._base_url}/im/v1/messages/{mid}",
                headers=headers,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            payload = resp.json()
            if int(payload.get("code", 0)) != 0:
                continue
            data = payload.get("data") or {}
            items = data.get("items")
            item = items[0] if isinstance(items, list) and items else data.get("message") or data
            if isinstance(item, dict) and item.get("message_id"):
                results.append(self._normalize_item(channel, item))
        return sorted(results, key=lambda m: (m.create_ms, m.message_id))

    async def download_media(
        self,
        channel: ChannelConfig,
        message: NormalizedMessage,
        dest_dir: Path,
    ) -> list[MediaArtifact]:
        del channel
        headers = await self._auth_headers()
        target_dir = Path(dest_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[MediaArtifact] = []
        for ref in message.media_refs:
            res_type = "image" if ref.kind == "image" else "file"
            default_ext = ref.ext or (".png" if ref.kind == "image" else ".opus" if ref.kind == "audio" else ".bin")
            try:
                resp = await self._client().get(
                    f"{self._base_url}/im/v1/messages/{message.message_id}/resources/{ref.key}",
                    params={"type": res_type},
                    headers=headers,
                )
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                guessed_ext = mimetypes.guess_extension(ctype) if ctype else None
                filename = _sanitize_filename(
                    str(ref.extra.get("file_name") or ""),
                    fallback=ref.key,
                    default_ext=guessed_ext or default_ext,
                )
                out_path = target_dir / filename
                out_path.write_bytes(resp.content)
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(out_path)))
            except Exception as exc:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=str(exc)))
        return artifacts

    def _normalize_item(self, channel: ChannelConfig, item: dict[str, Any]) -> NormalizedMessage:
        msg_id = str(item.get("message_id") or "")
        create_ms = int(item.get("create_time") or 0)
        msg_type = str(item.get("msg_type") or "text")
        deleted = bool(item.get("deleted"))

        sender = item.get("sender") or {}
        sender_id = str(sender.get("id") or "")
        sender_type = str(sender.get("sender_type") or "user")
        is_bot = sender_type in {"app", "bot"}
        owner_id = self._config.owner_open_id.strip()
        is_owner = bool(owner_id and sender_id == owner_id)

        mentions: list[Mention] = []
        mentions_owner = False
        mentions_all = False
        mention_by_key: dict[str, str] = {}
        for m_raw in item.get("mentions") or []:
            if not isinstance(m_raw, dict):
                continue
            mid = str(m_raw.get("id") or "")
            mname = str(m_raw.get("name") or mid)
            mkey = str(m_raw.get("key") or "")
            if mid and mname:
                self._sender_names[mid] = mname
            if mkey:
                mention_by_key[mkey] = mname or mkey
            if mid == "all" or mkey == "@_all":
                mentions_all = True
            if owner_id and mid == owner_id:
                mentions_owner = True
            mentions.append(Mention(id=mid, name=mname, key=mkey))

        sender_name = str(sender.get("name") or self._sender_names.get(sender_id) or "")
        if not sender_name:
            sender_name = self._config.owner_name or "Owner" if is_owner else (sender_id or "Unknown")

        body_raw = str((item.get("body") or {}).get("content") or "")
        clean_text, media_refs = self._extract_content(msg_type, body_raw, deleted, mention_by_key)
        reply_to_id = str(item.get("parent_id") or item.get("root_id") or "")

        return NormalizedMessage(
            message_id=msg_id,
            channel_id=channel.id,
            position=0,
            create_ms=create_ms,
            create_time=_format_ts_ms(create_ms),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type=sender_type,
            is_owner=is_owner,
            is_bot=is_bot,
            deleted=deleted,
            msg_type="system" if msg_type == "system" else msg_type,
            raw_content=body_raw,
            clean_text=clean_text,
            reply_to_id=reply_to_id,
            mentions=mentions,
            mentions_owner=mentions_owner,
            mentions_all=mentions_all,
            media_refs=media_refs,
        )

    def _extract_content(
        self,
        msg_type: str,
        body_raw: str,
        deleted: bool,
        mention_by_key: dict[str, str],
    ) -> tuple[str, list[MediaRef]]:
        if deleted:
            return "[Recalled]", []

        parsed: dict[str, Any] = {}
        if body_raw:
            try:
                loaded = json.loads(body_raw)
                if isinstance(loaded, dict):
                    parsed = loaded
            except json.JSONDecodeError:
                parsed = {}

        if msg_type == "text":
            text = str(parsed.get("text") or body_raw).strip()
            if mention_by_key:
                text = _MENTION_KEY_RE.sub(
                    lambda m: f"@{mention_by_key[m.group(0)]}" if m.group(0) in mention_by_key else m.group(0),
                    text,
                )
            return text.strip(), []

        if msg_type == "post":
            return _parse_post_content(parsed)

        if msg_type == "image":
            img_key = str(parsed.get("image_key") or "").strip()
            refs = [MediaRef(kind="image", key=img_key, ext=".png")] if img_key else []
            return "[Image]", refs

        if msg_type == "audio":
            file_key = str(parsed.get("file_key") or "").strip()
            refs = [MediaRef(kind="audio", key=file_key, ext=".opus")] if file_key else []
            return "[Voice]", refs

        if msg_type == "file":
            file_key = str(parsed.get("file_key") or "").strip()
            file_name = str(parsed.get("file_name") or "").strip()
            ext = Path(file_name).suffix or ".bin"
            refs = (
                [MediaRef(kind="file", key=file_key, ext=ext, extra={"file_name": file_name})]
                if file_key
                else []
            )
            return f"[File: {file_name}]" if file_name else "[File]", refs

        if msg_type == "sticker":
            return "[Sticker]", []

        if msg_type == "system":
            return "[System]", []

        return str(parsed.get("text") or f"[{msg_type}]").strip(), []
