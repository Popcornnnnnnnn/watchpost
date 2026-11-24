"""Cross-platform Telegram Bot NotifierAdapter (owner-only entrance)."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

from watchpost.adapters.telegram import TelegramUpdateHub
from watchpost.config import TelegramConfig
from watchpost.models import ChannelConfig, ChatType, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice

logger = logging.getLogger("watchpost.notifiers.telegram")

_MAX_INBOUND_MEDIA_BYTES = 25 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_OFFSET_KV_KEY = "telegram_owner_update_offset"

_COMMANDS_ZH = [
    {"command": "status", "description": "查看运行状态与待确认列表"},
    {"command": "list", "description": "查看最近 5 条会话（支持 #N）"},
    {"command": "brief", "description": "立即生成最近 12 小时简报"},
    {"command": "new", "description": "开启新的个人直聊会话"},
    {"command": "reset", "description": "重置个人直聊上下文"},
    {"command": "mute", "description": "临时静音通知（如 /mute 2h）"},
    {"command": "unmute", "description": "恢复消息通知"},
    {"command": "help", "description": "查看全部指令说明"},
]

_COMMANDS_EN = [
    {"command": "status", "description": "View daemon status and pending approvals"},
    {"command": "list", "description": "List 5 recent threads (#N reference)"},
    {"command": "brief", "description": "Send latest 12h briefing now"},
    {"command": "new", "description": "Start a new Personal Direct Chat"},
    {"command": "reset", "description": "Reset Personal Direct Chat context"},
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
    http = client or httpx.AsyncClient(timeout=15.0, proxy=proxy_url.strip() or None)
    try:
        resp = await http.get(url, params={"limit": 20, "timeout": 0})
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
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
        saved_off = self._store.get_kv(_OFFSET_KV_KEY) if self._store is not None else None
        self._offset: int | None = int(saved_off) if isinstance(saved_off, int) and saved_off > 0 else None

    @property
    def hub(self) -> TelegramUpdateHub | None:
        return self._hub

    def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            proxy_url = getattr(self._config, "proxy_url", "").strip() or None
            self._owned_client = httpx.AsyncClient(
                timeout=max(15.0, float(self._config.poll_timeout_s) + 10.0),
                proxy=proxy_url,
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
            self._store.set_kv(_OFFSET_KV_KEY, int(self._offset))

    async def _post_with_retry(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client().post(f"{self._base_url}/{endpoint}", **kwargs)
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or not payload.get("ok"):
                    raise RuntimeError(f"Telegram {endpoint} failed: {payload}")
                return payload
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                raise
        raise last_exc or RuntimeError(f"Telegram {endpoint} failed")

    async def seed_offset(self) -> int:
        """Advance past pending backlog updates once when no saved cursor exists."""
        if self._hub is not None:
            return await self._hub.seed_offset()
        if not self._token or not self._owner_chat_id or self._offset is not None:
            return 0
        resp = await self._client().get(
            f"{self._base_url}/getUpdates",
            params={"timeout": 0, "allowed_updates": '["message","callback_query"]'},
        )
        resp.raise_for_status()
        payload = resp.json()
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
        resp = await self._client().get(f"{self._base_url}/getFile", params={"file_id": file_id})
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

    async def poll_owner_actions(self) -> list[OwnerAction]:
        if not self._token or not self._owner_chat_id:
            return []

        if self._hub is not None:
            raw_msgs = await self._hub.poll_owner_messages(self._owner_chat_id)
            poll_cbs = getattr(self._hub, "poll_owner_callbacks", None)
            raw_cbs = await poll_cbs(self._owner_chat_id) if callable(poll_cbs) else []
            return await self._parse_owner_events(raw_msgs, raw_cbs)

        params: dict[str, Any] = {
            "timeout": max(0, int(self._config.poll_timeout_s)),
            "allowed_updates": '["message","callback_query"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        resp = await self._client().get(f"{self._base_url}/getUpdates", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")

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
            self._persist_offset()
        return await self._parse_owner_events(raw_msgs, raw_cbs)

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
