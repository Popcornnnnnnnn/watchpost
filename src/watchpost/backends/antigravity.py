"""Async Antigravity Desktop backend driven by `agentapi` + `watchpost hook-stop`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.models import AgentResult, ChannelConfig
from watchpost.store import WatchpostStore

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def extract_latest_assistant_response(transcript_path: Path) -> str:
    """Scan a JSONL transcript backwards and return the latest assistant response string."""
    path = Path(transcript_path)
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        ev_type = str(ev.get("type") or "")
        role = str(ev.get("role") or (ev.get("message") or {}).get("role") or "")
        if ev_type not in {"PLANNER_RESPONSE", "assistant", "model"} and role not in {"assistant", "model"}:
            continue
        content = ev.get("content") or ev.get("text") or (ev.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def handle_stop_hook_payload(payload: dict[str, Any], *, store: WatchpostStore) -> bool:
    """Record a Stop hook completion in WatchpostStore when `conversationId` is tracked."""
    conv_id = str(payload.get("conversationId") or "").strip()
    if not conv_id:
        return False
    if payload.get("fullyIdle") is False:
        return False
    if not store.is_tracked_backend_ref("antigravity", conv_id):
        return False

    error = str(payload.get("error") or "").strip()
    transcript_raw = str(payload.get("transcriptPath") or "").strip()
    latest_text = extract_latest_assistant_response(Path(transcript_raw)) if transcript_raw else ""
    if not latest_text and not error:
        return False

    digest = hashlib.sha256(f"{latest_text}\n--ERR--\n{error}".encode("utf-8")).hexdigest()
    inserted_id = store.record_backend_completion(
        "antigravity",
        conv_id,
        raw_text=latest_text,
        error=error,
        digest=digest,
    )
    return inserted_id > 0


def _extract_conversation_id(stdout: str, stderr: str = "") -> str:
    for text in (stdout, stderr):
        if not text:
            continue
        idx = text.find("{")
        if idx >= 0:
            try:
                data = json.loads(text[idx:])
                cid = ((data.get("response") or {}).get("newConversation") or {}).get("conversationId")
                if isinstance(cid, str) and cid.strip():
                    return cid.strip()
            except Exception:
                pass
        m = _UUID_RE.search(text)
        if m:
            return m.group(0)
    return ""


class AntigravityBackend:
    """Async AgentBackend dispatching turns via `agentapi` and awaiting `Stop` hook completion."""

    name = "antigravity"

    def __init__(
        self,
        store: WatchpostStore,
        *,
        agentapi_bin: str = "agentapi",
        model: str = "",
        profile: str = "",
        timeout_s: float = 480.0,
        spawn_timeout_s: float = 45.0,
        poll_interval_s: float = 0.4,
    ) -> None:
        self.store = store
        self.agentapi_bin = agentapi_bin.strip() or "agentapi"
        self.model = model.strip()
        self.profile = profile.strip()
        self.timeout_s = max(1.0, float(timeout_s))
        self.spawn_timeout_s = max(1.0, float(spawn_timeout_s))
        self.poll_interval_s = max(0.05, float(poll_interval_s))

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        extra_dir = str(Path.home() / ".gemini" / "antigravity" / "bin")
        cur_path = env.get("PATH", "")
        if extra_dir not in cur_path:
            env["PATH"] = f"{extra_dir}:{cur_path}" if cur_path else extra_dir
        return env

    async def _run_cli(self, args: list[str], *, cwd: Path) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            self.agentapi_bin,
            *args,
            cwd=str(cwd),
            env=self._build_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self.spawn_timeout_s)
        return (
            out_b.decode("utf-8", errors="replace").strip(),
            err_b.decode("utf-8", errors="replace").strip(),
            int(proc.returncode or 0),
        )

    async def _await_completion(self, conv_id: str, *, after_id: int) -> AgentResult:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            comp = self.store.get_new_completion(self.name, conv_id, after_id=after_id)
            if comp is not None:
                if comp["error"]:
                    return AgentResult(raw_text=comp["raw_text"] or comp["error"], error=comp["error"])
                return CLIAgentBackend._parse_output(comp["raw_text"])
            await asyncio.sleep(self.poll_interval_s)
        return AgentResult(raw_text="", error=f"Antigravity turn timed out after {int(self.timeout_s)}s")

    async def start_thread(
        self,
        title: str,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        channel: ChannelConfig | None = None,
    ) -> tuple[str, AgentResult]:
        del channel
        eff_model = model.strip() or self.model
        args = ["new-conversation"]
        if eff_model:
            args.append(f"--model={eff_model}")
        if self.profile:
            args.append(f"--profile={self.profile}")
        args.extend([f"--title={title}", "--", prompt])

        try:
            stdout, stderr, rc = await self._run_cli(args, cwd=cwd)
        except Exception as exc:
            return "", AgentResult(raw_text=str(exc), error=f"agentapi failed: {exc}")

        if rc != 0:
            msg = (stderr or stdout or f"agentapi exited {rc}").strip()[-300:]
            return "", AgentResult(raw_text=msg, error=msg)

        conv_id = _extract_conversation_id(stdout, stderr)
        if not conv_id:
            return "", AgentResult(raw_text=stdout, error="agentapi returned no conversationId")

        self.store.register_backend_ref(self.name, conv_id)
        return conv_id, await self._await_completion(conv_id, after_id=0)

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel: ChannelConfig | None = None,
    ) -> AgentResult:
        del channel
        conv_id = backend_thread_ref.strip()
        if not conv_id:
            return AgentResult(raw_text="", error="Missing Antigravity conversationId")
        self.store.register_backend_ref(self.name, conv_id)
        after_id = self.store.get_latest_completion_id(self.name, conv_id)
        args = ["send-message", f"--title={title}", "--", conv_id, message]
        try:
            stdout, stderr, rc = await self._run_cli(args, cwd=cwd)
        except Exception as exc:
            return AgentResult(raw_text=str(exc), error=f"agentapi steer failed: {exc}")
        if rc != 0:
            msg = (stderr or stdout or f"agentapi send-message exited {rc}").strip()[-300:]
            return AgentResult(raw_text=msg, error=msg)
        return await self._await_completion(conv_id, after_id=after_id)
