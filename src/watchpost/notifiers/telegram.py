"""Cross-platform Telegram Bot NotifierAdapter (owner-only entrance)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from watchpost.adapters.telegram import TelegramUpdateHub
from watchpost.config import TelegramConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice
from watchpost.security import redact_secrets

logger = logging.getLogger("watchpost.notifiers.telegram")

_MAX_INBOUND_MEDIA_BYTES = 25 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def telegram_owner_offset_key(bot_token: str) -> str:
    """Return a bot-token-scoped kv_state key so hot-swapping bots never reuses stale offsets."""
    digest = hashlib.sha256((bot_token or "").strip().encode("utf-8")).hexdigest()[:12]
    return f"telegram_owner_update_offset:{digest}"

_COMMANDS_ZH = [
    {"command": "status", "description": "查看运行状态与待确认列表"},
    {"command": "list", "description": "查看最近 5 条会话（支持 #N）"},
    {"command": "brief", "description": "立即生成最近 12 小时简报"},
    {"command": "new", "description": "开启新会话"},
    {"command": "reset", "description": "清空当前对话上下文"},
    {"command": "mute", "description": "临时静音通知（如 /mute 2h）"},
    {"command": "unmute", "description": "恢复消息通知"},
    {"command": "help", "description": "查看全部指令说明"},
]

_COMMANDS_EN = [
    {"command": "status", "description": "View daemon status and pending approvals"},
    {"command": "list", "description": "List 5 recent threads (#N reference)"},
    {"command": "brief", "description": "Send latest 12h briefing now"},
    {"command": "new", "description": "Start a new conversation"},
    {"command": "reset", "description": "Clear current conversation context"},
    {"command": "mute", "description": "Temporarily mute notifications (e.g. /mute 2h)"},
    {"command": "unmute", "description": "Unmute notifications"},
    {"command": "help", "description": "Show command help"},
]


def _sanitize_media_filename(name: str, *, fallback: str, default_ext: str) -> str:
    base = Path(name or "").name.strip()
    base = _SAFE_NAME_RE.sub("_", base).strip("._")
    if not base:
        base = _SAFE_NAME_RE.sub("_", fallback).strip("._") or "attachment"
    if "." not in base and default_ext:
        base = f"{base}.{default_ext.lstrip('.')}"
    return base[:120]


def default_avatar_path() -> Path:
    """Return the bundled Watchpost bot avatar image path (JPEG for Bot API profile photos)."""
    base = Path(__file__).resolve().parent.parent / "assets"
    jpg = base / "avatar.jpg"
    return jpg if jpg.is_file() else (base / "avatar.png")


def _make_http_client(proxy_url: str = "", *, timeout_s: float = 20.0) -> httpx.AsyncClient:
    """Construct an AsyncClient using direct OS/TUN routing or an explicit proxy."""
    proxy = proxy_url.strip() or None
    return httpx.AsyncClient(timeout=timeout_s, proxy=proxy, trust_env=False)


def _raise_redacted(exc: Exception) -> RuntimeError:
    return RuntimeError(redact_secrets(str(exc)))


async def get_bot_info(
    bot_token: str,
    *,
    base_url: str = "https://api.telegram.org",
    proxy_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Verify bot credentials via Telegram `getMe` and return the bot profile dict."""
    token = bot_token.strip()
    if not token:
        raise RuntimeError("Telegram bot_token is required")
    url = f"{base_url.rstrip('/')}/bot{token}/getMe"
    owns_client = client is None
    http = client or _make_http_client(proxy_url, timeout_s=15.0)
    try:
        try:
            resp = await http.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(redact_secrets(f"Telegram getMe failed: {payload}"))
        result = payload.get("result")
        return dict(result) if isinstance(result, dict) else {}
    finally:
        if owns_client:
            await http.aclose()


