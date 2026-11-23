"""DingTalk ChannelAdapter powered by signed callback webhooks + official robot media API."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from typing import Any

import httpx

from watchpost.config import DingTalkConfig
from watchpost.models import (
    ChannelConfig,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
)

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def verify_dingtalk_signature(
    timestamp: str,
    sign: str,
    secret: str,
    *,
    now_ms: int | None = None,
    max_skew_ms: int = 3_600_000,
) -> bool:
    """Verify DingTalk robot/event callback HMAC-SHA256 signature and timestamp window."""
    ts_clean = (timestamp or "").strip()
    sign_clean = (sign or "").strip()
    sec_clean = (secret or "").strip()
    if not ts_clean or not sign_clean or not sec_clean:
        return False
    try:
        ts_ms = int(ts_clean)
    except ValueError:
        return False
    ref_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(ref_ms - ts_ms) > max_skew_ms:
        return False
    string_to_sign = f"{ts_clean}\n{sec_clean}".encode("utf-8")
    digest = hmac.new(sec_clean.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, sign_clean)


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


class DingTalkAdapter:
    """DingTalk ChannelAdapter using signed HTTP callback events and official media APIs."""

    name = "dingtalk"

    def __init__(
        self,
        config: DingTalkConfig,
        *,
        inbox_path: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        raw_inbox = inbox_path or (
            Path(config.inbox_path).expanduser()
            if config.inbox_path
            else Path("~/.local/share/watchpost/webhooks/dingtalk.jsonl").expanduser()
        )
        self._inbox_path = Path(raw_inbox)
        self._owner_user_id = config.owner_user_id.strip()
        self._base_url = (config.base_url or "https://api.dingtalk.com").strip().rstrip("/")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._token_value: str = ""
        self._token_expires_at: float = 0.0

    @property
    def inbox_path(self) -> Path:
        return self._inbox_path

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
        now = time.monotonic()
        if self._token_value and now < self._token_expires_at - 60:
            return self._token_value
        if not self.config.app_key.strip() or not self.config.app_secret.strip():
            raise RuntimeError("DingTalk app_key and app_secret must be configured")
        resp = await self._client().post(
            f"{self._base_url}/v1.0/oauth2/accessToken",
            json={
                "appKey": self.config.app_key.strip(),
                "appSecret": self.config.app_secret.strip(),
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        token = str((payload or {}).get("accessToken") or "").strip()
        if not token:
            raise RuntimeError(f"DingTalk accessToken request failed: {payload}")
        ttl = int((payload or {}).get("expireIn") or 7200)
        self._token_value = token
        self._token_expires_at = now + ttl
        return token

    def ingest_webhook_payload(self, payload: dict[str, Any]) -> NormalizedMessage:
        """Normalize and persist an incoming DingTalk callback event to the local JSONL inbox."""
        conv_id = str(payload.get("conversationId") or "").strip()
        if not conv_id:
            raise ValueError("DingTalk payload missing conversationId")

        conv_type_raw = str(payload.get("conversationType") or "2").strip()
        chat_type = "p2p" if conv_type_raw == "1" else "group"
        sender_id = str(payload.get("senderStaffId") or payload.get("senderId") or "").strip()
        sender_name = str(payload.get("senderNick") or sender_id or "Unknown").strip()
        conv_title = str(payload.get("conversationTitle") or sender_name or conv_id).strip()

        raw_create = payload.get("createAt") or payload.get("timestamp")
        try:
            create_ms = int(raw_create) if raw_create is not None else int(time.time() * 1000)
            if 0 < create_ms < 10_000_000_000:
                create_ms *= 1000
        except (TypeError, ValueError):
            create_ms = int(time.time() * 1000)

        msg_id = str(payload.get("msgId") or f"dt_{create_ms}_{sender_id or 'msg'}").strip()
        is_owner = bool(self._owner_user_id and sender_id == self._owner_user_id)
        msgtype = str(payload.get("msgtype") or "text").strip().lower()

        text_parts: list[str] = []
        media_refs: list[MediaRef] = []
        content_obj = payload.get("content") if isinstance(payload.get("content"), dict) else {}

        if msgtype == "text":
            txt = str((payload.get("text") or {}).get("content") or "").strip()
            if txt:
                text_parts.append(txt)
        elif msgtype == "richtext":
            for chunk in content_obj.get("richText") or []:
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("text"):
                    text_parts.append(str(chunk["text"]).strip())
                pic_code = str(chunk.get("pictureDownloadCode") or chunk.get("downloadCode") or "").strip()
                if pic_code:
                    media_refs.append(MediaRef(kind="image", key=pic_code, ext="png", extra={"name": f"{pic_code}.png"}))
        elif msgtype in {"picture", "image"}:
            code = str(content_obj.get("downloadCode") or payload.get("downloadCode") or "").strip()
            if code:
                media_refs.append(MediaRef(kind="image", key=code, ext="png", extra={"name": f"{code}.png"}))
        elif msgtype in {"audio", "voice"}:
            code = str(content_obj.get("downloadCode") or payload.get("downloadCode") or "").strip()
            rec = str(content_obj.get("recognition") or "").strip()
            if rec:
                text_parts.append(rec)
            if code:
                media_refs.append(MediaRef(kind="audio", key=code, ext="amr", extra={"name": f"{code}.amr"}))
        elif msgtype == "file":
            code = str(content_obj.get("downloadCode") or payload.get("downloadCode") or "").strip()
            fname = str(content_obj.get("fileName") or f"{code}.bin").strip()
            ext = Path(fname).suffix.lower().lstrip(".") or "bin"
            if code:
                media_refs.append(MediaRef(kind="file", key=code, ext=ext, extra={"name": fname}))
        else:
            fallback = str((payload.get("text") or {}).get("content") or payload.get("content") or "").strip()
            if fallback:
                text_parts.append(fallback)

        clean_text = "\n".join(p for p in text_parts if p).strip()
        mentions: list[Mention] = []
        mentions_owner = False
        at_users = payload.get("atUsers") or []
        for u in at_users:
            if not isinstance(u, dict):
                continue
            staff_id = str(u.get("staffId") or "").strip()
            dt_id = str(u.get("dingtalkId") or "").strip()
            uid = staff_id or dt_id
            if not uid:
                continue
            disp = str(u.get("name") or u.get("nick") or uid).strip()
            mentions.append(Mention(id=uid, name=disp, key=f"@{disp}"))
            if self._owner_user_id and self._owner_user_id in {staff_id, dt_id}:
                mentions_owner = True

        if not self._owner_user_id and bool(payload.get("isInAtList")):
            mentions_owner = True

        norm_msg_type = "text"
        if not clean_text and media_refs:
            norm_msg_type = media_refs[0].kind

        msg = NormalizedMessage(
            message_id=msg_id,
            create_ms=create_ms,
            create_time=_format_ts_ms(create_ms),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type="user",
            is_owner=is_owner,
            is_bot=False,
            msg_type=norm_msg_type,
            raw_content=json.dumps(payload, ensure_ascii=False),
            clean_text=clean_text,
            mentions=mentions,
            mentions_owner=mentions_owner,
            mentions_all=False,
            media_refs=media_refs,
        )
        record = {
            "chat": {
                "external_chat_id": conv_id,
                "name": conv_title,
                "chat_type": chat_type,
            },
            "message": msg.model_dump(mode="json"),
        }
        self._inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self._inbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return msg

    def _read_inbox_records(self) -> list[dict[str, Any]]:
        if not self._inbox_path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        for raw_line in self._inbox_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("message"), dict):
                entries.append(obj)
        return entries

    async def list_available_chats(self) -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for entry in self._read_inbox_records():
            chat = entry.get("chat") or {}
            cid = str(chat.get("external_chat_id") or "").strip()
            if not cid:
                continue
            seen[cid] = {
                "external_chat_id": cid,
                "name": str(chat.get("name") or cid),
                "chat_type": str(chat.get("chat_type") or "group"),
            }
        return list(seen.values())

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        by_id: dict[str, NormalizedMessage] = {}
        for entry in self._read_inbox_records():
            chat = entry.get("chat") or {}
            if str(chat.get("external_chat_id") or "").strip() != channel.external_chat_id:
                continue
            msg = NormalizedMessage.model_validate(entry["message"])
            msg = msg.model_copy(update={"channel_id": channel.id})
            by_id[msg.message_id] = msg
        ordered = sorted(by_id.values(), key=lambda m: (m.create_ms, m.message_id))
        return ordered[-max(1, int(limit)) :]

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        wanted = {str(mid).strip() for mid in message_ids if str(mid).strip()}
        if not wanted:
            return []
        out: list[NormalizedMessage] = []
        for entry in self._read_inbox_records():
            chat = entry.get("chat") or {}
            if str(chat.get("external_chat_id") or "").strip() != channel.external_chat_id:
                continue
            msg = NormalizedMessage.model_validate(entry["message"])
            if msg.message_id in wanted:
                out.append(msg.model_copy(update={"channel_id": channel.id}))
        return sorted(out, key=lambda m: (m.create_ms, m.message_id))

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
        robot_code = (self.config.robot_code or self.config.app_key).strip()
        artifacts: list[MediaArtifact] = []

        for ref in message.media_refs:
            try:
                token = await self._get_access_token()
                resp = await self._client().post(
                    f"{self._base_url}/v1.0/robot/messageFiles/download",
                    headers={"x-acs-dingtalk-access-token": token},
                    json={"downloadCode": ref.key, "robotCode": robot_code},
                )
                resp.raise_for_status()
                dl_url = str((resp.json() or {}).get("downloadUrl") or "").strip()
                if not dl_url:
                    raise RuntimeError("DingTalk messageFiles/download did not return downloadUrl")
                dl = await self._client().get(dl_url, follow_redirects=True)
                dl.raise_for_status()
                fname = _sanitize_filename(
                    str((ref.extra or {}).get("name") or ""),
                    fallback=ref.key,
                    default_ext=ref.ext or "bin",
                )
                dest_path = target_dir / f"{_SAFE_STEM_RE.sub('_', ref.key)}_{fname}"
                dest_path.write_bytes(dl.content)
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(dest_path)))
            except Exception as exc:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=str(exc)))
        return artifacts
