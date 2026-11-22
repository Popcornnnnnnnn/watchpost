"""Terminal ConsoleNotifier rendering phone bubbles and queueing interactive owner replies."""

from __future__ import annotations

import sys
from typing import TextIO

from watchpost.models import ChannelConfig, OwnerAction, ThreadRecord
from watchpost.notifiers import classify_owner_command, format_start_notice


class ConsoleNotifier:
    """Renders mobile-sized phone bubbles to a terminal stream without touching external chat apps."""

    name = "console"

    def __init__(self, *, stream: TextIO | None = None, language: str = "zh") -> None:
        self._stream = stream
        self._language = language
        self._seq = 0
        self.history: list[tuple[str, str]] = []
        self._owner_queue: list[OwnerAction] = []

    def _out(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    def _next_token(self) -> str:
        self._seq += 1
        return f"c-{self._seq}"

    def enqueue_owner_text(self, text: str, *, reply_token: str = "") -> OwnerAction | None:
        action = classify_owner_command(text, notifier=self.name, reply_token=reply_token)
        if action is not None:
            self._owner_queue.append(action)
        return action

    async def send_message(self, text: str) -> str:
        clean = (text or "").strip()
        if not clean:
            return ""
        tok = self._next_token()
        self.history.append((tok, clean))
        out = self._out()
        indented = "\n  ".join(clean.splitlines())
        out.write(f"  [Phone {tok}] {indented}\n")
        out.flush()
        return tok

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
        attachments: list[str] | None = None,
    ) -> list[str]:
        del thread, attachments
        msg = format_start_notice(
            channel.name,
            preview,
            adapter=channel.adapter,
            is_steer=is_steer,
            language=self._language,
        )
        tok = await self.send_message(msg)
        return [tok] if tok else []

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        del channel
        tokens: list[str] = []
        out = self._out()
        out.write(f"\n--- Phone Inbox ({thread.triage_mode.value} | {thread.status.value}) ---\n")
        for bubble in bubbles:
            tok = await self.send_message(bubble)
            if tok:
                tokens.append(tok)
        if thread.pending_go:
            hint = (
                "  -> 等待确认执行：输入 `/phone go`，或 `/phone @<气泡编号> <补充说明>`。\n"
                if self._language.lower().startswith("zh")
                else "  -> Awaiting approval: type `/phone go` or `/phone @<bubble_id> <follow-up>`.\n"
            )
            out.write(hint)
            out.flush()
        return tokens

    async def poll_owner_actions(self) -> list[OwnerAction]:
        queued = list(self._owner_queue)
        self._owner_queue.clear()
        return queued