async def get_webhook_info(
    bot_token: str,
    *,
    base_url: str = "https://api.telegram.org",
    proxy_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Inspect active Telegram Bot webhook configuration via `getWebhookInfo`."""
    token = bot_token.strip()
    if not token:
        raise RuntimeError("Telegram bot_token is required")
    url = f"{base_url.rstrip('/')}/bot{token}/getWebhookInfo"
    owns_client = client is None
    http = client or _make_http_client(proxy_url, timeout_s=15.0)
    try:
        try:
            resp = await http.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(redact_secrets(f"Telegram getWebhookInfo failed: {payload}"))
        result = payload.get("result")
        return dict(result) if isinstance(result, dict) else {}
    finally:
        if owns_client:
            await http.aclose()


async def detect_owner_chat_id(
    bot_token: str,
    *,
    base_url: str = "https://api.telegram.org",
    proxy_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """Inspect recent Telegram getUpdates messages to discover the owner's numeric chat_id."""
    if not bot_token.strip():
        raise RuntimeError("Telegram bot_token is required")
    url = f"{base_url.rstrip('/')}/bot{bot_token.strip()}/getUpdates"
    owns_client = client is None
    http = client or _make_http_client(proxy_url, timeout_s=15.0)
    try:
        try:
            resp = await http.get(url, params={"limit": 20, "timeout": 0})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not payload.get("ok"):
            raise RuntimeError(redact_secrets(f"Telegram getUpdates failed: {payload}"))
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


async def wait_for_owner_chat_id(
    bot_token: str,
    *,
    expected_nonce: str = "",
    timeout_s: float = 120.0,
    poll_interval_s: float = 2.0,
    base_url: str = "https://api.telegram.org",
    proxy_url: str = "",
    client: httpx.AsyncClient | None = None,
    on_tick: Callable[[], None] | None = None,
) -> dict[str, str] | None:
    """Poll `getUpdates` until the owner messages in a private chat (matching optional nonce)."""
    token = bot_token.strip()
    if not token:
        raise RuntimeError("Telegram bot_token is required")
    nonce = expected_nonce.strip()
    url = f"{base_url.rstrip('/')}/bot{token}/getUpdates"
    owns_client = client is None
    http = client or _make_http_client(proxy_url, timeout_s=15.0)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.01, float(poll_interval_s))
    try:
        while True:
            try:
                resp = await http.get(url, params={"limit": 50, "timeout": 0})
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                raise _raise_redacted(exc) from exc
            if isinstance(payload, dict) and payload.get("ok"):
                for upd in reversed(payload.get("result") or []):
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    if str(chat.get("type") or "").strip() != "private":
                        continue
                    cid = str(chat.get("id") or "").strip()
                    if not cid:
                        continue
                    txt = str(msg.get("text") or "").strip()
                    if nonce and txt not in {f"/start {nonce}", nonce}:
                        continue
                    return {
                        "chat_id": cid,
                        "type": "private",
                        "username": str(chat.get("username") or ""),
                        "name": " ".join(
                            p
                            for p in [str(chat.get("first_name") or "").strip(), str(chat.get("last_name") or "").strip()]
                            if p
                        ),
                    }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if on_tick is not None:
                on_tick()
            await asyncio.sleep(min(interval, remaining))
    finally:
        if owns_client:
            await http.aclose()


