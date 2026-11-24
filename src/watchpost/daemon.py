"""Continuous headless Watchpost daemon, config builder, and health doctor."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any

import httpx

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

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
from watchpost.config import (
    WatchpostConfig,
    default_config_path,
    find_unresolved_placeholders,
    load_config,
    load_raw_config,
)
from watchpost.engine import WatchpostEngine
from watchpost.media import LocalWhisperProcessor, prune_old_media
from watchpost.models import EngineConfig
from watchpost.notifiers.bark import BarkNotifier
from watchpost.notifiers.discord import DiscordNotifier
from watchpost.notifiers.external_plugin import ExternalCommandNotifier
from watchpost.notifiers.imessage import IMessageNotifier
from watchpost.notifiers.ntfy import NtfyNotifier
from watchpost.notifiers.pushover import PushoverNotifier
from watchpost.notifiers.slack import SlackNotifier
from watchpost.notifiers.telegram import TelegramNotifier, get_bot_info, get_webhook_info
from watchpost.notifiers.wecom import WeComNotifier
from watchpost.ports import AgentBackend, MediaProcessor
from watchpost.security import redact_secrets
from watchpost.store import WatchpostStore

logger = logging.getLogger("watchpost.daemon")


def _antigravity_home() -> Path:
    override = os.environ.get("ANTIGRAVITY_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "antigravity"


def _antigravity_hooks_file() -> Path:
    override = os.environ.get("WATCHPOST_ANTIGRAVITY_HOOKS", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "config" / "hooks.json"


def _find_antigravity_bin(bin_name: str) -> str | None:
    found = shutil.which(bin_name)
    if found:
        return found
    candidate = _antigravity_home() / "bin" / bin_name
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

            kept_set = set(kept_notifiers)
            reused_hub = _register_configured_platforms(
                self.engine,
                new_cfg,
                existing_tg_hub=existing_tg_hub,
                register_imessage=not has_live_imessage,
            )
            if reused_hub is not None:
                set_owners = getattr(reused_hub, "set_owner_chat_ids", None)
                if callable(set_owners):
                    owner_ids = (
                        {new_cfg.telegram.owner_chat_id.strip()}
                        if (new_cfg.telegram.enabled and new_cfg.telegram.owner_chat_id.strip())
                        else set()
                    )
                    set_owners(owner_ids)

            for ntf in self.engine.notifiers:
                if ntf in kept_set:
                    continue
                seed_fn = getattr(ntf, "seed_offset", None)
                if callable(seed_fn):
                    try:
                        await seed_fn()
                    except Exception as exc:
                        logger.warning(
                            "Notifier %s seed_offset failed on reload: %s",
                            getattr(ntf, "name", "?"),
                            redact_secrets(str(exc)),
                        )
                reg_fn = getattr(ntf, "register_commands", None)
                if callable(reg_fn):
                    try:
                        await reg_fn()
                    except Exception as exc:
                        logger.warning(
                            "Notifier %s register_commands failed on reload: %s",
                            getattr(ntf, "name", "?"),
                            redact_secrets(str(exc)),
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
                        logger.warning("Reload seed failed (%s / %s): %s", ch.adapter, ch.id, redact_secrets(str(exc)))

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

    @contextlib.contextmanager
    def _instance_lock(self):
        lock_path = self.engine.store.db_path.parent / "watchpost.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+", encoding="utf-8")
        acquired = False
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    fh.seek(0)
                    other_pid = fh.read().strip() or "unknown"
                    raise RuntimeError(
                        f"Another Watchpost daemon is already running (PID {other_pid}, lock {lock_path})"
                    ) from exc
            acquired = True
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield lock_path
        finally:
            if acquired:
                try:
                    fh.seek(0)
                    fh.truncate()
                    fh.flush()
                except OSError:
                    pass
                if fcntl is not None:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
            fh.close()

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        seed_on_start: bool = True,
    ) -> None:
        with self._instance_lock():
            stopper = stop_event or asyncio.Event()
            recovered = self.engine.store.recover_stale_active_threads()
            if recovered:
                logger.warning("Recovered %d orphaned active thread(s) on startup", recovered)

            pruned_init = prune_old_media(
                self.engine.media_dir,
                max_age_days=int(getattr(self.engine.config, "media_retention_days", 14) or 14),
            )
            if pruned_init:
                logger.info("Pruned %d expired media artifact(s) on startup", pruned_init)

            if seed_on_start:
                await self.engine.poll_all_channels(seed_if_empty=True, debounced=False)
                for ntf in self.engine.notifiers:
                    seed_fn = getattr(ntf, "seed_offset", None)
                    if callable(seed_fn):
                        try:
                            await seed_fn()
                        except Exception as exc:
                            logger.warning(
                                "Notifier %s seed_offset failed: %s",
                                getattr(ntf, "name", "?"),
                                redact_secrets(str(exc)),
                            )
                    reg_fn = getattr(ntf, "register_commands", None)
                    if callable(reg_fn):
                        try:
                            await reg_fn()
                        except Exception as exc:
                            logger.warning(
                                "Notifier %s register_commands failed: %s",
                                getattr(ntf, "name", "?"),
                                redact_secrets(str(exc)),
                            )

            logger.info(
                "Watchpost daemon started (backend=%s, channels=%d, api_port=%d, webhook_port=%d)",
                self.engine.backend.name,
                self.engine.store.get_metrics()["channels"],
                int(getattr(self.config, "api_port", 0) or 0),
                int(getattr(self.config, "webhook_port", 0) or 0),
            )

            api_server: Any = None
            api_task: asyncio.Task[None] | None = None
            webhook_server: Any = None
            webhook_task: asyncio.Task[None] | None = None
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

            if int(getattr(self.config, "webhook_port", 0) or 0) > 0:
                import uvicorn

                from watchpost.server import create_webhook_app

                wh_app = create_webhook_app(self.engine)
                wh_cfg = uvicorn.Config(
                    wh_app,
                    host="127.0.0.1",
                    port=int(self.config.webhook_port),
                    log_level="warning",
                    access_log=False,
                )
                webhook_server = uvicorn.Server(wh_cfg)
                webhook_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
                webhook_task = asyncio.create_task(webhook_server.serve())

            next_prune_at = time.monotonic() + 21600.0

            async def _channel_loop() -> None:
                nonlocal next_prune_at
                while not stopper.is_set():
                    chan_interval = max(1.0, float(self.config.poll_interval_s))
                    await self.engine.poll_all_channels(seed_if_empty=False, debounced=True)
                    await self.engine.flush_quiet_digest_if_due()
                    if time.monotonic() >= next_prune_at:
                        prune_old_media(
                            self.engine.media_dir,
                            max_age_days=int(getattr(self.engine.config, "media_retention_days", 14) or 14),
                        )
                        next_prune_at = time.monotonic() + 21600.0
                    try:
                        await asyncio.wait_for(stopper.wait(), timeout=chan_interval)
                    except asyncio.TimeoutError:
                        pass

            async def _owner_loop() -> None:
                while not stopper.is_set():
                    owner_interval = max(0.5, float(self.config.owner_poll_interval_s))
                    await self.engine.poll_owner_actions_once()
                    try:
                        await asyncio.wait_for(stopper.wait(), timeout=owner_interval)
                    except asyncio.TimeoutError:
                        pass

            chan_task = asyncio.create_task(_channel_loop())
            owner_task = asyncio.create_task(_owner_loop())
            stop_task = asyncio.create_task(stopper.wait())
            try:
                done_wait, _ = await asyncio.wait(
                    [stop_task, chan_task, owner_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done_wait:
                    if t is not stop_task and not t.cancelled() and t.exception() is not None:
                        raise t.exception()  # type: ignore[misc]
            finally:
                stop_task.cancel()
                for srv, tsk in ((api_server, api_task), (webhook_server, webhook_task)):
                    if srv is not None:
                        srv.should_exit = True
                    if tsk is not None:
                        try:
                            await asyncio.wait_for(tsk, timeout=3.0)
                        except Exception:
                            tsk.cancel()
                _done_loops, still_loops = await asyncio.wait([chan_task, owner_task], timeout=5.0)
                for lt in still_loops:
                    lt.cancel()
                await asyncio.gather(chan_task, owner_task, return_exceptions=True)
                await self.engine.wait_background_tasks(timeout_s=5.0)
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
        hooks_file = _antigravity_hooks_file()
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


def _run_async_in_sync(coro: Any, *, timeout_s: float = 15.0) -> Any:
    """Execute an async helper safely whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=timeout_s)


