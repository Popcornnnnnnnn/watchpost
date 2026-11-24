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

from watchpost.adapters.dingtalk import DingTalkAdapter
from watchpost.adapters.discord import DiscordAdapter
from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.adapters.slack import SlackAdapter
from watchpost.adapters.telegram import TelegramInboundAdapter, TelegramUpdateHub
from watchpost.adapters.wecom import WeComAdapter
from watchpost.backends.antigravity import AntigravityBackend
from watchpost.backends.cli_agent import CLI_PRESETS, CLIAgentBackend
from watchpost.backends.fallback import FallbackBackend
from watchpost.backends.mock import MockAgentBackend
from watchpost.config import WatchpostConfig, default_config_path, load_config
from watchpost.engine import WatchpostEngine
from watchpost.media import LocalWhisperProcessor
from watchpost.models import EngineConfig
from watchpost.notifiers.bark import BarkNotifier
from watchpost.notifiers.discord import DiscordNotifier
from watchpost.notifiers.external_plugin import ExternalCommandNotifier
from watchpost.notifiers.imessage import IMessageNotifier
from watchpost.notifiers.ntfy import NtfyNotifier
from watchpost.notifiers.pushover import PushoverNotifier
from watchpost.notifiers.slack import SlackNotifier
from watchpost.notifiers.telegram import TelegramNotifier
from watchpost.notifiers.wecom import WeComNotifier
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


_NATIVE_ADAPTER_NAMES = frozenset({"feishu", "slack", "discord", "telegram", "dingtalk", "wecom"})


def _default_webhook_inbox(cfg: WatchpostConfig, name: str) -> Path:
    return Path(cfg.db_path).expanduser().parent / "webhooks" / f"{name}.jsonl"


