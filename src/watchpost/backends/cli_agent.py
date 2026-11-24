"""Pluggable subprocess CLI backend supporting Claude Code, Codex, Gemini CLI, and Antigravity."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

from watchpost.bubbles import extract_sections, split_into_bubbles
from watchpost.models import AgentResult, AutonomyMode, ChannelConfig, TriageMode

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_META_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
}
_META_ONLY_KEYS = {"session_id", "thread_id", "conversationId", "conversation_id", "id", "status", "usage"}

CLI_PRESETS: dict[str, dict[str, list[str]]] = {
    "claude_code": {
        "start": ["claude", "-p"],
        "steer": ["claude", "-r", "{ref}", "-p"],
    },
    "codex": {
        "start": ["codex", "exec", "--json", "--skip-git-repo-check"],
        "steer": ["codex", "exec", "resume", "--json", "--skip-git-repo-check", "{ref}"],
    },
    "gemini_cli": {
        "start": ["gemini", "-p"],
        "steer": ["gemini", "-r", "{ref}", "-p"],
    },
    "antigravity": {
        "start": ["agentapi", "new-conversation", "--"],
        "steer": ["agentapi", "send-message", "--", "{ref}"],
    },
}


class CLIAgentBackend:
    """Runs any external agent CLI as an async subprocess using structured presets or custom commands."""

    def __init__(
        self,
        preset: str = "claude_code",
        *,
        start_cmd: list[str] | None = None,
        steer_cmd: list[str] | None = None,
        timeout_s: float = 120.0,
        sandbox_mode: str = "",
    ) -> None:
        if preset not in CLI_PRESETS and (not start_cmd or not steer_cmd):
            raise ValueError(f"Unknown CLI preset: {preset}")
        self.name = preset
        defaults = CLI_PRESETS.get(preset, {"start": [], "steer": []})
        self._start_cmd = list(start_cmd or defaults["start"])
        self._steer_cmd = list(steer_cmd or defaults["steer"])
        self._timeout_s = float(timeout_s)
        self._sandbox_mode = sandbox_mode.strip()

    async def start_thread(
        self,
        title: str,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        channel: ChannelConfig | None = None,
    ) -> tuple[str, AgentResult]:
        del title, model
        cmd = list(self._start_cmd)
        if self.name == "codex" and "--sandbox" not in cmd and "-s" not in cmd:
            if self._sandbox_mode:
                cmd.extend(["--sandbox", self._sandbox_mode])
            elif channel is not None and channel.autonomy_mode in {
                AutonomyMode.assistant_only,
                AutonomyMode.diagnose_only,
            }:
                cmd.extend(["--sandbox", "read-only"])
            elif channel is not None and channel.autonomy_mode == AutonomyMode.auto_fix:
                cmd.extend(["--sandbox", "workspace-write"])
        cmd.append(prompt)
        stdout, stderr, rc = await self._run(cmd, cwd=cwd)
        if rc != 0:
            msg = (stderr or stdout or f"CLI exited {rc}").strip()[-300:]
            return "", AgentResult(raw_text=msg, error=msg)
        ref = self._extract_ref(stdout) or f"{self.name}-{uuid.uuid4().hex[:10]}"
        return ref, self._parse_output(stdout)

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel: ChannelConfig | None = None,
    ) -> AgentResult:
        del title, channel
        cmd = [part.format(ref=backend_thread_ref) for part in self._steer_cmd]
        cmd.append(message)
        stdout, stderr, rc = await self._run(cmd, cwd=cwd)
        if rc != 0:
            msg = (stderr or stdout or f"CLI steer exited {rc}").strip()[-300:]
            return AgentResult(raw_text=msg, error=msg)
        return self._parse_output(stdout)

    async def _run(self, cmd: list[str], *, cwd: Path) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        return (
            out_b.decode("utf-8", errors="replace").strip(),
            err_b.decode("utf-8", errors="replace").strip(),
            int(proc.returncode or 0),
        )

    @staticmethod
    def _extract_ref(stdout: str) -> str:
        decoder = json.JSONDecoder()
        for line in stdout.splitlines():
            idx = line.find("{")
            if idx < 0:
                continue
            try:
                payload, _end = decoder.raw_decode(line[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                for key in ("session_id", "thread_id", "conversationId"):
                    val = payload.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        m = _UUID_RE.search(stdout)
        return m.group(0) if m else ""

    @staticmethod
    def _extract_reply_text(stdout: str) -> str:
        structured_msgs: list[str] = []
        prose_lines: list[str] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                prose_lines.append("")
                continue
            if stripped.startswith("{"):
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    item = payload.get("item")
                    if (
                        payload.get("type") == "item.completed"
                        and isinstance(item, dict)
                        and item.get("type") == "agent_message"
                        and isinstance(item.get("text"), str)
                    ):
                        structured_msgs.append(item["text"])
                        continue
                    res_val = payload.get("result")
                    if isinstance(res_val, str) and res_val.strip():
                        structured_msgs.append(res_val)
                        continue
                    if payload.get("type") in _META_EVENT_TYPES or set(payload).issubset(_META_ONLY_KEYS):
                        continue
            prose_lines.append(line)

        if structured_msgs:
            return structured_msgs[-1].strip()
        prose = "\n".join(prose_lines).strip()
        return prose or stdout.strip()

    @classmethod
    def _parse_output(cls, raw_text: str) -> AgentResult:
        extracted = cls._extract_reply_text(raw_text)
        sec = extract_sections(extracted)
        summary = sec.get("summary") or extracted
        att = sec.get("attention", "").strip()
        if att.lower() in {"none", "n/a", "-", "无", "暂无", "无需处理", "不需要"}:
            att = ""
        mode = TriageMode.mode_4_bug if att else TriageMode.mode_1_qa
        return AgentResult(
            raw_text=extracted,
            summary_bubbles=split_into_bubbles(summary, max_bubbles=3),
            suggested_reply=sec.get("suggested_reply", "").strip(),
            needs_attention=att,
            triage_mode=mode,
        )
