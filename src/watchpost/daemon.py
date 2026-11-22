"""Continuous headless Watchpost daemon, config builder, and health doctor."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from watchpost.adapters.feishu import FeishuAdapter
from watchpost.backends.cli_agent import CLI_PRESETS, CLIAgentBackend
from watchpost.backends.mock import MockAgentBackend
from watchpost.config import WatchpostConfig, default_config_path, load_config
from watchpost.engine import WatchpostEngine
from watchpost.media import LocalWhisperProcessor
from watchpost.models import EngineConfig
from watchpost.notifiers.imessage import IMessageNotifier
from watchpost.notifiers.telegram import TelegramNotifier
from watchpost.ports import AgentBackend, MediaProcessor
from watchpost.store import WatchpostStore

logger = logging.getLogger("watchpost.daemon")


def build_engine_from_config(
    cfg: WatchpostConfig,
    *,
    feishu_client: httpx.AsyncClient | None = None,
    telegram_client: httpx.AsyncClient | None = None,
    imessage_send_fn: Any = None,
) -> WatchpostEngine:
    """Construct a fully wired WatchpostEngine from a resolved WatchpostConfig."""
    store = WatchpostStore(Path(cfg.db_path).expanduser())
    media_proc: MediaProcessor | None = None
    if cfg.whisper.enabled:
        media_proc = LocalWhisperProcessor(
            model_path=Path(cfg.whisper.model_path).expanduser() if cfg.whisper.model_path else None,
            whisper_bin=cfg.whisper.whisper_bin,
            ffmpeg_bin=cfg.whisper.ffmpeg_bin,
        )

    backend: AgentBackend
    if cfg.backend.preset == "mock":
        backend = MockAgentBackend(language=cfg.language)
    else:
        backend = CLIAgentBackend(
            cfg.backend.preset,
            start_cmd=cfg.backend.start_cmd or None,
            steer_cmd=cfg.backend.steer_cmd or None,
            timeout_s=cfg.backend.timeout_s,
        )

    engine = WatchpostEngine(
        store,
        backend,
        config=EngineConfig(
            workspace_dir=Path(cfg.workspace_dir).expanduser(),
            language=cfg.language,
            debounce_s=cfg.debounce_s,
            max_debounce_s=cfg.max_debounce_s,
        ),
        media_processor=media_proc,
        media_dir=Path(cfg.media_dir).expanduser(),
    )

    has_feishu_channel = any(c.adapter == "feishu" and c.enabled for c in cfg.channels)
    if cfg.feishu.enabled or (
        has_feishu_channel
        and ((cfg.feishu.app_id and cfg.feishu.app_secret) or cfg.feishu.user_access_token)
    ):
        engine.register_adapter(FeishuAdapter(cfg.feishu, client=feishu_client))

    if cfg.telegram.enabled and cfg.telegram.bot_token and cfg.telegram.owner_chat_id:
        engine.register_notifier(TelegramNotifier(cfg.telegram, client=telegram_client))

    if cfg.imessage.enabled and cfg.imessage.owner_handle:
        engine.register_notifier(IMessageNotifier(cfg.imessage, send_fn=imessage_send_fn))

    for ch in cfg.channels:
        store.upsert_channel(ch)

    return engine


class WatchpostDaemon:
    """Runs passive channel watching and owner phone-command polling cleanly."""

    def __init__(self, engine: WatchpostEngine, config: WatchpostConfig) -> None:
        self.engine = engine
        self.config = config

    async def run_once(self, *, seed_if_empty: bool = False, debounced: bool = False) -> dict[str, int]:
        polled = await self.engine.poll_all_channels(seed_if_empty=seed_if_empty, debounced=debounced)
        flushed = await self.engine.flush_all() if debounced else 0
        actions = await self.engine.poll_owner_actions_once()
        return {
            "channels_polled": polled,
            "batches_flushed": flushed,
            "owner_actions": actions,
        }

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        seed_on_start: bool = True,
    ) -> None:
        stopper = stop_event or asyncio.Event()
        if seed_on_start:
            await self.engine.poll_all_channels(seed_if_empty=True, debounced=False)

        chan_interval = max(1.0, float(self.config.poll_interval_s))
        owner_interval = max(0.5, float(self.config.owner_poll_interval_s))
        next_chan_at = 0.0
        next_owner_at = 0.0

        try:
            while not stopper.is_set():
                now = time.monotonic()
                if now >= next_chan_at:
                    await self.engine.poll_all_channels(seed_if_empty=False, debounced=True)
                    next_chan_at = now + chan_interval
                if now >= next_owner_at:
                    await self.engine.poll_owner_actions_once()
                    next_owner_at = now + owner_interval

                wake_in = max(0.1, min(next_chan_at - time.monotonic(), next_owner_at - time.monotonic(), 1.0))
                try:
                    await asyncio.wait_for(stopper.wait(), timeout=wake_in)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.engine.flush_all()
            await self.close_resources()

    async def close_resources(self) -> None:
        for obj in [*self.engine.adapters.values(), *self.engine.notifiers]:
            aclose = getattr(obj, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass


def run_doctor(cfg_path: Path | str | None = None) -> dict[str, Any]:
    """Inspect config, dependencies, harnesses, and adapters without leaking secrets."""
    resolved_path = Path(cfg_path).expanduser() if cfg_path else default_config_path()
    checks: dict[str, dict[str, Any]] = {}
    if not resolved_path.is_file():
        return {
            "ok": False,
            "config_path": str(resolved_path),
            "checks": {
                "config": {"ok": False, "detail": f"Missing config file: {resolved_path} (run watchpost init-config)"}
            },
            "channels": [],
        }

    try:
        cfg = load_config(resolved_path)
    except Exception as exc:
        return {
            "ok": False,
            "config_path": str(resolved_path),
            "checks": {"config": {"ok": False, "detail": f"Invalid config: {exc}"}},
            "channels": [],
        }
    checks["config"] = {"ok": True, "detail": str(resolved_path)}

    # Storage & workspace
    db_parent = Path(cfg.db_path).expanduser().parent
    try:
        db_parent.mkdir(parents=True, exist_ok=True)
        db_writable = os.access(db_parent, os.W_OK)
    except OSError:
        db_writable = False
    checks["storage"] = {"ok": db_writable, "detail": str(Path(cfg.db_path).expanduser())}

    ws = Path(cfg.workspace_dir).expanduser()
    checks["workspace"] = {"ok": ws.is_dir(), "detail": str(ws)}

    # Agent CLI backend
    if cfg.backend.preset == "mock":
        checks["backend"] = {"ok": True, "detail": "mock (offline testing)"}
    else:
        cmd0 = (
            cfg.backend.start_cmd[0]
            if cfg.backend.start_cmd
            else (CLI_PRESETS.get(cfg.backend.preset, {}).get("start") or [""])[0]
        )
        found = shutil.which(cmd0) if cmd0 else None
        checks["backend"] = {
            "ok": bool(found),
            "detail": f"{cfg.backend.preset} ({found or f'{cmd0} not found on PATH'})",
        }

    # Feishu adapter
    if cfg.feishu.enabled:
        has_auth = bool(
            (cfg.feishu.auth_mode == "user" and cfg.feishu.user_access_token)
            or (cfg.feishu.app_id and cfg.feishu.app_secret)
            or cfg.feishu.user_access_token
        )
        checks["feishu"] = {
            "ok": has_auth,
            "detail": (
                f"configured (auth_mode={cfg.feishu.auth_mode}, owner_open_id={'set' if cfg.feishu.owner_open_id else 'unset'})"
                if has_auth
                else "enabled but missing app_id/app_secret or user_access_token"
            ),
        }
    else:
        checks["feishu"] = {"ok": True, "detail": "disabled"}

    # Phone entrances
    if cfg.telegram.enabled:
        tg_ok = bool(cfg.telegram.bot_token and cfg.telegram.owner_chat_id)
        checks["telegram"] = {
            "ok": tg_ok,
            "detail": "configured" if tg_ok else "enabled but missing bot_token or owner_chat_id",
        }
    else:
        checks["telegram"] = {"ok": True, "detail": "disabled"}

    if cfg.imessage.enabled:
        chat_db = Path(cfg.imessage.chat_db_path).expanduser()
        is_mac = sys.platform == "darwin"
        imsg_ok = bool(cfg.imessage.owner_handle and (cfg.imessage.dry_run or (is_mac and chat_db.is_file())))
        detail = (
            f"configured (handle set, chat_db={'found' if chat_db.is_file() else 'missing'})"
            if cfg.imessage.owner_handle
            else "enabled but missing owner_handle"
        )
        checks["imessage"] = {"ok": imsg_ok, "detail": detail}
    else:
        checks["imessage"] = {"ok": True, "detail": "disabled"}

    # Whisper (optional)
    if cfg.whisper.enabled:
        ff_ok = bool(shutil.which(cfg.whisper.ffmpeg_bin))
        wh_ok = bool(shutil.which(cfg.whisper.whisper_bin))
        model_ok = bool(cfg.whisper.model_path and Path(cfg.whisper.model_path).expanduser().is_file())
        checks["whisper"] = {
            "ok": ff_ok and wh_ok and model_ok,
            "detail": f"ffmpeg={ff_ok}, whisper={wh_ok}, model={model_ok}",
        }
    else:
        checks["whisper"] = {"ok": True, "detail": "disabled"}

    overall_ok = checks["config"]["ok"] and checks["storage"]["ok"] and all(v["ok"] for v in checks.values())
    return {
        "ok": overall_ok,
        "config_path": str(resolved_path),
        "checks": checks,
        "channels": [{"id": c.id, "adapter": c.adapter, "name": c.name, "enabled": c.enabled} for c in cfg.channels],
    }