def _register_configured_platforms(
    engine: WatchpostEngine,
    cfg: WatchpostConfig,
    *,
    existing_tg_hub: TelegramUpdateHub | None = None,
    feishu_client: httpx.AsyncClient | None = None,
    telegram_client: httpx.AsyncClient | None = None,
    imessage_send_fn: Any = None,
    slack_client: httpx.AsyncClient | None = None,
    discord_client: httpx.AsyncClient | None = None,
    dingtalk_client: httpx.AsyncClient | None = None,
    wecom_client: httpx.AsyncClient | None = None,
    ntfy_client: httpx.AsyncClient | None = None,
    bark_client: httpx.AsyncClient | None = None,
    pushover_client: httpx.AsyncClient | None = None,
    register_imessage: bool = True,
) -> TelegramUpdateHub | None:
    """Register enabled native & external adapters/notifiers onto `engine` cleanly."""
    enabled_adapters = {c.adapter for c in cfg.channels if c.enabled}

    if cfg.feishu.enabled or (
        "feishu" in enabled_adapters
        and (
            cfg.feishu.auth_mode == "lark_cli"
            or (cfg.feishu.app_id and cfg.feishu.app_secret)
            or cfg.feishu.user_access_token
        )
    ):
        engine.register_adapter(FeishuAdapter(cfg.feishu, client=feishu_client))

    if (cfg.slack.enabled or "slack" in enabled_adapters) and cfg.slack.bot_token.strip():
        engine.register_adapter(SlackAdapter(cfg.slack, client=slack_client))

    if (cfg.discord.enabled or "discord" in enabled_adapters) and cfg.discord.bot_token.strip():
        engine.register_adapter(DiscordAdapter(cfg.discord, client=discord_client))

    if cfg.dingtalk.enabled or "dingtalk" in enabled_adapters:
        engine.register_adapter(
            DingTalkAdapter(
                cfg.dingtalk,
                inbox_path=Path(cfg.dingtalk.inbox_path).expanduser()
                if cfg.dingtalk.inbox_path
                else _default_webhook_inbox(cfg, "dingtalk"),
                client=dingtalk_client,
            )
        )

    if cfg.wecom.enabled or "wecom" in enabled_adapters:
        engine.register_adapter(
            WeComAdapter(
                cfg.wecom,
                inbox_path=Path(cfg.wecom.inbox_path).expanduser()
                if cfg.wecom.inbox_path
                else _default_webhook_inbox(cfg, "wecom"),
                client=wecom_client,
            )
        )

    for ext_cfg in cfg.external_adapters:
        if ext_cfg.name and ext_cfg.command:
            engine.register_adapter(ExternalCommandAdapter(ext_cfg))

    # Share one TelegramUpdateHub when inbound adapter + owner notifier use the same bot_token.
    tg_base = (cfg.telegram.base_url or "https://api.telegram.org").strip().rstrip("/")
    notify_tok = cfg.telegram.bot_token.strip()
    inbound_tok = (cfg.telegram.inbound_bot_token or cfg.telegram.bot_token).strip()
    tg_notify_on = bool(cfg.telegram.enabled and notify_tok and cfg.telegram.owner_chat_id.strip())
    tg_inbound_on = bool(
        (cfg.telegram.inbound_enabled or "telegram" in enabled_adapters) and inbound_tok
    )
    same_tg_token = bool(tg_notify_on and tg_inbound_on and notify_tok == inbound_tok)

    reused_hub: TelegramUpdateHub | None = None
    if same_tg_token:
        if (
            existing_tg_hub is not None
            and existing_tg_hub.token == inbound_tok
            and existing_tg_hub.base_url == tg_base
        ):
            reused_hub = existing_tg_hub
        else:
            reused_hub = TelegramUpdateHub(
                inbound_tok,
                base_url=tg_base,
                poll_timeout_s=cfg.telegram.poll_timeout_s,
                proxy_url=cfg.telegram.proxy_url,
                store=engine.store,
                client=telegram_client,
            )

    if tg_inbound_on:
        inbound_hub = reused_hub
        if inbound_hub is None and existing_tg_hub is not None:
            if existing_tg_hub.token == inbound_tok and existing_tg_hub.base_url == tg_base:
                inbound_hub = existing_tg_hub
                reused_hub = existing_tg_hub
        engine.register_adapter(
            TelegramInboundAdapter(cfg.telegram, hub=inbound_hub, client=telegram_client)
        )

    if tg_notify_on:
        engine.register_notifier(
            TelegramNotifier(
                cfg.telegram,
                hub=reused_hub if same_tg_token else None,
                store=engine.store,
                media_dir=Path(cfg.media_dir).expanduser(),
                language=cfg.language,
                client=telegram_client,
            )
        )

    if register_imessage and cfg.imessage.enabled and (cfg.imessage.owner_handle or cfg.imessage.allowed_handles):
        engine.register_notifier(IMessageNotifier(cfg.imessage, send_fn=imessage_send_fn))

    if cfg.slack.dm_enabled and cfg.slack.bot_token.strip() and (
        cfg.slack.owner_dm_channel_id.strip() or cfg.slack.owner_user_id.strip()
    ):
        engine.register_notifier(SlackNotifier(cfg.slack, client=slack_client))

    if cfg.discord.dm_enabled and cfg.discord.bot_token.strip() and (
        cfg.discord.owner_dm_channel_id.strip() or cfg.discord.owner_user_id.strip()
    ):
        engine.register_notifier(DiscordNotifier(cfg.discord, client=discord_client))

    if (
        cfg.wecom.notify_enabled
        and cfg.wecom.corp_id.strip()
        and cfg.wecom.corp_secret.strip()
        and int(cfg.wecom.agent_id or 0) > 0
        and cfg.wecom.owner_user_id.strip()
    ):
        engine.register_notifier(WeComNotifier(cfg.wecom, client=wecom_client))

    if cfg.ntfy.enabled and cfg.ntfy.topic.strip():
        engine.register_notifier(NtfyNotifier(cfg.ntfy, client=ntfy_client))

    if cfg.bark.enabled and cfg.bark.device_key.strip():
        engine.register_notifier(BarkNotifier(cfg.bark, client=bark_client))

    if cfg.pushover.enabled and cfg.pushover.app_token.strip() and cfg.pushover.user_key.strip():
        engine.register_notifier(PushoverNotifier(cfg.pushover, client=pushover_client))

    for ext_ntf in cfg.external_notifiers:
        if ext_ntf.name and ext_ntf.command:
            engine.register_notifier(ExternalCommandNotifier(ext_ntf))

    return reused_hub


def _resolve_personal_settings(cfg: WatchpostConfig) -> tuple[Path | None, int, bool]:
    """Resolve personal direct chat settings from top-level config with imessage fallback."""
    raw_ws = (cfg.personal_workspace_dir or cfg.imessage.personal_workspace_dir or "").strip()
    personal_ws = Path(raw_ws).expanduser() if raw_ws else None
    ttl_s = (
        int(cfg.personal_session_ttl_s)
        if int(cfg.personal_session_ttl_s or 0) > 0
        else int(cfg.imessage.personal_session_ttl_s)
    )
    ack = cfg.ack_on_receive if cfg.ack_on_receive is not None else bool(cfg.imessage.ack_on_receive)
    return personal_ws, ttl_s, ack