def _nearest_existing_parent(path: Path) -> Path:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def _is_port_free(port: int) -> bool:
    if port <= 0:
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) != 0
    except OSError:
        return True


def _inspect_lockfile(lock_path: Path) -> tuple[bool, str]:
    if not lock_path.is_file() or fcntl is None:
        return False, ""
    try:
        with lock_path.open("r", encoding="utf-8", errors="replace") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return False, ""
            except BlockingIOError:
                pid = fh.read().strip()
                return True, pid or "unknown"
    except OSError:
        return False, ""


def run_doctor(cfg_path: Path | str | None = None, *, live: bool = False) -> dict[str, Any]:
    """Inspect config, dependencies, harnesses, and adapters without leaking secrets or mutating disk."""
    resolved_path = Path(cfg_path).expanduser() if cfg_path else default_config_path()
    checks: dict[str, dict[str, Any]] = {}
    if not resolved_path.is_file():
        return {
            "ok": False,
            "config_path": str(resolved_path),
            "checks": {
                "config": {"ok": False, "detail": f"Missing config file: {resolved_path} (run watchpost setup)"}
            },
            "channels": [],
        }

    try:
        raw_cfg = load_raw_config(resolved_path)
        cfg = load_config(resolved_path)
    except Exception as exc:
        return {
            "ok": False,
            "config_path": str(resolved_path),
            "checks": {"config": {"ok": False, "detail": f"Invalid config: {redact_secrets(str(exc))}"}},
            "channels": [],
        }
    checks["config"] = {"ok": True, "detail": str(resolved_path)}

    # Env placeholders & storage (read-only: never mkdir inside doctor)
    unresolved = find_unresolved_placeholders(raw_cfg, config_path=resolved_path)
    if unresolved:
        missing_str = ", ".join(f"{field}->${{{env}}}" for field, env in unresolved[:6])
        checks["env_placeholders"] = {"ok": False, "detail": f"Unresolved env vars: {missing_str}"}
    else:
        checks["env_placeholders"] = {"ok": True, "detail": "all active placeholders resolved"}

    db_file = Path(cfg.db_path).expanduser()
    probe_dir = db_file.parent if db_file.parent.exists() else _nearest_existing_parent(db_file.parent)
    db_writable = probe_dir.is_dir() and os.access(probe_dir, os.W_OK)
    checks["storage"] = {"ok": db_writable, "detail": str(db_file)}

    if os.name == "posix" and db_file.is_file():
        mode = stat.S_IMODE(db_file.stat().st_mode)
        perm_ok = (mode & 0o077) == 0
        checks["db_permissions"] = {
            "ok": perm_ok,
            "detail": (
                f"mode={oct(mode)}"
                if perm_ok
                else f"insecure mode={oct(mode)} on {db_file} (expected 0600)"
            ),
        }

    ws = Path(cfg.workspace_dir).expanduser()
    checks["workspace"] = {"ok": ws.is_dir(), "detail": str(ws)}

    # Daemon lock & loopback port conflicts
    lock_path = db_file.parent / "watchpost.lock"
    running, running_pid = _inspect_lockfile(lock_path)
    api_port = int(getattr(cfg, "api_port", 0) or 0)
    if running:
        checks["daemon_instance"] = {"ok": True, "detail": f"running (PID {running_pid})"}
    elif api_port > 0 and not _is_port_free(api_port):
        checks["daemon_instance"] = {
            "ok": False,
            "detail": f"stopped, but api_port {api_port} is already in use on 127.0.0.1",
        }
    else:
        checks["daemon_instance"] = {"ok": True, "detail": "stopped (lock free)"}

    # Per-channel workspace and adapter checks
    ext_adapter_names = {e.name for e in cfg.external_adapters}
    ch_issues: list[str] = []
    for ch in cfg.channels:
        if not ch.enabled or ch.adapter == "owner_direct":
            continue
        ch_ws = Path(ch.workspace_dir).expanduser() if ch.workspace_dir else ws
        if not ch_ws.is_dir():
            ch_issues.append(f"{ch.id}: missing workspace {ch_ws}")
        if ch.adapter not in _NATIVE_ADAPTER_NAMES and ch.adapter != "simulator" and ch.adapter not in ext_adapter_names:
            ch_issues.append(f"{ch.id}: unknown adapter {ch.adapter!r}")
    checks["channel_workspaces"] = {
        "ok": not ch_issues,
        "detail": "; ".join(ch_issues) if ch_issues else f"{len(cfg.channels)} channel(s) valid",
    }

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
        tg_detail = "configured" if tg_ok else "enabled but missing bot_token or owner_chat_id"
        if tg_ok and live:
            try:
                bot_info = _run_async_in_sync(
                    get_bot_info(
                        cfg.telegram.bot_token,
                        base_url=cfg.telegram.base_url,
                        proxy_url=cfg.telegram.proxy_url,
                    )
                )
                uname = str(bot_info.get("username") or "").strip()
                tg_detail = f"configured (@{uname}, API reachable)" if uname else "configured (API reachable)"
                try:
                    wh_info = _run_async_in_sync(
                        get_webhook_info(
                            cfg.telegram.bot_token,
                            base_url=cfg.telegram.base_url,
                            proxy_url=cfg.telegram.proxy_url,
                        )
                    )
                    wh_url = str(wh_info.get("url") or "").strip()
                    if wh_url:
                        tg_ok = False
                        tg_detail = f"active Telegram webhook conflicts with getUpdates ({wh_url}); call deleteWebhook first"
                except Exception:
                    pass
            except Exception as exc:
                tg_ok = False
                tg_detail = (
                    f"configured locally, but Telegram API unreachable ({redact_secrets(str(exc))}); "
                    "check network/Clash TUN or telegram.proxy_url"
                )
        checks["telegram"] = {
            "ok": tg_ok,
            "detail": tg_detail,
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
