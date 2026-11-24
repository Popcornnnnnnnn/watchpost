"""Out-of-process external NotifierAdapter speaking JSON over subprocess stdin/stdout."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from watchpost.config import ExternalNotifierConfig
from watchpost.models import ChannelConfig, OwnerAction, ThreadRecord
from watchpost.security import kill_process_tree


def _parse_tokens(res: Any) -> list[str]:
    if isinstance(res, list):
        return [str(tok).strip() for tok in res if str(tok).strip()]
    if isinstance(res, dict):
        raw = res.get("tokens")
        if isinstance(raw, list):
            return [str(tok).strip() for tok in raw if str(tok).strip()]
        single = res.get("token")
        if single:
            return [str(single).strip()]
    return []


class ExternalCommandNotifier:
    """Runs an out-of-process executable/script implementing Watchpost's JSON notifier contract."""

    def __init__(self, config: ExternalNotifierConfig) -> None:
        if not config.name or not config.command:
            raise ValueError("ExternalNotifierConfig requires non-empty name and command")
        self.name = config.name
        self._config = config

    async def _invoke(self, action: str, payload: dict[str, Any]) -> Any:
        env = os.environ.copy()
        env.update(self._config.env)
        cmd = [*self._config.command, action]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        stdin_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=self._config.timeout_s)
        except BaseException:
            await kill_process_tree(proc)
            raise
        stdout = out_b.decode("utf-8", errors="replace").strip()
        stderr = err_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"Notifier plugin {self.name} ({action}) failed (rc={proc.returncode}): {stderr or stdout}")
        if not stdout:
            return {}
        return json.loads(stdout)

    async def health(self) -> dict[str, Any]:
        res = await self._invoke("health", {})
        return dict(res) if isinstance(res, dict) else {"ok": True, "detail": str(res)}

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
        attachments: list[str] | None = None,
    ) -> list[str]:
        res = await self._invoke(
            "notify-start",
            {
                "channel": channel.model_dump(mode="json"),
                "thread": thread.model_dump(mode="json"),
                "preview": preview,
                "is_steer": bool(is_steer),
                "attachments": [str(Path(a)) for a in (attachments or []) if a],
            },
        )
        return _parse_tokens(res)

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        res = await self._invoke(
            "notify-completion",
            {
                "channel": channel.model_dump(mode="json"),
                "thread": thread.model_dump(mode="json"),
                "bubbles": list(bubbles),
            },
        )
        return _parse_tokens(res)

    async def send_message(self, text: str) -> str:
        res = await self._invoke("send-message", {"text": text})
        if isinstance(res, dict):
            return str(res.get("token") or "")
        if isinstance(res, str):
            return res.strip()
        return ""

    async def poll_owner_actions(self) -> list[OwnerAction]:
        res = await self._invoke("poll-actions", {})
        raw_actions = res if isinstance(res, list) else (res.get("actions") if isinstance(res, dict) else [])
        out: list[OwnerAction] = []
        for item in raw_actions or []:
            if isinstance(item, dict):
                normalized = dict(item)
                normalized.setdefault("notifier", self.name)
                out.append(OwnerAction.model_validate(normalized))
        return out
