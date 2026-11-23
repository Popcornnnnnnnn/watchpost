"""Read-only Slack ChannelAdapter built cleanly on the official Slack Web API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any

import httpx

from watchpost.config import SlackConfig
from watchpost.models import (
    ChannelConfig,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
)

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9_]+)(?:\|([^>]+))?>")
_SPECIAL_MENTION_RE = re.compile(r"<!(channel|here|everyone)(?:\|[^>]+)?>")
_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_SLACK_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_IGNORED_SUBTYPES = frozenset({"channel_join", "channel_leave", "channel_topic", "channel_purpose"})
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic"})
_AUDIO_EXTS = frozenset({"mp3", "wav", "ogg", "oga", "m4a", "aac", "opus", "webm", "flac"})


def _parse_slack_ts_ms(ts: str) -> int:
    clean = (ts or "").strip()
    if not clean:
        return 0
    try:
        return int(float(clean) * 1000)
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
        clean_ext = default_ext.lstrip(".")
        base = f"{base}.{clean_ext}"
    return base[:120]


def _classify_slack_file(raw_file: dict[str, Any]) -> tuple[str, str]:
    mimetype = str(raw_file.get("mimetype") or "").lower()
    filetype = str(raw_file.get("filetype") or "").strip().lower().lstrip(".")
    if not filetype and raw_file.get("name"):
        filetype = Path(str(raw_file["name"])).suffix.lower().lstrip(".")
    if mimetype.startswith("image/") or filetype in _IMAGE_EXTS:
        return "image", filetype or "png"
    if mimetype.startswith("audio/") or filetype in _AUDIO_EXTS:
        return "audio", filetype or "m4a"
    return "file", filetype or "bin"


class SlackAdapter:
    """Official Slack Web API ChannelAdapter (`bot_token`)."""

    name = "slack"

    def __init__(self, config: SlackConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._token = config.bot_token.strip()
        self._owner_user_id = config.owner_user_id.strip()
        self._base_url = (config.base_url or "https://slack.com/api").strip().rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._user_cache: dict[str, str] = {}

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
            raise RuntimeError("Slack bot_token must be configured")
        return {"Authorization": f"Bearer {self._token}"}

    async def _api_get(self, method: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._client().get(
            f"{self._base_url}/{method}",
            params=params or {},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            err = payload.get("error") if isinstance(payload, dict) else payload
            raise RuntimeError(f"Slack {method} failed: {err}")
        return payload

    async def _resolve_user_name(self, user_id: str) -> str:
        uid = (user_id or "").strip()
        if not uid:
            return "Unknown"
        if uid in self._user_cache:
            return self._user_cache[uid]
        try:
            payload = await self._api_get("users.info", params={"user": uid})
            user_obj = payload.get("user") or {}
            profile = user_obj.get("profile") or {}
            resolved = (
                str(profile.get("display_name") or "").strip()
                or str(user_obj.get("real_name") or "").strip()
                or str(profile.get("real_name") or "").strip()
                or str(user_obj.get("name") or "").strip()
                or uid
            )
        except Exception:
            resolved = uid
        self._user_cache[uid] = resolved
        return resolved

    async def _clean_slack_text(self, raw_text: str) -> tuple[str, list[Mention], bool, bool]:
        mentions: list[Mention] = []
        mentions_owner = False
        mentions_all = False

        matches = list(_MENTION_RE.finditer(raw_text or ""))
        id_to_name: dict[str, str] = {}
        for m in matches:
            uid = m.group(1)
            hint = (m.group(2) or "").strip()
            if hint:
                id_to_name[uid] = hint
                self._user_cache.setdefault(uid, hint)
            elif uid not in id_to_name:
                id_to_name[uid] = await self._resolve_user_name(uid)
            mentions.append(Mention(id=uid, name=id_to_name[uid], key=m.group(0)))
            if self._owner_user_id and uid == self._owner_user_id:
                mentions_owner = True

        def _repl_user(m: re.Match[str]) -> str:
            return f"@{id_to_name.get(m.group(1), m.group(1))}"

        text = _MENTION_RE.sub(_repl_user, raw_text or "")
        if _SPECIAL_MENTION_RE.search(text):
            mentions_all = True
            text = _SPECIAL_MENTION_RE.sub(lambda m: f"@{m.group(1).lower()}", text)
        text = _SLACK_LINK_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
        text = _SLACK_BARE_LINK_RE.sub(r"\1", text)
        return text.strip(), mentions, mentions_owner, mentions_all

    async def _normalize_message(self, channel_id: str, raw: dict[str, Any]) -> NormalizedMessage:
        ts = str(raw.get("ts") or "").strip()
        create_ms = _parse_slack_ts_ms(ts)
        subtype = str(raw.get("subtype") or "")
        sender_id = str(raw.get("user") or raw.get("bot_id") or "").strip()
        is_bot = bool(raw.get("bot_id") or subtype == "bot_message")
        deleted = subtype == "message_deleted"

        if raw.get("user"):
            sender_name = await self._resolve_user_name(sender_id)
        else:
            bot_profile = raw.get("bot_profile") or {}
            sender_name = (
                str(raw.get("username") or bot_profile.get("name") or sender_id or "Unknown").strip()
            )
        is_owner = bool(self._owner_user_id and sender_id == self._owner_user_id)

        raw_text = str(raw.get("text") or "")
        clean_text, mentions, mentions_owner, mentions_all = await self._clean_slack_text(raw_text)

        thread_ts = str(raw.get("thread_ts") or "").strip()
        reply_to_id = thread_ts if (thread_ts and thread_ts != ts) else ""

        media_refs: list[MediaRef] = []
        for raw_file in raw.get("files") or []:
            if not isinstance(raw_file, dict):
                continue
            kind, ext = _classify_slack_file(raw_file)
            file_id = str(raw_file.get("id") or f"{ts}_file").strip()
            dl_url = str(
                raw_file.get("url_private_download") or raw_file.get("url_private") or ""
            ).strip()
            media_refs.append(
                MediaRef(
                    kind=kind,
                    key=file_id,
                    ext=ext,
                    extra={
                        "url": dl_url,
                        "name": str(raw_file.get("name") or f"{file_id}.{ext}"),
                    },
                )
            )

        msg_type = "text"
        if not clean_text and media_refs:
            msg_type = media_refs[0].kind

        return NormalizedMessage(
            message_id=ts,
            channel_id=channel_id,
            create_ms=create_ms,
            create_time=_format_ts_ms(create_ms),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type="bot" if is_bot else "user",
            is_owner=is_owner,
            is_bot=is_bot,
            deleted=deleted,
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
        chats: list[dict[str, str]] = []
        cursor = ""
        for _ in range(5):
            params: dict[str, Any] = {
                "types": "public_channel,private_channel,im,mpim",
                "exclude_archived": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            payload = await self._api_get("conversations.list", params=params)
            for ch in payload.get("channels") or []:
                cid = str(ch.get("id") or "").strip()
                if not cid:
                    continue
                if ch.get("is_im"):
                    uid = str(ch.get("user") or "").strip()
                    name = await self._resolve_user_name(uid) if uid else cid
                    chat_type = "p2p"
                else:
                    raw_name = str(ch.get("name") or cid).strip()
                    name = f"#{raw_name}" if raw_name and not raw_name.startswith("#") else (raw_name or cid)
                    chat_type = "group"
                chats.append(
                    {
                        "external_chat_id": cid,
                        "name": name,
                        "chat_type": chat_type,
                    }
                )
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
        return chats

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        payload = await self._api_get(
            "conversations.history",
            params={"channel": channel.external_chat_id, "limit": min(max(int(limit), 1), 200)},
        )
        by_ts: dict[str, dict[str, Any]] = {}
        thread_parents: list[str] = []

        for item in payload.get("messages") or []:
            ts = str(item.get("ts") or "").strip()
            if not ts:
                continue
            by_ts[ts] = item
            reply_count = int(item.get("reply_count") or 0)
            latest_reply_ms = _parse_slack_ts_ms(str(item.get("latest_reply") or ts))
            if (
                self.config.fetch_thread_replies
                and reply_count > 0
                and (channel.cursor_ms == 0 or latest_reply_ms > channel.cursor_ms)
            ):
                thread_parents.append(ts)

        for parent_ts in thread_parents[:5]:
            try:
                rep_payload = await self._api_get(
                    "conversations.replies",
                    params={"channel": channel.external_chat_id, "ts": parent_ts, "limit": 30},
                )
            except Exception:
                continue
            for item in rep_payload.get("messages") or []:
                ts = str(item.get("ts") or "").strip()
                if ts and ts not in by_ts:
                    by_ts[ts] = item

        ordered = sorted(by_ts.values(), key=lambda m: (_parse_slack_ts_ms(str(m.get("ts") or "")), str(m.get("ts") or "")))
        out: list[NormalizedMessage] = []
        for raw in ordered:
            if str(raw.get("subtype") or "") in _IGNORED_SUBTYPES:
                continue
            out.append(await self._normalize_message(channel.id, raw))
        return out

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        out: list[NormalizedMessage] = []
        for mid in message_ids:
            ts = str(mid or "").strip()
            if not ts:
                continue
            try:
                payload = await self._api_get(
                    "conversations.history",
                    params={
                        "channel": channel.external_chat_id,
                        "latest": ts,
                        "oldest": ts,
                        "inclusive": "true",
                        "limit": 1,
                    },
                )
            except Exception:
                continue
            for raw in payload.get("messages") or []:
                if str(raw.get("ts") or "").strip() == ts:
                    out.append(await self._normalize_message(channel.id, raw))
                    break
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
                    MediaArtifact(kind=ref.kind, key=ref.key, error="Missing Slack private file URL")
                )
                continue
            fname = _sanitize_filename(
                str((ref.extra or {}).get("name") or ""),
                fallback=ref.key,
                default_ext=ref.ext or "bin",
            )
            dest_path = target_dir / f"{_SAFE_STEM_RE.sub('_', ref.key)}_{fname}"
            try:
                resp = await self._client().get(
                    url,
                    headers=self._auth_headers(),
                    follow_redirects=True,
                )
                resp.raise_for_status()
                dest_path.write_bytes(resp.content)
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(dest_path)))
            except Exception as exc:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=str(exc)))
        return artifacts
