"""Security helpers: secret redaction, atomic file writes, API bearer tokens, and process-tree kills."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import signal
from pathlib import Path

_TG_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_BEARER_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SLACK_TOKEN_RE = re.compile(r"xox[baprs]-[A-Za-z0-9-]+")
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def redact_secrets(text: str) -> str:
    """Redact bot tokens, bearer tokens, and Slack tokens from logs and error messages."""
    raw = str(text or "")
    if not raw:
        return ""
    masked = _TG_BOT_TOKEN_RE.sub("bot<redacted>", raw)
    masked = _BEARER_TOKEN_RE.sub("Bearer <redacted>", masked)
    masked = _SLACK_TOKEN_RE.sub("xox-<redacted>", masked)
    return masked


class RedactingLogFilter(logging.Filter):
    """Logging filter scrubbing secret tokens from formatted messages and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_secrets(v) if isinstance(v, str) else v for v in record.args)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        return True


def atomic_write_text(path: Path | str, content: str, *, mode: int = 0o600) -> Path:
    """Atomically write text to a file using an exclusive restrictive temp file."""
    target = Path(path).expanduser()
    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass

    tmp_path = parent / f".{target.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        data = content.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.close(fd)
    if os.name == "posix":
        os.chmod(tmp_path, mode)
    os.replace(tmp_path, target)
    if os.name == "posix":
        os.chmod(target, mode)
    return target


def api_token_path(db_path: Path | str) -> Path:
    """Return the per-installation local control API bearer token path."""
    return Path(db_path).expanduser().parent / "api.token"


def load_or_create_api_token(db_path: Path | str) -> str:
    """Read an existing non-empty API bearer token or generate a new 0600 token next to the DB."""
    tok_path = api_token_path(db_path)
    if tok_path.is_file():
        existing = tok_path.read_text(encoding="utf-8", errors="replace").strip()
        if existing:
            if os.name == "posix":
                try:
                    os.chmod(tok_path, 0o600)
                except OSError:
                    pass
            return existing
    token = secrets.token_urlsafe(32)
    atomic_write_text(tok_path, token + "\n", mode=0o600)
    return token


def is_allowed_local_host(host_header: str) -> bool:
    """Return True if the HTTP Host header names loopback only (blocks DNS rebinding)."""
    raw = (host_header or "").strip().lower()
    if not raw:
        return True
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return False
        host = raw[: end + 1]
    else:
        host = raw.split(":", 1)[0]
    return host in _ALLOWED_LOCAL_HOSTS


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a timed-out or cancelled subprocess and its process group cleanly without leaving orphans."""
    if os.name == "posix" and getattr(proc, "pid", None):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
    elif proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    if proc.returncode is None:
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=1.0)
        except BaseException:
            pass