async def set_bot_profile_photo(
    bot_token: str,
    photo_path: str | Path,
    *,
    base_url: str = "https://api.telegram.org",
    proxy_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Upload a static profile avatar image for the bot via `setMyProfilePhoto`."""
    token = bot_token.strip()
    if not token:
        raise RuntimeError("Telegram bot_token is required")
    src = Path(photo_path).expanduser()
    if not src.is_file():
        return False
    mime_type = mimetypes.guess_type(src.name)[0] or "image/jpeg"
    content = src.read_bytes()
    url = f"{base_url.rstrip('/')}/bot{token}/setMyProfilePhoto"
    owns_client = client is None
    http = client or _make_http_client(proxy_url, timeout_s=30.0)
    try:
        try:
            resp = await http.post(
                url,
                data={"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
                files={"avatar": (src.name, content, mime_type)},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(redact_secrets(f"Telegram setMyProfilePhoto failed: {payload}"))
        return True
    finally:
        if owns_client:
            await http.aclose()


async def configure_bot_profile(
    bot_token: str,
    *,
    language: str = "zh",
    avatar_path: str | Path | None = None,
    set_avatar: bool = True,
    base_url: str = "https://api.telegram.org",
    proxy_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, bool]:
    """Configure Telegram slash commands, bot descriptions, and optional avatar image."""
    token = bot_token.strip()
    if not token:
        raise RuntimeError("Telegram bot_token is required")
    is_zh = (language or "zh").lower().startswith("zh")
    cmds = _COMMANDS_ZH if is_zh else _COMMANDS_EN
    short_desc = (
        "Watchpost 本地 AI 值班哨兵 — 群消息分诊、排查修复与一键批准"
        if is_zh
        else "Watchpost local AI sentinel — chat triage, fix drafts & 1-tap approvals"
    )
    full_desc = (
        "Watchpost 运行在你的本机，安静监听工作群，只将排查结论、修复草案与一键确认按钮发给你，绝不自动外发工作群消息。"
        if is_zh
        else "Watchpost runs silently on your machine, triages noisy work chats with your coding agent, and sends concise updates plus 1-tap approval buttons here."
    )
    base = f"{base_url.rstrip('/')}/bot{token}"
    owns_client = client is None
    http = client or _make_http_client(proxy_url, timeout_s=30.0)
    results = {
        "commands": False,
        "short_description": False,
        "description": False,
        "avatar": False,
    }
    try:
        for key, endpoint, payload in (
            ("commands", "setMyCommands", {"commands": cmds}),
            ("short_description", "setMyShortDescription", {"short_description": short_desc}),
            ("description", "setMyDescription", {"description": full_desc}),
        ):
            try:
                resp = await http.post(f"{base}/{endpoint}", json=payload)
                resp.raise_for_status()
                body = resp.json()
                results[key] = bool(isinstance(body, dict) and body.get("ok"))
            except Exception as exc:
                logger.warning("Telegram %s failed: %s", endpoint, redact_secrets(str(exc)))

        if set_avatar:
            resolved_avatar = Path(avatar_path).expanduser() if avatar_path else default_avatar_path()
            if resolved_avatar.is_file():
                try:
                    results["avatar"] = await set_bot_profile_photo(
                        token,
                        resolved_avatar,
                        base_url=base_url,
                        proxy_url=proxy_url,
                        client=http,
                    )
                except Exception as exc:
                    logger.warning("Telegram setMyProfilePhoto failed: %s", redact_secrets(str(exc)))
        return results
    finally:
        if owns_client:
            await http.aclose()


class TelegramNotifier:
    """Delivers Watchpost bubbles/media to the owner via Telegram Bot API and polls replies/taps."""

    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        *,
        hub: TelegramUpdateHub | None = None,
        store: Any = None,
        media_dir: Path | str | None = None,
        language: str = "zh",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._token = config.bot_token.strip()
        self._owner_chat_id = str(config.owner_chat_id or "").strip()
        base_root = config.base_url.rstrip("/")
        self._base_url = f"{base_root}/bot{self._token}"
        self._file_base = f"{base_root}/file/bot{self._token}"
        self._hub = hub
        self._store = store
        self._media_dir = Path(media_dir).expanduser() if media_dir else None
        self._language = (language or "zh").strip()
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._offset_kv_key = telegram_owner_offset_key(self._token)
        saved_off = self._store.get_kv(self._offset_kv_key) if self._store is not None else None
        self._offset: int | None = int(saved_off) if isinstance(saved_off, int) and saved_off > 0 else None
        self._committed_offset: int | None = self._offset
        self._pending_offset: int | None = None
        self._backoff_s = 0.0
        self._backoff_until = 0.0
        if self._hub is not None and self._owner_chat_id:
            register_fn = getattr(self._hub, "register_owner_chat_id", None)
            if callable(register_fn):
                register_fn(self._owner_chat_id)

    @property
    def hub(self) -> TelegramUpdateHub | None:
        return self._hub

    @property
    def owner_chat_id(self) -> str:
        return self._owner_chat_id

    def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            proxy_url = getattr(self._config, "proxy_url", "")
            self._owned_client = _make_http_client(
                proxy_url,
                timeout_s=max(15.0, float(self._config.poll_timeout_s) + 10.0),
            )
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
        if self._hub is not None:
            await self._hub.aclose()

    def _persist_offset(self) -> None:
        if self._store is not None and self._offset is not None:
            self._store.set_kv(self._offset_kv_key, int(self._offset))
        self._committed_offset = self._offset
        self._pending_offset = None

    def commit_offset(self) -> None:
        """Persist any deferred Telegram update cursor after owner actions have been processed."""
        if self._hub is not None:
            commit_fn = getattr(self._hub, "commit_offset", None)
            if callable(commit_fn):
                commit_fn()
            return
        if self._pending_offset is not None:
            self._offset = max(self._offset or 0, self._pending_offset)
            self._persist_offset()

    def abort_deferred_offset(self) -> None:
        """Roll back uncommitted cursor if owner dispatch is cancelled."""
        if self._hub is not None:
            abort_fn = getattr(self._hub, "abort_deferred_offset", None)
            if callable(abort_fn):
                abort_fn()
            return
        self._offset = self._committed_offset
        self._pending_offset = None

    async def _post_with_retry(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client().post(f"{self._base_url}/{endpoint}", **kwargs)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    retry_after = float(resp.headers.get("Retry-After") or 1.0)
                    await asyncio.sleep(min(5.0, max(0.2, retry_after)))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or not payload.get("ok"):
                    raise RuntimeError(redact_secrets(f"Telegram {endpoint} failed: {payload}"))
                return payload
            except httpx.TransportError as exc:
                last_exc = _raise_redacted(exc)
                if attempt == 0:
                    continue
                raise last_exc from exc
            except httpx.HTTPStatusError as exc:
                raise _raise_redacted(exc) from exc
        raise last_exc or RuntimeError(f"Telegram {endpoint} failed")

    async def seed_offset(self) -> int:
        """Advance past pending backlog updates once when no saved cursor exists."""
        if self._hub is not None:
            return await self._hub.seed_offset()
        if not self._token or not self._owner_chat_id or self._offset is not None:
            return 0
        try:
            resp = await self._client().get(
                f"{self._base_url}/getUpdates",
                params={"timeout": 0, "allowed_updates": '["message","callback_query"]'},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            return 0
        results = payload.get("result") or []
        max_id = max(
            (upd["update_id"] for upd in results if isinstance(upd.get("update_id"), int)),
            default=None,
        )
        if max_id is not None:
            self._offset = max_id + 1
            self._persist_offset()
        return len(results)

    async def send_message(self, text: str, *, reply_markup: dict[str, Any] | None = None) -> str:
        if not self._token or not self._owner_chat_id:
            raise RuntimeError("Telegram bot_token and owner_chat_id must be configured")
        body = (text or "").strip()
        if not body:
            return ""
        json_body: dict[str, Any] = {
            "chat_id": self._owner_chat_id,
            "text": body[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            json_body["reply_markup"] = reply_markup
        payload = await self._post_with_retry("sendMessage", json=json_body)
        msg_id = (payload.get("result") or {}).get("message_id")
        return str(msg_id) if msg_id is not None else ""

    async def send_photo(self, photo_path: str | Path, *, caption: str = "") -> str:
        if not self._token or not self._owner_chat_id:
            raise RuntimeError("Telegram bot_token and owner_chat_id must be configured")
        src = Path(photo_path).expanduser()
        if not src.is_file():
            return ""
        data: dict[str, str] = {"chat_id": self._owner_chat_id}
        if caption.strip():
            data["caption"] = caption.strip()[:1024]
        content = src.read_bytes()
        payload = await self._post_with_retry(
            "sendPhoto",
            data=data,
            files={"photo": (src.name, content)},
        )
        msg_id = (payload.get("result") or {}).get("message_id")
        return str(msg_id) if msg_id is not None else ""

    async def send_typing(self) -> None:
        if not self._token or not self._owner_chat_id:
            return
        try:
            await self._client().post(
                f"{self._base_url}/sendChatAction",
                json={"chat_id": self._owner_chat_id, "action": "typing"},
            )
        except Exception:
            pass

    async def register_commands(self) -> bool:
        if not self._token:
            return False
        cmds = _COMMANDS_ZH if self._language.lower().startswith("zh") else _COMMANDS_EN
        try:
            await self._post_with_retry("setMyCommands", json={"commands": cmds})
            return True
        except Exception as exc:
            logger.warning("Telegram setMyCommands failed: %s", exc)
            return False

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
        img_list = [p for p in (attachments or []) if p and Path(p).expanduser().is_file()][:4]
        msg = format_start_notice(
            label,
            preview,
            adapter=channel.adapter,
            is_steer=is_steer,
            has_images=bool(img_list),
            language=self._language,
        )
        tokens: list[str] = []
        tok = await self.send_message(msg)
        if tok:
            tokens.append(tok)
        for img in img_list:
            try:
                img_tok = await self.send_photo(img)
                if img_tok:
                    tokens.append(img_tok)
            except Exception as exc:
                logger.warning("Telegram sendPhoto failed (%s): %s", img, exc)
        return tokens

    def _build_go_keyboard(self, thread_id: str) -> dict[str, Any]:
        if self._language.lower().startswith("zh"):
            approve_label, skip_label = "✅ 批准执行 (go)", "⏭ 忽略 (skip)"
        else:
            approve_label, skip_label = "✅ Approve go", "⏭ Skip"
        return {
            "inline_keyboard": [
                [
                    {"text": approve_label, "callback_data": f"wp:go:{thread_id}"},
                    {"text": skip_label, "callback_data": f"wp:skip:{thread_id}"},
                ]
            ]
        }

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        del channel
        tokens: list[str] = []
        reply_markup = self._build_go_keyboard(thread.id) if (thread.pending_go and thread.id) else None
        last_idx = len(bubbles) - 1
        for idx, bubble in enumerate(bubbles):
            markup = reply_markup if (reply_markup is not None and idx == last_idx) else None
            tok = await self.send_message(bubble, reply_markup=markup)
            if tok:
                tokens.append(tok)
        return tokens

    def _owner_media_dir(self) -> Path:
        base = self._media_dir if self._media_dir is not None else Path(tempfile.gettempdir()) / "watchpost_telegram_media"
        target = base / "telegram_owner"
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def _download_file(self, file_id: str) -> tuple[bytes, str]:
        if self._hub is not None:
            return await self._hub.download_file(file_id)
        try:
            resp = await self._client().get(f"{self._base_url}/getFile", params={"file_id": file_id})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(redact_secrets(f"Telegram getFile failed: {payload}"))
        file_path = str((payload.get("result") or {}).get("file_path") or "").lstrip("/")
        if not file_path:
            raise RuntimeError(f"Telegram getFile returned empty file_path for {file_id}")
        try:
            dl = await self._client().get(f"{self._file_base}/{file_path}", follow_redirects=True)
            dl.raise_for_status()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        return dl.content, Path(file_path).name

    async def _extract_message_attachments(self, msg: dict[str, Any]) -> list[dict[str, str]]:
        specs: list[tuple[str, str, str, bool, int]] = []
        photos = msg.get("photo") or []
        if isinstance(photos, list) and photos:
            largest = photos[-1]
            if isinstance(largest, dict) and largest.get("file_id"):
                fid = str(largest["file_id"])
                specs.append((fid, f"{fid}.jpg", "jpg", False, int(largest.get("file_size") or 0)))

        for field_name, default_ext in (("voice", "ogg"), ("audio", "mp3")):
            item = msg.get(field_name)
            if isinstance(item, dict) and item.get("file_id"):
                fid = str(item["file_id"])
                fname = str(item.get("file_name") or f"{fid}.{default_ext}")
                specs.append((fid, fname, default_ext, True, int(item.get("file_size") or 0)))

        doc = msg.get("document")
        if isinstance(doc, dict) and doc.get("file_id"):
            fid = str(doc["file_id"])
            fname = str(doc.get("file_name") or f"{fid}.bin")
            mime = str(doc.get("mime_type") or "").lower()
            ext = Path(fname).suffix.lower().lstrip(".") or "bin"
            is_audio = mime.startswith("audio/") or ext in {"caf", "m4a", "aac", "amr", "wav", "mp3", "ogg"}
            specs.append((fid, fname, ext, is_audio, int(doc.get("file_size") or 0)))

        if not specs:
            return []

        dest_dir = self._owner_media_dir()
        msg_id = str(msg.get("message_id") or uuid.uuid4().hex[:8])
        attachments: list[dict[str, str]] = []
        for idx, (fid, hint_name, default_ext, is_audio, declared_size) in enumerate(specs):
            if declared_size > _MAX_INBOUND_MEDIA_BYTES:
                logger.warning("Skipping oversized Telegram owner attachment (%s bytes)", declared_size)
                continue
            try:
                content, remote_name = await self._download_file(fid)
                if len(content) > _MAX_INBOUND_MEDIA_BYTES:
                    logger.warning("Skipping downloaded Telegram owner attachment over 25MB (%s bytes)", len(content))
                    continue
                fname = _sanitize_media_filename(
                    hint_name or remote_name,
                    fallback=f"tg_{msg_id}_{idx}",
                    default_ext=default_ext,
                )
                dest_path = dest_dir / f"{_SAFE_NAME_RE.sub('_', msg_id)}_{idx}_{fname}"
                dest_path.write_bytes(content)
                attachments.append(
                    {
                        "kind": "audio" if is_audio else "file",
                        "name": fname,
                        "path": str(dest_path),
                        "is_audio": "true" if is_audio else "false",
                    }
                )
            except Exception as exc:
                logger.warning("Failed downloading Telegram owner attachment %s: %s", fid, exc)
        return attachments

    async def _handle_callback_query(self, cb: dict[str, Any]) -> OwnerAction | None:
        sender = cb.get("from") or {}
        if sender.get("is_bot"):
            return None
        cb_msg = cb.get("message") or {}
        chat_id = str((cb_msg.get("chat") or {}).get("id") or "").strip()
        if chat_id != self._owner_chat_id:
            return None
        data = str(cb.get("data") or "").strip()
        cb_id = str(cb.get("id") or "").strip()
        msg_id = cb_msg.get("message_id")

        kind = ""
        thread_id = ""
        text = ""
        if data.startswith("wp:go:"):
            kind = "approve_go"
            thread_id = data[len("wp:go:") :].strip()
            text = "go"
        elif data.startswith("wp:skip:"):
            kind = "dismiss_go"
            thread_id = data[len("wp:skip:") :].strip()
            text = "skip"
        else:
            return None
        if not thread_id:
            return None

        if cb_id:
            ack_text = (
                "已批准执行"
                if (kind == "approve_go" and self._language.lower().startswith("zh"))
                else "已忽略"
                if self._language.lower().startswith("zh")
                else "Approved"
                if kind == "approve_go"
                else "Skipped"
            )
            try:
                await self._client().post(
                    f"{self._base_url}/answerCallbackQuery",
                    json={"callback_query_id": cb_id, "text": ack_text},
                )
            except Exception:
                pass
        if isinstance(msg_id, int):
            try:
                await self._client().post(
                    f"{self._base_url}/editMessageReplyMarkup",
                    json={
                        "chat_id": self._owner_chat_id,
                        "message_id": msg_id,
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            except Exception:
                pass

        return OwnerAction(
            notifier=self.name,
            kind=kind,
            thread_id=thread_id,
            text=text,
            reply_token=str(msg_id) if isinstance(msg_id, int) else "",
        )

    async def poll_owner_actions(self, *, defer_commit: bool = False) -> list[OwnerAction]:
        if not self._token or not self._owner_chat_id:
            return []

        if self._hub is not None:
            try:
                poll_events = getattr(self._hub, "poll_owner_events", None)
                if callable(poll_events):
                    raw_msgs, raw_cbs = await poll_events(self._owner_chat_id, defer_commit=defer_commit)
                else:
                    raw_msgs = await self._hub.poll_owner_messages(self._owner_chat_id, defer_commit=defer_commit)
                    poll_cbs = getattr(self._hub, "poll_owner_callbacks", None)
                    raw_cbs = (
                        await poll_cbs(self._owner_chat_id, defer_commit=defer_commit)
                        if callable(poll_cbs)
                        else []
                    )
                return await self._parse_owner_events(raw_msgs, raw_cbs)
            except BaseException:
                if defer_commit:
                    self.abort_deferred_offset()
                raise

        now_mono = time.monotonic()
        if now_mono < self._backoff_until:
            return []

        params: dict[str, Any] = {
            "timeout": max(0, int(self._config.poll_timeout_s)),
            "allowed_updates": '["message","callback_query"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            resp = await self._client().get(f"{self._base_url}/getUpdates", params=params)
        except httpx.TransportError as exc:
            self._backoff_s = min(60.0, max(2.0, self._backoff_s * 2.0 if self._backoff_s else 2.0))
            self._backoff_until = time.monotonic() + self._backoff_s
            logger.warning(
                "Telegram getUpdates transport error (backing off %.0fs): %s",
                self._backoff_s,
                redact_secrets(str(exc)),
            )
            return []

        if resp.status_code in {409, 429, 500, 502, 503, 504}:
            retry_after = 0.0
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After") or 0.0)
                except ValueError:
                    retry_after = 0.0
            self._backoff_s = min(60.0, max(2.0, retry_after or (self._backoff_s * 2.0 if self._backoff_s else 2.0)))
            self._backoff_until = time.monotonic() + self._backoff_s
            if resp.status_code == 409:
                logger.warning(
                    "Telegram getUpdates 409 Conflict (another Watchpost instance or active webhook is using this bot token; backing off %.0fs)",
                    self._backoff_s,
                )
            else:
                logger.warning(
                    "Telegram getUpdates HTTP %d (backing off %.0fs)",
                    resp.status_code,
                    self._backoff_s,
                )
            return []

        try:
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise _raise_redacted(exc) from exc
        if not payload.get("ok"):
            raise RuntimeError(redact_secrets(f"Telegram getUpdates failed: {payload}"))

        self._backoff_s = 0.0
        self._backoff_until = 0.0
        prev_offset = self._offset
        raw_msgs: list[dict[str, Any]] = []
        raw_cbs: list[dict[str, Any]] = []
        for upd in payload.get("result") or []:
            upd_id = upd.get("update_id")
            if isinstance(upd_id, int):
                self._offset = max(self._offset or 0, upd_id + 1)

            msg = upd.get("message") or {}
            if isinstance(msg, dict) and msg:
                chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
                # Strictly reject messages from any chat other than the configured owner
                if chat_id == self._owner_chat_id:
                    raw_msgs.append(msg)

            cb = upd.get("callback_query") or {}
            if isinstance(cb, dict) and cb:
                cb_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "").strip()
                if cb_chat == self._owner_chat_id:
                    raw_cbs.append(cb)

        if self._offset != prev_offset:
            if defer_commit:
                self._pending_offset = self._offset
            else:
                self._persist_offset()
        try:
            return await self._parse_owner_events(raw_msgs, raw_cbs)
        except BaseException:
            if defer_commit:
                self.abort_deferred_offset()
            raise

    async def _parse_owner_events(
        self,
        messages: list[dict[str, Any]],
        callbacks: list[dict[str, Any]] | None = None,
    ) -> list[OwnerAction]:
        actions: list[OwnerAction] = []
        for cb in callbacks or []:
            act = await self._handle_callback_query(cb)
            if act is not None:
                actions.append(act)

        for msg in messages:
            sender = msg.get("from") or {}
            if sender.get("is_bot"):
                continue
            text = str(msg.get("text") or msg.get("caption") or "").strip()
            attachments = await self._extract_message_attachments(msg)
            if not text and not attachments:
                continue
            reply_msg = msg.get("reply_to_message") or {}
            reply_token = str(reply_msg.get("message_id") or "")
            action = classify_owner_command(
                text,
                notifier=self.name,
                reply_token=reply_token,
                attachments=attachments,
            )
            if action is not None:
                actions.append(action)
        return actions
