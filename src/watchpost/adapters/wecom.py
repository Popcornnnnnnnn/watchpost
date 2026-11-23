"""WeCom (Enterprise WeChat) ChannelAdapter via signed XML/JSON callbacks + media API."""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from typing import Any
import xml.etree.ElementTree as ET

import httpx

from watchpost.config import WeComConfig
from watchpost.models import (
    ChannelConfig,
    MediaArtifact,
    MediaRef,
    Mention,
    NormalizedMessage,
)

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FORBIDDEN_XML_RE = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def verify_wecom_signature(
    token: str,
    timestamp: str,
    nonce: str,
    msg_signature: str,
    encrypt_or_echostr: str = "",
) -> bool:
    """Verify WeCom SHA1 callback signature (`msg_signature`)."""
    tok = (token or "").strip()
    ts = (timestamp or "").strip()
    nc = (nonce or "").strip()
    sig = (msg_signature or "").strip()
    if not tok or not ts or not nc or not sig:
        return False
    parts = [tok, ts, nc]
    extra = (encrypt_or_echostr or "").strip()
    if extra:
        parts.append(extra)
    raw = "".join(sorted(parts)).encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()
    return hmac.compare_digest(digest, sig)


def parse_wecom_xml(xml_text: str) -> dict[str, str]:
    """Parse an inbound WeCom XML callback safely (rejecting XXE / un-decrypted payloads)."""
    text = (xml_text or "").strip()
    if not text:
        raise ValueError("Empty WeCom XML payload")
    if _FORBIDDEN_XML_RE.search(text):
        raise ValueError("Forbidden XML DOCTYPE/ENTITY declaration")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid WeCom XML payload: {exc}") from exc

    parsed = {child.tag: (child.text or "").strip() for child in root}
    if "Encrypt" in parsed and not parsed.get("MsgType"):
        raise ValueError("Encrypted WeCom payload requires upstream AES decryption proxy")
    return parsed