def build_engine_from_config(
    cfg: WatchpostConfig,
    *,
    feishu_client: httpx.AsyncClient | None = None,
    telegram_client: httpx.AsyncClient | None = None,
    imessage_send_fn: Any = None,
    slack_client: httpx.AsyncClient | None = None,
    discord_client: httpx.AsyncClient | None = None,
    dingtalk_client: httpx.AsyncClient | None = None,
    wecom_client: httpx.AsyncClient | None = None,
    ntfy_client: httpx.AsyncClient | None = None,
    bark_client: httpx.AsyncClient | None = None,
    pushover_client: httpx.AsyncClient | None = None,
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

    personal_ws, personal_ttl_s, ack_on_receive = _resolve_personal_settings(cfg)
    engine = WatchpostEngine(
        store,
        backend,
        config=EngineConfig(
            workspace_dir=Path(cfg.workspace_dir).expanduser(),
            language=cfg.language,
            debounce_s=cfg.debounce_s,
            max_debounce_s=cfg.max_debounce_s,
            personal_workspace_dir=personal_ws,
            personal_session_ttl_s=personal_ttl_s,
            ack_on_receive=ack_on_receive,
            quiet_hours=cfg.quiet_hours,
            cross_channel_window_s=cfg.cross_channel_window_s,
            cross_channel_max_items=cfg.cross_channel_max_items,
        ),
        media_processor=media_proc,
        media_dir=Path(cfg.media_dir).expanduser(),
    )

    _register_configured_platforms(
        engine,
        cfg,
        feishu_client=feishu_client,
        telegram_client=telegram_client,
        imessage_send_fn=imessage_send_fn,
        slack_client=slack_client,
        discord_client=discord_client,
        dingtalk_client=dingtalk_client,
        wecom_client=wecom_client,
        ntfy_client=ntfy_client,
        bark_client=bark_client,
        pushover_client=pushover_client,
    )

    for ch in cfg.channels:
        store.upsert_channel(ch)

    return engine


class WatchpostDaemon:
    """Runs passive channel watching and owner phone-command polling cleanly."""

    def __init__(
        self,
        engine: WatchpostEngine,
        config: WatchpostConfig,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.engine = engine
        self.config = config
        self.config_path = Path(config_path).expanduser() if config_path else None
        self._reload_lock = asyncio.Lock()

    async def reload_from_disk(self) -> dict[str, Any]:
        """Hot-reload channels, personas, quiet hours, and timings from disk without restarting."""
        async with self._reload_lock:
            if self.config_path is None or not self.config_path.is_file():
                raise FileNotFoundError(
                    f"Watchpost config path unavailable for reload: {self.config_path}"
                )
            new_cfg = load_config(self.config_path)
            personal_ws, personal_ttl_s, ack_on_receive = _resolve_personal_settings(new_cfg)
            self.engine.config = EngineConfig(
                workspace_dir=Path(new_cfg.workspace_dir).expanduser(),
                language=new_cfg.language,
                debounce_s=new_cfg.debounce_s,
                max_debounce_s=new_cfg.max_debounce_s,
                media_retention_days=self.engine.config.media_retention_days,
                personal_workspace_dir=personal_ws,
                personal_session_ttl_s=personal_ttl_s,
                ack_on_receive=ack_on_receive,
                quiet_hours=new_cfg.quiet_hours,
                cross_channel_window_s=new_cfg.cross_channel_window_s,
                cross_channel_max_items=new_cfg.cross_channel_max_items,
            )
            self.engine.debouncer.debounce_s = float(new_cfg.debounce_s)
            self.engine.debouncer.max_debounce_s = float(new_cfg.max_debounce_s)

            configured_ids = {ch.id for ch in new_cfg.channels}
            disabled_count = 0
            for existing in self.engine.store.list_channels():
                if existing.adapter == "owner_direct":
                    continue
                if existing.id not in configured_ids and existing.enabled:
                    self.engine.store.update_channel_settings(existing.id, enabled=False)
                    disabled_count += 1
            for ch in new_cfg.channels:
                self.engine.store.upsert_channel(ch)

            old_to_close: list[Any] = []
            existing_tg_hub: TelegramUpdateHub | None = None
            old_tg_adapter = self.engine.adapters.get("telegram")
            if isinstance(old_tg_adapter, TelegramInboundAdapter):
                existing_tg_hub = old_tg_adapter.hub

            for name in list(self.engine.adapters):
                if name == "simulator":
                    continue
                if name in _NATIVE_ADAPTER_NAMES or name not in {"simulator"}:
                    old_to_close.append(self.engine.adapters.pop(name))

            kept_notifiers = []
            has_live_imessage = False
            for ntf in self.engine.notifiers:
                if isinstance(ntf, IMessageNotifier) and new_cfg.imessage.enabled:
                    kept_notifiers.append(ntf)
                    has_live_imessage = True
                    continue
                if existing_tg_hub is None and isinstance(ntf, TelegramNotifier) and ntf.hub is not None:
                    existing_tg_hub = ntf.hub
                old_to_close.append(ntf)
            self.engine.notifiers = kept_notifiers

            reused_hub = _register_configured_platforms(
                self.engine,
                new_cfg,
                existing_tg_hub=existing_tg_hub,
                register_imessage=not has_live_imessage,
            )

            for obj in old_to_close:
                if reused_hub is not None:
                    if isinstance(obj, TelegramInboundAdapter) and obj.hub is reused_hub:
                        obj._owns_hub = False
                    elif isinstance(obj, TelegramNotifier) and obj.hub is reused_hub:
                        obj._hub = None
                aclose = getattr(obj, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:
                        pass

            self.config = new_cfg
            for ch in new_cfg.channels:
                stored = self.engine.store.get_channel(ch.id)
                if (
                    ch.enabled
                    and ch.adapter != "owner_direct"
                    and ch.adapter in self.engine.adapters
                    and stored is not None
                    and stored.cursor_pos == 0
                    and stored.cursor_ms == 0
                    and not self.engine.store.get_recent_messages(ch.id, limit=1)
                ):
                    try:
                        fetched = await self.engine.adapters[ch.adapter].fetch_recent(ch)
                        await self.engine.ingest_messages(
                            ch.id,
                            fetched,
                            seed_if_empty=True,
                            debounced=True,
                        )
                    except Exception as exc:
                        logger.warning("Reload seed failed (%s / %s): %s", ch.adapter, ch.id, exc)

            active_count = sum(
                1 for c in self.engine.store.list_channels() if c.enabled and c.adapter != "owner_direct"
            )
            logger.info("Hot-reloaded Watchpost config (%d active channels, %d disabled)", active_count, disabled_count)
            return {
                "reloaded": True,
                "channels": active_count,
                "disabled": disabled_count,
                "quiet_hours": self.engine.get_quiet_status(),
            }

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
        recovered = self.engine.store.recover_stale_active_threads()
        if recovered:
            logger.warning("Recovered %d orphaned active thread(s) on startup", recovered)
        if seed_on_start:
            await self.engine.poll_all_channels(seed_if_empty=True, debounced=False)
            for ntf in self.engine.notifiers:
                seed_fn = getattr(ntf, "seed_offset", None)
                if callable(seed_fn):
                    try:
                        await seed_fn()
                    except Exception as exc:
                        logger.warning("Notifier %s seed_offset failed: %s", getattr(ntf, "name", "?"), exc)
                reg_fn = getattr(ntf, "register_commands", None)
                if callable(reg_fn):
                    try:
                        await reg_fn()
                    except Exception as exc:
                        logger.warning("Notifier %s register_commands failed: %s", getattr(ntf, "name", "?"), exc)
        logger.info(
            "Watchpost daemon started (backend=%s, channels=%d, api_port=%d)",
            self.engine.backend.name,
            self.engine.store.get_metrics()["channels"],
            int(getattr(self.config, "api_port", 0) or 0),
        )

        api_server: Any = None
        api_task: asyncio.Task[None] | None = None
        if int(getattr(self.config, "api_port", 0) or 0) > 0:
            import uvicorn

            from watchpost.server import create_app

            app = create_app(
                self.engine.store.db_path,
                backend=self.engine.backend,
                config=self.engine.config,
                engine=self.engine,
                reload_fn=self.reload_from_disk,
                config_path=self.config_path,
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

        next_chan_at = 0.0
        next_owner_at = 0.0

        try:
            while not stopper.is_set():
                now = time.monotonic()
                chan_interval = max(1.0, float(self.config.poll_interval_s))
                owner_interval = max(0.5, float(self.config.owner_poll_interval_s))
                if now >= next_chan_at:
                    await self.engine.poll_all_channels(seed_if_empty=False, debounced=True)
                    await self.engine.flush_quiet_digest_if_due()
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

    # External out-of-process notifiers
    for ext_ntf in cfg.external_notifiers:
        cmd0 = ext_ntf.command[0] if ext_ntf.command else ""
        ext_ok = bool(cmd0 and (Path(cmd0).is_file() or shutil.which(cmd0)))
        checks[f"notifier:{ext_ntf.name}"] = {
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

    enabled_adapters = {c.adapter for c in cfg.channels if c.enabled}
    if cfg.telegram.inbound_enabled or "telegram" in enabled_adapters:
        has_tg_in = bool((cfg.telegram.inbound_bot_token or cfg.telegram.bot_token).strip())
        checks["telegram_inbound"] = {
            "ok": has_tg_in,
            "detail": "configured" if has_tg_in else "enabled but missing inbound_bot_token/bot_token",
        }

    if cfg.slack.enabled or "slack" in enabled_adapters:
        has_slack = bool(cfg.slack.bot_token.strip())
        checks["slack"] = {
            "ok": has_slack,
            "detail": "configured" if has_slack else "enabled but missing bot_token",
        }
    if cfg.slack.dm_enabled:
        has_slack_dm = bool(
            cfg.slack.bot_token.strip()
            and (cfg.slack.owner_dm_channel_id.strip() or cfg.slack.owner_user_id.strip())
        )
        checks["slack_dm"] = {
            "ok": has_slack_dm,
            "detail": "configured" if has_slack_dm else "enabled but missing bot_token or owner_dm_channel_id/owner_user_id",
        }

    if cfg.discord.enabled or "discord" in enabled_adapters:
        has_discord = bool(cfg.discord.bot_token.strip())
        checks["discord"] = {
            "ok": has_discord,
            "detail": "configured" if has_discord else "enabled but missing bot_token",
        }
    if cfg.discord.dm_enabled:
        has_discord_dm = bool(
            cfg.discord.bot_token.strip()
            and (cfg.discord.owner_dm_channel_id.strip() or cfg.discord.owner_user_id.strip())
        )
        checks["discord_dm"] = {
            "ok": has_discord_dm,
            "detail": "configured" if has_discord_dm else "enabled but missing bot_token or owner_dm_channel_id/owner_user_id",
        }

    if cfg.dingtalk.enabled or "dingtalk" in enabled_adapters:
        dt_ok = bool(
            cfg.dingtalk.webhook_secret.strip()
            or cfg.dingtalk.app_secret.strip()
            or cfg.dingtalk.allow_unsigned_webhooks
        )
        checks["dingtalk"] = {
            "ok": dt_ok,
            "detail": (
                "webhook receiver ready"
                if dt_ok
                else "enabled but missing webhook_secret/app_secret (or allow_unsigned_webhooks)"
            ),
        }

    if cfg.wecom.enabled or "wecom" in enabled_adapters:
        wc_ok = bool(cfg.wecom.callback_token.strip() or cfg.wecom.allow_unsigned_webhooks)
        checks["wecom"] = {
            "ok": wc_ok,
            "detail": (
                "webhook receiver ready"
                if wc_ok
                else "enabled but missing callback_token (or allow_unsigned_webhooks)"
            ),
        }
    if cfg.wecom.notify_enabled:
        wc_ntf_ok = bool(
            cfg.wecom.corp_id.strip()
            and cfg.wecom.corp_secret.strip()
            and int(cfg.wecom.agent_id or 0) > 0
            and cfg.wecom.owner_user_id.strip()
        )
        checks["wecom_notify"] = {
            "ok": wc_ntf_ok,
            "detail": (
                "configured"
                if wc_ntf_ok
                else "enabled but missing corp_id, corp_secret, agent_id, or owner_user_id"
            ),
        }

    if cfg.ntfy.enabled:
        ntfy_ok = bool(cfg.ntfy.topic.strip())
        checks["ntfy"] = {
            "ok": ntfy_ok,
            "detail": f"configured (topic={cfg.ntfy.topic})" if ntfy_ok else "enabled but missing topic",
        }
    if cfg.bark.enabled:
        bark_ok = bool(cfg.bark.device_key.strip())
        checks["bark"] = {
            "ok": bark_ok,
            "detail": "configured" if bark_ok else "enabled but missing device_key",
        }
    if cfg.pushover.enabled:
        po_ok = bool(cfg.pushover.app_token.strip() and cfg.pushover.user_key.strip())
        checks["pushover"] = {
            "ok": po_ok,
            "detail": "configured" if po_ok else "enabled but missing app_token or user_key",
        }

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
