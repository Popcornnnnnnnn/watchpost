"""Primary-with-automatic-backup AgentBackend wrapper (e.g., Antigravity -> Codex)."""

from __future__ import annotations

import logging
from pathlib import Path

from watchpost.models import AgentResult, ChannelConfig
from watchpost.ports import AgentBackend

logger = logging.getLogger("watchpost.backends.fallback")


class FallbackBackend:
    """Runs primary backend first and transparently fails over to backup on exception or error."""

    def __init__(self, primary: AgentBackend, backup: AgentBackend) -> None:
        self.primary = primary
        self.backup = backup
        self.name = f"{primary.name}+{backup.name}"

    def _encode_ref(self, provider: str, raw_ref: str) -> str:
        return f"{provider}:{raw_ref}"

    def _decode_ref(self, backend_thread_ref: str) -> tuple[str, str]:
        for prefix in (f"{self.primary.name}:", f"{self.backup.name}:"):
            if backend_thread_ref.startswith(prefix):
                return prefix[:-1], backend_thread_ref[len(prefix) :]
        return self.primary.name, backend_thread_ref

    async def start_thread(
        self,
        title: str,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        channel: ChannelConfig | None = None,
    ) -> tuple[str, AgentResult]:
        try:
            ref, res = await self.primary.start_thread(title, prompt, cwd=cwd, model=model, channel=channel)
            if ref and not res.error:
                return self._encode_ref(self.primary.name, ref), res
            err_msg = res.error or "empty thread ref"
        except Exception as exc:
            err_msg = str(exc)

        logger.warning(
            "Primary backend %s failed (%s); failing over to %s",
            self.primary.name,
            err_msg,
            self.backup.name,
        )
        b_ref, b_res = await self.backup.start_thread(title, prompt, cwd=cwd, model=model, channel=channel)
        return self._encode_ref(self.backup.name, b_ref), b_res

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel: ChannelConfig | None = None,
    ) -> AgentResult:
        provider, raw_ref = self._decode_ref(backend_thread_ref)
        target = self.backup if provider == self.backup.name else self.primary

        try:
            res = await target.steer_thread(raw_ref, title, message, cwd=cwd, channel=channel)
            if not res.error:
                return res
            err_msg = res.error
        except Exception as exc:
            err_msg = str(exc)

        if target is self.backup:
            return AgentResult(raw_text=err_msg, error=err_msg)

        logger.warning(
            "Primary backend %s steer failed (%s); starting recovery turn on %s",
            self.primary.name,
            err_msg,
            self.backup.name,
        )
        recovery_prompt = f"[Backup recovery after primary turn error: {err_msg}]\nTitle: {title}\n\n{message}"
        _b_ref, b_res = await self.backup.start_thread(title, recovery_prompt, cwd=cwd, channel=channel)
        return b_res