async def fetch_wecom_access_token(
    client: httpx.AsyncClient,
    base_url: str,
    corp_id: str,
    corp_secret: str,
) -> tuple[str, int]:
    """Exchange WeCom `corpid` + `corpsecret` for an access token via official `gettoken`."""
    if not corp_id.strip() or not corp_secret.strip():
        raise RuntimeError("WeCom corp_id and corp_secret must be configured")
    resp = await client.get(
        f"{base_url.rstrip('/')}/gettoken",
        params={"corpid": corp_id.strip(), "corpsecret": corp_secret.strip()},
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or payload.get("errcode", 0) != 0:
        raise RuntimeError(f"WeCom gettoken failed: {payload}")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("WeCom gettoken returned empty access_token")
    ttl = int(payload.get("expires_in") or 7200)
    return token, ttl


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return ""


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


class WeComAdapter:
    """WeCom (Enterprise WeChat) ChannelAdapter backed by verified webhook ingest."""

    name = "wecom"

    def __init__(
        self,
        config: WeComConfig,
        *,
        inbox_path: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        raw_inbox = inbox_path or (
            Path(config.inbox_path).expanduser()
            if config.inbox_path
            else Path("~/.local/share/watchpost/webhooks/wecom.jsonl").expanduser()
        )
        self._inbox_path = Path(raw_inbox)
        self._owner_user_id = config.owner_user_id.strip()
        self._owner_name = config.owner_name.strip()
        self._base_url = (config.base_url or "https://qyapi.weixin.qq.com/cgi-bin").strip().rstrip("/")
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
        token, ttl = await fetch_wecom_access_token(
            self._client(),
            self._base_url,
            self.config.corp_id,
            self.config.corp_secret,
        )
        self._token_value = token
        self._token_expires_at = now + ttl
        return token

    def ingest_webhook_payload(self, payload: dict[str, Any] | str) -> NormalizedMessage:
        """Normalize and persist an incoming WeCom XML or JSON event into the JSONL inbox."""
        data: dict[str, Any] = parse_wecom_xml(payload) if isinstance(payload, str) else dict(payload)

        sender_id = str(_pick(data, "FromUserName", "from_user", "sender_id")).strip()
        room_id = str(_pick(data, "ChatId", "RoomId", "chat_id", "room_id")).strip()
        chat_id = room_id or sender_id
        if not chat_id:
            raise ValueError("WeCom payload missing ChatId/FromUserName")

        chat_type = "group" if room_id else "p2p"
        sender_name = str(_pick(data, "SenderName", "sender_name") or sender_id or "Unknown").strip()
        chat_name = str(_pick(data, "ChatName", "chat_name") or (chat_id if room_id else sender_name)).strip()

        raw_create = _pick(data, "CreateTime", "create_time", "timestamp")
        try:
            create_num = int(raw_create) if raw_create else int(time.time())
            create_ms = create_num * 1000 if 0 < create_num < 10_000_000_000 else create_num
        except (TypeError, ValueError):
            create_ms = int(time.time() * 1000)

        msg_id = str(_pick(data, "MsgId", "msg_id") or f"wc_{create_ms}_{sender_id or 'msg'}").strip()
        msg_type = str(_pick(data, "MsgType", "msg_type") or "text").strip().lower()
        clean_text = str(_pick(data, "Content", "content", "text")).strip()
        media_id = str(_pick(data, "MediaId", "media_id")).strip()
        pic_url = str(_pick(data, "PicUrl", "pic_url")).strip()
        file_name = str(_pick(data, "FileName", "file_name")).strip()

        media_refs: list[MediaRef] = []
        if msg_type == "image" and (media_id or pic_url):
            key = media_id or pic_url
            media_refs.append(MediaRef(kind="image", key=key, ext="jpg", extra={"url": pic_url, "name": f"{key}.jpg"}))
        elif msg_type in {"voice", "audio"} and media_id:
            media_refs.append(MediaRef(kind="audio", key=media_id, ext="amr", extra={"name": f"{media_id}.amr"}))
        elif msg_type == "file" and media_id:
            fname = file_name or f"{media_id}.bin"
            ext = Path(fname).suffix.lower().lstrip(".") or "bin"
            media_refs.append(MediaRef(kind="file", key=media_id, ext=ext, extra={"name": fname}))

        raw_mentioned = _pick(data, "MentionedList", "mentioned_list")
        if isinstance(raw_mentioned, list):
            mentioned_ids = [str(x).strip() for x in raw_mentioned if str(x).strip()]
        elif isinstance(raw_mentioned, str) and raw_mentioned.strip():
            mentioned_ids = [p.strip() for p in re.split(r"[,|]", raw_mentioned) if p.strip()]
        else:
            mentioned_ids = []

        mentions = [Mention(id=mid, name=mid, key=f"@{mid}") for mid in mentioned_ids]
        mentions_owner = bool(
            (self._owner_user_id and self._owner_user_id in mentioned_ids)
            or (self._owner_name and f"@{self._owner_name}" in clean_text)
        )
        mentions_all = "@all" in mentioned_ids or "@所有人" in clean_text
        is_owner = bool(self._owner_user_id and sender_id == self._owner_user_id)

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
            raw_content=json.dumps(data, ensure_ascii=False),
            clean_text=clean_text,
            mentions=mentions,
            mentions_owner=mentions_owner,
            mentions_all=mentions_all,
            media_refs=media_refs,
        )
        record = {
            "chat": {
                "external_chat_id": chat_id,
                "name": chat_name,
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
            msg = NormalizedMessage.model_validate(entry["message"]).model_copy(update={"channel_id": channel.id})
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
        artifacts: list[MediaArtifact] = []

        for ref in message.media_refs:
            try:
                direct_url = ref.key if ref.key.startswith(("http://", "https://")) else ""
                if direct_url:
                    resp = await self._client().get(direct_url, follow_redirects=True)
                else:
                    token = await self._get_access_token()
                    resp = await self._client().get(
                        f"{self._base_url}/media/get",
                        params={"access_token": token, "media_id": ref.key},
                        follow_redirects=True,
                    )
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").lower()
                if "application/json" in ctype:
                    err_obj = resp.json()
                    if isinstance(err_obj, dict) and err_obj.get("errcode", 0) != 0:
                        raise RuntimeError(f"WeCom media/get failed: {err_obj}")
                fname = _sanitize_filename(
                    str((ref.extra or {}).get("name") or ""),
                    fallback=ref.key,
                    default_ext=ref.ext or "bin",
                )
                dest_path = target_dir / f"{_SAFE_STEM_RE.sub('_', ref.key)[:40]}_{fname}"
                dest_path.write_bytes(resp.content)
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, path=str(dest_path)))
            except Exception as exc:
                artifacts.append(MediaArtifact(kind=ref.kind, key=ref.key, error=str(exc)))
        return artifacts
