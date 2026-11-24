"""Interactive clean-room SandboxSession for simulating inbound chat channels locally."""

from __future__ import annotations

import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Callable, TextIO

from watchpost.adapters.local_sandbox import LocalSandboxAdapter, default_sandbox_channels
from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.backends.mock import MockAgentBackend
from watchpost.engine import WatchpostEngine
from watchpost.models import AutonomyMode, ChannelConfig, ChatType, EngineConfig, TriggerMode
from watchpost.notifiers.console import ConsoleNotifier
from watchpost.ports import AgentBackend
from watchpost.store import WatchpostStore

_PHONE_REPLY_RE = re.compile(r"^@(c-\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
_MENTION_HINT_RE = re.compile(r"(?:^|\s)@(?:me|owner|lead)\b", re.IGNORECASE)


def build_sandbox_backend(backend_name: str, *, language: str = "zh") -> AgentBackend:
    """Create either the offline MockAgentBackend or a real CLI harness backend."""
    if backend_name == "mock":
        return MockAgentBackend(language=language)
    return CLIAgentBackend(backend_name, timeout_s=180.0)


class SandboxSession:
    """Drives Watchpost interactively with simulated external colleagues + owner phone commands."""

    def __init__(
        self,
        sandbox_dir: Path | str,
        *,
        backend: AgentBackend | None = None,
        backend_name: str = "mock",
        workspace_dir: Path | str = ".",
        initial_mode: str = "dm",
        autonomy: AutonomyMode = AutonomyMode.diagnose_only,
        language: str = "zh",
        persona_text: str = "",
        stream: TextIO | None = None,
    ) -> None:
        self.sandbox_dir = Path(sandbox_dir).expanduser()
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.language = language
        self._stream = stream if stream is not None else sys.stdout

        self.store = WatchpostStore(self.sandbox_dir / "sandbox.db")
        self.adapter = LocalSandboxAdapter(self.sandbox_dir)
        self.notifier = ConsoleNotifier(stream=self._stream, language=language)
        chosen_backend = backend or build_sandbox_backend(backend_name, language=language)
        self.engine = WatchpostEngine(
            self.store,
            chosen_backend,
            config=EngineConfig(
                workspace_dir=self.workspace_dir,
                language=language,
                debounce_s=0.0,
                max_debounce_s=0.0,
            ),
            adapters={"sandbox": self.adapter},
            notifiers=[self.notifier],
            media_dir=self.sandbox_dir / "media",
        )

        self._channels: dict[str, str] = {"dm": "sandbox_dm", "group": "sandbox_group"}
        for ch in default_sandbox_channels(autonomy=autonomy, persona_text=persona_text):
            if self.store.get_channel(ch.id) is None:
                self.store.upsert_channel(ch)

        self.current_mode = "group" if initial_mode == "group" else "dm"
        self.sender_name = "Alex"

    @property
    def active_channel(self) -> ChannelConfig:
        cid = self._channels[self.current_mode]
        ch = self.store.get_channel(cid)
        assert ch is not None
        return ch

    def _write(self, line: str) -> None:
        self._stream.write(f"{line}\n")
        self._stream.flush()

    def print_banner(self) -> None:
        ch = self.active_channel
        self._write(
            f"\nWatchpost Interactive Sandbox (backend={self.engine.backend.name}, cwd={self.workspace_dir})"
        )
        self._write(f"Isolated state: {self.sandbox_dir} (live Watchpost/sidecars untouched)")
        self._write(
            f"Active channel: [{self.current_mode}] {ch.name} | autonomy={ch.autonomy_mode.value} | sender={self.sender_name}"
        )
        self.print_help()

    def print_help(self) -> None:
        self._write(
            "Commands: type any message to send as external colleague | /as <name> | /mode dm|group | "
            "/autonomy assistant_only|diagnose_only|auto_fix | /at <text> | /reply <sb_id> <text> | "
            "/img <path> [caption] | /recall <sb_id> | /phone <go|status|list|steer|@c-N reply> | /history | /quit"
        )

    async def handle_line(self, raw_line: str) -> bool:
        """Process one user input line. Returns False only when the user exits."""
        line = raw_line.strip()
        if not line:
            await self.engine.poll_channel(self.active_channel.id, debounced=False)
            return True

        if line in {"/quit", "/exit"}:
            return False

        if line == "/help":
            self.print_help()
            return True

        if line.startswith("/as "):
            name = line[4:].strip()
            if name:
                self.sender_name = name
                self._write(f"[Sandbox] Colleague sender set to: {self.sender_name}")
            else:
                self._write("[Sandbox] Usage: /as <name>")
            return True

        if line.startswith("/mode"):
            target = line[5:].strip().lower()
            if target in self._channels:
                self.current_mode = target
                ch = self.active_channel
                self._write(
                    f"[Sandbox] Switched to [{self.current_mode}] {ch.name} "
                    f"(trigger={ch.trigger_mode.value}, autonomy={ch.autonomy_mode.value})"
                )
            else:
                self._write("[Sandbox] Usage: /mode dm|group")
            return True

        if line.startswith("/autonomy"):
            raw_mode = line[9:].strip().lower()
            try:
                mode = AutonomyMode(raw_mode)
            except ValueError:
                self._write("[Sandbox] Usage: /autonomy assistant_only|diagnose_only|auto_fix")
                return True
            updated = self.store.update_channel_settings(self.active_channel.id, autonomy_mode=mode)
            if updated:
                self._write(f"[Sandbox] {updated.name} autonomy updated to: {updated.autonomy_mode.value}")
            return True

        if line == "/history":
            msgs = self.adapter.list_messages(self.active_channel.external_chat_id, limit=25)
            if not msgs:
                self._write("[Sandbox] No messages in this channel yet.")
                return True
            for m in msgs:
                tag = " [RECALLED]" if m.deleted else (" [@owner]" if m.mentions_owner else "")
                reply_tag = f" (reply->{m.reply_to_id})" if m.reply_to_id else ""
                media_tag = f" [media:{len(m.media_refs)}]" if m.media_refs else ""
                self._write(f"  {m.message_id} [{m.create_time}] {m.sender_name}: {m.clean_text}{reply_tag}{media_tag}{tag}")
            return True

        if line.startswith("/recall "):
            target_id = line[8:].strip()
            if not target_id:
                self._write("[Sandbox] Usage: /recall <sb_id>")
                return True
            msg = self.adapter.recall_message(
                self.active_channel.external_chat_id,
                target_id,
                sender_name=self.sender_name,
            )
            await self.engine.poll_channel(self.active_channel.id, debounced=False)
            self._write(f"[Watchpost] Ignored recalled message {msg.message_id} (targeting {target_id}).")
            return True

        if line.startswith("/phone"):
            cmd = line[6:].strip()
            if not cmd:
                self._write("[Sandbox] Usage: /phone <go|status|list|steer text|@c-N text>")
                return True
            reply_token = ""
            m = _PHONE_REPLY_RE.match(cmd)
            if m:
                reply_token, cmd = m.group(1), m.group(2).strip()
            self.notifier.enqueue_owner_text(cmd, reply_token=reply_token)
            await self.engine.poll_owner_actions_once()
            return True

        if line.startswith("/at"):
            text = line[3:].strip()
            if not text:
                self._write("[Sandbox] Usage: /at <message>")
                return True
            msg_text = text if "@" in text else f"@Owner {text}"
            await self._send_and_poll(text=msg_text, mentions_owner=True)
            return True

        if line.startswith("/reply "):
            parts = line[7:].strip().split(maxsplit=1)
            if len(parts) < 2:
                self._write("[Sandbox] Usage: /reply <sb_id> <message>")
                return True
            reply_id, body = parts[0], parts[1]
            mentions_owner = bool(_MENTION_HINT_RE.search(body))
            await self._send_and_poll(text=body, reply_to_id=reply_id, mentions_owner=mentions_owner)
            return True

        if line.startswith("/img "):
            raw_args = line[5:].strip()
            try:
                tokens = shlex.split(raw_args)
            except ValueError:
                tokens = raw_args.split(maxsplit=1)
            if not tokens:
                self._write("[Sandbox] Usage: /img <image_path> [caption]")
                return True
            img_path = Path(tokens[0]).expanduser()
            caption = " ".join(tokens[1:]).strip()
            if not img_path.is_file():
                self._write(f"[Sandbox] Image file not found: {img_path}")
                return True
            mentions_owner = bool(_MENTION_HINT_RE.search(caption))
            await self._send_and_poll(text=caption, image_path=img_path, mentions_owner=mentions_owner)
            return True

        mentions_owner = bool(_MENTION_HINT_RE.search(line))
        await self._send_and_poll(text=line, mentions_owner=mentions_owner)
        return True

    async def _send_and_poll(
        self,
        *,
        text: str,
        reply_to_id: str = "",
        mentions_owner: bool = False,
        image_path: Path | None = None,
    ) -> None:
        ch = self.active_channel
        msg = self.adapter.append_message(
            ch.external_chat_id,
            sender_name=self.sender_name,
            text=text,
            reply_to_id=reply_to_id,
            mentions_owner=mentions_owner,
            image_path=image_path,
        )
        at_tag = " [@owner]" if msg.mentions_owner else ""
        reply_tag = f" (reply->{msg.reply_to_id})" if msg.reply_to_id else ""
        self._write(f"\n[{ch.name}] {msg.sender_name} ({msg.message_id}){reply_tag}{at_tag}: {msg.clean_text}")
        created = await self.engine.poll_channel(ch.id, debounced=False)
        if not created:
            if ch.chat_type == ChatType.group and ch.trigger_mode == TriggerMode.mention_owner and not msg.mentions_owner:
                self._write("[Watchpost] Saved to group context (not triggered — no @owner/@me mention).")
            else:
                self._write("[Watchpost] Ignored (filtered as small talk / acknowledgement).")


async def run_interactive_sandbox(
    *,
    sandbox_dir: Path | str | None = None,
    backend_name: str = "mock",
    workspace_dir: Path | str = ".",
    initial_mode: str = "dm",
    autonomy: AutonomyMode = AutonomyMode.diagnose_only,
    language: str = "zh",
    persona_text: str = "",
    input_fn: Callable[[str], str] | None = None,
    stream: TextIO | None = None,
) -> int:
    """Run an interactive sandbox REPL loop cleanly in a temp or persistent directory."""
    reader = input_fn or input
    if sandbox_dir:
        return await _run_loop(
            Path(sandbox_dir),
            backend_name=backend_name,
            workspace_dir=workspace_dir,
            initial_mode=initial_mode,
            autonomy=autonomy,
            language=language,
            persona_text=persona_text,
            reader=reader,
            stream=stream,
        )
    with tempfile.TemporaryDirectory(prefix="watchpost-sandbox-") as tmp:
        return await _run_loop(
            Path(tmp),
            backend_name=backend_name,
            workspace_dir=workspace_dir,
            initial_mode=initial_mode,
            autonomy=autonomy,
            language=language,
            persona_text=persona_text,
            reader=reader,
            stream=stream,
        )


async def _run_loop(
    root: Path,
    *,
    backend_name: str,
    workspace_dir: Path | str,
    initial_mode: str,
    autonomy: AutonomyMode,
    language: str,
    persona_text: str,
    reader: Callable[[str], str],
    stream: TextIO | None,
) -> int:
    session = SandboxSession(
        root,
        backend_name=backend_name,
        workspace_dir=workspace_dir,
        initial_mode=initial_mode,
        autonomy=autonomy,
        language=language,
        persona_text=persona_text,
        stream=stream,
    )
    session.print_banner()
    while True:
        ch = session.active_channel
        prompt = f"[{session.current_mode}:{session.sender_name}]> "
        try:
            if not sys.stdin.isatty() and stream is None:
                raw = sys.stdin.readline()
                if raw == "":
                    break
            else:
                raw = reader(prompt)
        except EOFError:
            break
        keep_going = await session.handle_line(raw)
        if not keep_going:
            break
        del ch
    session._write("[Sandbox] Exited cleanly.")
    return 0
