"""Discord Guild ChannelAdapter built cleanly on the official Discord REST API v10."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any

import httpx

from watchpost.config import DiscordConfig
from watchpost.models import (
    ChannelConfig,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
)

_MENTION_RE = re.compile(r"<@!?(\d+)>")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic"})
_AUDIO_EXTS = frozenset({"mp3", "wav", "ogg", "oga", "m4a", "aac", "opus", "webm", "flac"})
_DISCORD_TEXT_CHANNEL_TYPES = frozenset({0, 5, 10, 11, 12})


def _snowflake_key(msg_id: str) -> tuple[int, str]:
    clean = (msg_id or "").strip()
    return (int(clean) if clean.isdigit() else 0), clean


def _parse_iso_ms(raw_ts: str) -> int:
    clean = (raw_ts or "").strip()
    if not clean:
        return 0
    try:
        return int(datetime.fromisoformat(clean.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


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
        base = f"{base}.{default_ext.lstrip('.')}"
    return base[:120]


def _classify_attachment(att: dict[str, Any]) -> tuple[str, str]:
    ctype = str(att.get("content_type") or "").lower()
    fname = str(att.get("filename") or "").strip()
    ext = Path(fname).suffix.lower().lstrip(".")
    if ctype.startswith("image/") or ext in _IMAGE_EXTS:
        return "image", ext or "png"
    if ctype.startswith("audio/") or ext in _AUDIO_EXTS:
        return "audio", ext or "ogg"
    return "file", ext or "bin"


class DiscordAdapter:
    """Official Discord REST API v10 ChannelAdapter (`bot_token`)."""

    name = "discord"

    def __init__(self, config: DiscordConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._token = config.bot_token.strip()
        self._owner_user_id = config.owner_user_id.strip()
        self._base_url = (config.base_url or "https://discord.com/api/v10").strip().rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None

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

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            raise RuntimeError("Discord bot_token must be configured")
        return {"Authorization": f"Bot {self._token}"}

    def _normalize_message(self, channel_id: str, raw: dict[str, Any]) -> NormalizedMessage:
        msg_id = str(raw.get("id") or "").strip()
        create_ms = _parse_iso_ms(str(raw.get("timestamp") or ""))
        author = raw.get("author") or {}
        sender_id = str(author.get("id") or "").strip()
        sender_name = (
            str(author.get("global_name") or author.get("username") or sender_id or "Unknown").strip()
        )
        is_bot = bool(author.get("bot"))
        is_owner = bool(self._owner_user_id and sender_id == self._owner_user_id)

        raw_text = str(raw.get("content") or "")
        user_names: dict[str, str] = {}
        mentions: list[Mention] = []
        seen_mention_ids: set[str] = set()
        mentions_owner = False

        for m_user in raw.get("mentions") or []:
            if not isinstance(m_user, dict):
                continue
            uid = str(m_user.get("id") or "").strip()
            if not uid:
                continue
            disp = str(m_user.get("global_name") or m_user.get("username") or uid).strip()
            user_names[uid] = disp
            if uid not in seen_mention_ids:
                seen_mention_ids.add(uid)
                mentions.append(Mention(id=uid, name=disp, key=f"<@{uid}>"))
            if self._owner_user_id and uid == self._owner_user_id:
                mentions_owner = True

        for match in _MENTION_RE.finditer(raw_text):
            uid = match.group(1)
            if uid not in seen_mention_ids:
                seen_mention_ids.add(uid)
                mentions.append(Mention(id=uid, name=user_names.get(uid, uid), key=match.group(0)))
            if self._owner_user_id and uid == self._owner_user_id:
                mentions_owner = True

        clean_text = _MENTION_RE.sub(lambda m: f"@{user_names.get(m.group(1), m.group(1))}", raw_text).strip()
        mentions_all = bool(raw.get("mention_everyone"))
        reply_to_id = str((raw.get("message_reference") or {}).get("message_id") or "").strip()

        media_refs: list[MediaRef] = []
        for att in raw.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            kind, ext = _classify_attachment(att)
            att_id = str(att.get("id") or f"{msg_id}_att").strip()
            media_refs.append(
                MediaRef(
                    kind=kind,
                    key=att_id,
                    ext=ext,
                    extra={
                        "url": str(att.get("url") or att.get("proxy_url") or "").strip(),
                        "name": str(att.get("filename") or f"{att_id}.{ext}"),
                    },
                )
            )

        msg_type = "text"
        if not clean_text and media_refs:
            msg_type = media_refs[0].kind

        return NormalizedMessage(
            message_id=msg_id,
            channel_id=channel_id,
            create_ms=create_ms,
            create_time=_format_ts_ms(create_ms),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type="bot" if is_bot else "user",
            is_owner=is_owner,
            is_bot=is_bot,
            msg_type=msg_type,
            raw_content=raw_text,
            clean_text=clean_text,
            reply_to_id=reply_to_id,
            mentions=mentions,
            mentions_owner=mentions_owner,
            mentions_all=mentions_all,
            media_refs=media_refs,
        )

    async def list_available_chats(self) -> list[dict[str, str]]:
        guild_ids = [str(g).strip() for g in self.config.guild_ids if str(g).strip()]
        if not guild_ids:
            resp = await self._client().get(
                f"{self._base_url}/users/@me/guilds",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            guild_ids = [
                str(g.get("id") or "").strip()
                for g in (resp.json() or [])
                if isinstance(g, dict) and str(g.get("id") or "").strip()
            ]

        chats: list[dict[str, str]] = []
        for gid in guild_ids:
            resp = await self._client().get(
                f"{self._base_url}/guilds/{gid}/channels",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            for ch in resp.json() or []:
                if not isinstance(ch, dict):
                    continue
                if ch.get("type") not in _DISCORD_TEXT_CHANNEL_TYPES:
                    continue
                cid = str(ch.get("id") or "").strip()
                if not cid:
                    continue
                raw_name = str(ch.get("name") or cid).strip()
                chats.append(
                    {
                        "external_chat_id": cid,
                        "name": raw_name if raw_name.startswith("#") else f"#{raw_name}",
                        "chat_type": "group",
                    }
                )
        return chats

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        resp = await self._client().get(
            f"{self._base_url}/channels/{channel.external_chat_id}/messages",
            params={"limit": min(max(int(limit), 1), 100)},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        items = resp.json() or []
        ordered = sorted(
            [m for m in items if isinstance(m, dict) and str(m.get("id") or "").strip()],
            key=lambda m: _snowflake_key(str(m.get("id") or "")),
        )
        return [self._normalize_message(channel.id, raw) for raw in ordered]

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        out: list[NormalizedMessage] = []
        for mid in message_ids:
            clean_id = str(mid or "").strip()
            if not clean_id:
                continue
            resp = await self._client().get(
                f"{self._base_url}/channels/{channel.external_chat_id}/messages/{clean_id}",
                headers=self._auth_headers(),
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            raw = resp.json()
            if isinstance(raw, dict):
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
            url = str((ref.extra or {}).get("url") or "").strip()
            if not url:
                artifacts.append(
                    MediaArtifact(kind=ref.kind, key=ref.key, error="Missing Discord attachment URL")
                )
                continue
            fname = _sanitize_filename(
                str((ref.extra or {}).get("name") or ""),
                fallback=ref.key,
                default_ext=ref.ext or "bin",
            )
            dest_path = target_dir / f"{_SAFE_STEM_RE.sub('_', ref.key)}_{fname}"
            try:
                resp = await self._client().get(url, follow_redirects=True)
                resp.raise_for_status()
                dest_path.write_bytes(resp.content)
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(dest_path)))
            except Exception as exc:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=str(exc)))
        return artifacts
