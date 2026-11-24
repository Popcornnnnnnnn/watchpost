"""Out-of-process external ChannelAdapter speaking JSON over subprocess stdin/stdout."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from watchpost.config import ExternalAdapterConfig
from watchpost.models import ChannelConfig, MediaArtifact, NormalizedMessage
from watchpost.security import kill_process_tree


class ExternalCommandAdapter:
    """Runs an out-of-process executable/script implementing Watchpost's JSON plugin contract."""

    def __init__(self, config: ExternalAdapterConfig) -> None:
        if not config.name or not config.command:
            raise ValueError("ExternalAdapterConfig requires non-empty name and command")
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
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            raise
        stdout = out_b.decode("utf-8", errors="replace").strip()
        stderr = err_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"Plugin {self.name} ({action}) failed (rc={proc.returncode}): {stderr or stdout}")
        if not stdout:
            return {}
        return json.loads(stdout)

    async def health(self) -> dict[str, Any]:
        res = await self._invoke("health", {})
        return dict(res) if isinstance(res, dict) else {"ok": True, "detail": str(res)}

    async def list_available_chats(self) -> list[dict[str, str]]:
        res = await self._invoke("list-chats", {})
        items = res if isinstance(res, list) else (res.get("chats") if isinstance(res, dict) else [])
        out: list[dict[str, str]] = []
        for item in items or []:
            if isinstance(item, dict) and item.get("external_chat_id"):
                out.append(
                    {
                        "external_chat_id": str(item["external_chat_id"]),
                        "name": str(item.get("name") or item["external_chat_id"]),
                        "chat_type": str(item.get("chat_type") or "p2p"),
                    }
                )
        return out

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        res = await self._invoke("fetch-recent", {"channel": channel.model_dump(mode="json"), "limit": int(limit)})
        raw_msgs = res if isinstance(res, list) else (res.get("messages") if isinstance(res, dict) else [])
        return [NormalizedMessage.model_validate(item) for item in raw_msgs or []]

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        res = await self._invoke(
            "fetch-by-ids",
            {"channel": channel.model_dump(mode="json"), "message_ids": list(message_ids)},
        )
        raw_msgs = res if isinstance(res, list) else (res.get("messages") if isinstance(res, dict) else [])
        return [NormalizedMessage.model_validate(item) for item in raw_msgs or []]

    async def download_media(
        self,
        channel: ChannelConfig,
        message: NormalizedMessage,
        dest_dir: Path,
    ) -> list[MediaArtifact]:
        res = await self._invoke(
            "download-media",
            {
                "channel": channel.model_dump(mode="json"),
                "message": message.model_dump(mode="json"),
                "dest_dir": str(dest_dir),
            },
        )
        raw_arts = res if isinstance(res, list) else (res.get("artifacts") if isinstance(res, dict) else [])
        return [MediaArtifact.model_validate(item) for item in raw_arts or []]
