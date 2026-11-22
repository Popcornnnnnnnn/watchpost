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

from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.backends.antigravity import AntigravityBackend
from watchpost.backends.cli_agent import CLI_PRESETS, CLIAgentBackend
from watchpost.backends.fallback import FallbackBackend
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


def _find_antigravity_bin(bin_name: str) -> str | None:
    found = shutil.which(bin_name)
    if found:
        return found
    candidate = Path.home() / ".gemini" / "antigravity" / "bin" / bin_name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _build_single_backend(
    preset: str,
    cfg: WatchpostConfig,
    store: WatchpostStore,
    *,
    timeout_s: float,
    use_custom_cmd: bool,
) -> AgentBackend:
    if preset == "mock":
        return MockAgentBackend(language=cfg.language)
    if preset == "antigravity":
        return AntigravityBackend(
            store,
            agentapi_bin=cfg.backend.antigravity_bin,
            model=cfg.backend.antigravity_model,
            profile=cfg.backend.antigravity_profile,
            timeout_s=timeout_s,
        )
    start_cmd = cfg.backend.start_cmd or None if use_custom_cmd else None
    steer_cmd = cfg.backend.steer_cmd or None if use_custom_cmd else None
    sandbox_mode = cfg.backend.codex_sandbox if preset == "codex" else ""
    return CLIAgentBackend(
        preset,
        start_cmd=start_cmd,
        steer_cmd=steer_cmd,
        timeout_s=timeout_s,
        sandbox_mode=sandbox_mode,
    )


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

    primary = _build_single_backend(
        cfg.backend.preset,
        cfg,
        store,
        timeout_s=cfg.backend.timeout_s,
        use_custom_cmd=True,
    )
    backup_name = cfg.backend.backup_preset.strip()
    if backup_name and backup_name != cfg.backend.preset:
        backup = _build_single_backend(
            backup_name,
            cfg,
            store,
            timeout_s=cfg.backend.backup_timeout_s,
            use_custom_cmd=False,
        )
        backend: AgentBackend = FallbackBackend(primary, backup)
    else:
        backend = primary

    personal_ws = (
        Path(cfg.imessage.personal_workspace_dir).expanduser()
        if cfg.imessage.personal_workspace_dir
        else None
    )
    engine = WatchpostEngine(
        store,
        backend,
        config=EngineConfig(
            workspace_dir=Path(cfg.workspace_dir).expanduser(),
            language=cfg.language,
            debounce_s=cfg.debounce_s,
            max_debounce_s=cfg.max_debounce_s,
            personal_workspace_dir=personal_ws,
            personal_session_ttl_s=cfg.imessage.personal_session_ttl_s,
            ack_on_receive=cfg.imessage.ack_on_receive,
        ),
        media_processor=media_proc,
        media_dir=Path(cfg.media_dir).expanduser(),
    )

    has_feishu_channel = any(c.adapter == "feishu" and c.enabled for c in cfg.channels)
    if cfg.feishu.enabled or (
        has_feishu_channel
        and (
            cfg.feishu.auth_mode == "lark_cli"
            or (cfg.feishu.app_id and cfg.feishu.app_secret)
            or cfg.feishu.user_access_token
        )
    ):
        engine.register_adapter(FeishuAdapter(cfg.feishu, client=feishu_client))

    for ext_cfg in cfg.external_adapters:
        if ext_cfg.name and ext_cfg.command:
            engine.register_adapter(ExternalCommandAdapter(ext_cfg))

    if cfg.telegram.enabled and cfg.telegram.bot_token and cfg.telegram.owner_chat_id:
        engine.register_notifier(TelegramNotifier(cfg.telegram, client=telegram_client))

    if cfg.imessage.enabled and (cfg.imessage.owner_handle or cfg.imessage.allowed_handles):
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

        api_server: Any = None
        api_task: asyncio.Task[None] | None = None
        if int(getattr(self.config, "api_port", 0) or 0) > 0:
            import uvicorn

            from watchpost.server import create_app

            app = create_app(
                self.engine.store.db_path,
                backend=self.engine.backend,
                config=self.engine.config,
            )
            uv_cfg = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=int(self.config.api_port),
                log_level="warning",
                access_log=False,
            )
            api_server = uvicorn.Server(uv_cfg)
            api_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            api_task = asyncio.create_task(api_server.serve())

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
            if api_server is not None:
                api_server.should_exit = True
            if api_task is not None:
                try:
                    await asyncio.wait_for(api_task, timeout=3.0)
                except Exception:
                    api_task.cancel()
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


def _check_backend_binary(
    preset: str,
    cfg: WatchpostConfig,
    *,
    is_primary: bool,
) -> dict[str, Any]:
    if preset == "mock":
        return {"ok": True, "detail": "mock (offline testing)"}
    if preset == "antigravity":
        found = _find_antigravity_bin(cfg.backend.antigravity_bin)
        hooks_file = Path.home() / ".gemini" / "config" / "hooks.json"
        hook_wired = False
        if hooks_file.is_file():
            try:
                hook_wired = "hook-stop" in hooks_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                hook_wired = False
        hook_note = "stop_hook=registered" if hook_wired else "stop_hook=not_yet_wired"
        return {
            "ok": bool(found),
            "detail": f"antigravity ({found or f'{cfg.backend.antigravity_bin} not found'}, {hook_note})",
        }
    cmd0 = (
        cfg.backend.start_cmd[0]
        if (is_primary and cfg.backend.start_cmd)
        else (CLI_PRESETS.get(preset, {}).get("start") or [""])[0]
    )
    found = shutil.which(cmd0) if cmd0 else None
    return {
        "ok": bool(found),
        "detail": f"{preset} ({found or f'{cmd0} not found on PATH'})",
    }


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

    # Agent CLI backend (+ optional backup backend)
    checks["backend"] = _check_backend_binary(cfg.backend.preset, cfg, is_primary=True)
    if cfg.backend.backup_preset.strip() and cfg.backend.backup_preset.strip() != cfg.backend.preset:
        checks["backup_backend"] = _check_backend_binary(cfg.backend.backup_preset.strip(), cfg, is_primary=False)

    # Feishu adapter
    if cfg.feishu.enabled:
        if cfg.feishu.auth_mode == "lark_cli":
            lark_found = shutil.which(cfg.feishu.lark_bin)
            checks["feishu"] = {
                "ok": bool(lark_found),
                "detail": (
                    f"configured (auth_mode=lark_cli, bin={lark_found})"
                    if lark_found
                    else f"enabled with auth_mode=lark_cli but {cfg.feishu.lark_bin!r} not found on PATH"
                ),
            }
        else:
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

    # External out-of-process adapters
    for ext in cfg.external_adapters:
        cmd0 = ext.command[0] if ext.command else ""
        ext_ok = bool(cmd0 and (Path(cmd0).is_file() or shutil.which(cmd0)))
        checks[f"adapter:{ext.name}"] = {
            "ok": ext_ok,
            "detail": f"command={cmd0}" if ext_ok else f"executable not found: {cmd0 or '<empty>'}",
        }

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
        has_handles = bool(cfg.imessage.owner_handle or cfg.imessage.allowed_handles)
        imsg_ok = bool(has_handles and (cfg.imessage.dry_run or (is_mac and chat_db.is_file())))
        detail = (
            f"configured (handles set, chat_db={'found' if chat_db.is_file() else 'missing'})"
            if has_handles
            else "enabled but missing owner_handle or allowed_handles"
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
