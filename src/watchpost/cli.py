"""Watchpost CLI entry point (`init`, `init-config`, `setup`, `run`, `doctor`, `channels`, `telegram-setup`, `telegram-whoami`, `install-skill`, `demo`, `serve`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

import httpx
import uvicorn

from watchpost import __version__
from watchpost.adapters.dingtalk import DingTalkAdapter
from watchpost.adapters.discord import DiscordAdapter
from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.adapters.local_sandbox import LocalSandboxAdapter
from watchpost.adapters.slack import SlackAdapter
from watchpost.adapters.telegram import TelegramInboundAdapter
from watchpost.adapters.wecom import WeComAdapter
from watchpost.backends.antigravity import handle_stop_hook_payload
from watchpost.backends.mock import MockAgentBackend
from watchpost.config import (
    DEFAULT_DB_PATH,
    WatchpostConfig,
    default_config_path,
    init_config_file,
    load_config,
    load_raw_config,
    save_config,
)
from watchpost.daemon import WatchpostDaemon, build_engine_from_config, run_doctor
from watchpost.engine import WatchpostEngine
from watchpost.models import AutonomyMode, ChannelConfig, ChatType, EngineConfig, TriggerMode
from watchpost.notifiers.telegram import (
    TelegramNotifier,
    configure_bot_profile,
    detect_owner_chat_id,
    get_bot_info,
    wait_for_owner_chat_id,
)
from watchpost.plugins import check_plugin, scaffold_plugin
from watchpost.sandbox import run_interactive_sandbox
from watchpost.sandbox_ui import create_sandbox_ui_app
from watchpost.security import RedactingLogFilter, api_token_path, atomic_write_text, redact_secrets
from watchpost.server import create_app
from watchpost.setup_wizard import run_setup_wizard
from watchpost.skill_installer import SKILL_TARGETS, install_skill, uninstall_skill
from watchpost.store import WatchpostStore

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _derive_channel_id(adapter: str, chat_id: str) -> str:
    tail = _SLUG_RE.sub("_", chat_id).strip("_")[-24:] or "chat"
    return f"{adapter}_{tail}".lower()


async def _run_demo(db_path: Path | None, *, language: str = "zh") -> int:
    if db_path is None:
        with tempfile.TemporaryDirectory() as tmp:
            return await _run_demo_into(Path(tmp) / "demo.db", language=language)
    return await _run_demo_into(db_path, language=language)


async def _run_demo_into(db_path: Path, *, language: str) -> int:
    store = WatchpostStore(db_path)
    engine = WatchpostEngine(
        store,
        MockAgentBackend(language=language),
        config=EngineConfig(language=language),
    )
    summary = await engine.run_demo_scenario()
    print(
        f"[Watchpost Demo] channels={summary['channels_seeded']} "
        f"threads={summary['threads_created']} downloaded_media={summary['downloaded_media_ids']}"
    )
    for th in store.list_threads():
        print(f"\n--- {th.title} [{th.triage_mode.value} | {th.status.value}] ---")
        for ev in store.list_thread_events(th.id):
            if ev.kind != "agent_reply":
                continue
            for idx, bubble in enumerate(ev.summary_bubbles, start=1):
                print(f"  [Bubble {idx}] {bubble}")
            if ev.suggested_reply:
                print(f"  [Copy Draft] {ev.suggested_reply}")
            if ev.needs_attention:
                print(f"  [Attention] {ev.needs_attention}")
    return 0


async def _run_daemon_cli(
    config_path: Path,
    *,
    once: bool,
    seed: bool,
    api_port: int = 0,
    log_file: str = "",
) -> int:
    if not once:
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
        if log_file.strip():
            log_path = Path(log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_path,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
            )
        redactor = RedactingLogFilter()
        for h in handlers:
            h.addFilter(redactor)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            handlers=handlers,
            force=True,
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    cfg = load_config(config_path)
    if api_port > 0:
        cfg = cfg.model_copy(update={"api_port": api_port})
    engine = build_engine_from_config(cfg)
    daemon = WatchpostDaemon(engine, cfg, config_path=config_path)
    if once:
        try:
            summary = await daemon.run_once(seed_if_empty=seed, debounced=False)
            print(json.dumps(summary, ensure_ascii=False))
        finally:
            await daemon.close_resources()
        return 0

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    if hasattr(signal, "SIGHUP"):
        def _on_sighup() -> None:
            task = asyncio.create_task(daemon.reload_from_disk())
            task.add_done_callback(
                lambda t: logging.getLogger("watchpost.cli").warning("SIGHUP reload failed: %s", t.exception())
                if not t.cancelled() and t.exception() is not None
                else None
            )

        try:
            loop.add_signal_handler(signal.SIGHUP, _on_sighup)
        except NotImplementedError:
            pass
    await daemon.run_forever(stop_event=stop_event, seed_on_start=True)
    return 0


def _handle_hook_stop(args: argparse.Namespace) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    db_arg = str(getattr(args, "db", "") or "").strip()
    cfg_arg = str(getattr(args, "config", "") or "").strip()
    if db_arg:
        db_path = Path(db_arg).expanduser()
    elif cfg_arg:
        db_path = Path(load_config(Path(cfg_arg).expanduser()).db_path).expanduser()
    else:
        default_cfg = default_config_path()
        if default_cfg.is_file():
            db_path = Path(load_config(default_cfg).db_path).expanduser()
        else:
            db_path = Path(DEFAULT_DB_PATH).expanduser()

    store = WatchpostStore(db_path)
    handle_stop_hook_payload(payload, store=store)
    return 0


async def run_telegram_setup(
    *,
    config_path: Path,
    token: str = "",
    chat_id: str = "",
    proxy_url: str | None = None,
    avatar_path: str = "",
    set_avatar: bool = True,
    wait_s: float = 120.0,
    keep_imessage: bool = False,
    no_reload: bool = False,
    send_test: bool = False,
    print_fn: Any = print,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """One-command Telegram onboarding: verify bot, upload avatar, pair owner chat_id, save 0600 config."""
    target_cfg = Path(config_path).expanduser()
    raw_cfg = load_raw_config(target_cfg) if target_cfg.is_file() else WatchpostConfig()
    resolved_cfg = load_config(target_cfg) if target_cfg.is_file() else raw_cfg

    bot_token = token.strip()
    if not bot_token:
        cand = resolved_cfg.telegram.bot_token.strip()
        if cand and not cand.startswith("${"):
            bot_token = cand
    if not bot_token:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token and sys.stdin.isatty():
        bot_token = input("Paste Telegram bot token from @BotFather (/newbot): ").strip()
    if not bot_token:
        raise RuntimeError(
            "Telegram bot_token is required. Create one via @BotFather (/newbot) in Telegram and pass --token."
        )

    eff_proxy = resolved_cfg.telegram.proxy_url if proxy_url is None else proxy_url.strip()
    base_url = raw_cfg.telegram.base_url or "https://api.telegram.org"

    try:
        bot_info = await get_bot_info(bot_token, base_url=base_url, proxy_url=eff_proxy, client=client)
    except Exception as exc:
        raise RuntimeError(
            f"Failed reaching Telegram API getMe ({redact_secrets(str(exc))}); verify network/Clash TUN or pass --proxy-url"
        ) from exc

    bot_username = str(bot_info.get("username") or "").strip()
    bot_name = str(bot_info.get("first_name") or "Watchpost").strip()
    print_fn(
        f"Verified bot: {bot_name} (@{bot_username})" if bot_username else f"Verified bot: {bot_name}"
    )

    profile = await configure_bot_profile(
        bot_token,
        language=raw_cfg.language,
        avatar_path=avatar_path.strip() or None,
        set_avatar=set_avatar,
        base_url=base_url,
        proxy_url=eff_proxy,
        client=client,
    )

    owner_chat_id = chat_id.strip()
    paired_info: dict[str, str] = {}
    pair_nonce = ""
    if not owner_chat_id and wait_s > 0:
        pair_nonce = f"wp_{secrets.token_urlsafe(8)}"
        if bot_username:
            print_fn(
                f"Open https://t.me/{bot_username}?start={pair_nonce} in Telegram and tap Start "
                f"(waiting up to {int(wait_s)}s)..."
            )
        else:
            print_fn(
                f"Send `/start {pair_nonce}` to your bot in a private Telegram chat "
                f"(waiting up to {int(wait_s)}s)..."
            )
        waited = await wait_for_owner_chat_id(
            bot_token,
            expected_nonce=pair_nonce,
            timeout_s=wait_s,
            base_url=base_url,
            proxy_url=eff_proxy,
            client=client,
        )
        if waited is not None:
            owner_chat_id = str(waited.get("chat_id") or "").strip()
            paired_info = waited

    if not owner_chat_id:
        existing_cid = resolved_cfg.telegram.owner_chat_id.strip()
        if existing_cid and not existing_cid.startswith("${"):
            owner_chat_id = existing_cid

    if not owner_chat_id:
        hint = (
            f"https://t.me/{bot_username}?start={pair_nonce}"
            if (bot_username and pair_nonce)
            else f"https://t.me/{bot_username}"
            if bot_username
            else "your bot in a private Telegram chat"
        )
        raise RuntimeError(
            f"No matching private Telegram pairing message received. Open {hint}, send `/start {pair_nonce or '<token>'}`, "
            "or pass --chat-id explicitly."
        )

    if paired_info:
        p_name = paired_info.get("name") or paired_info.get("username") or owner_chat_id
        p_user = f" (@{paired_info['username']})" if paired_info.get("username") else ""
        print_fn(f"Paired Telegram owner: {p_name}{p_user} [chat_id={owner_chat_id}]")

    personal_ws = raw_cfg.personal_workspace_dir or raw_cfg.imessage.personal_workspace_dir or raw_cfg.workspace_dir
    personal_ttl = (
        raw_cfg.personal_session_ttl_s
        if int(raw_cfg.personal_session_ttl_s or 0) > 0
        else raw_cfg.imessage.personal_session_ttl_s
    )
    ack_on_receive = (
        raw_cfg.ack_on_receive
        if raw_cfg.ack_on_receive is not None
        else bool(raw_cfg.imessage.ack_on_receive)
    )

    saved_bot_token = (
        raw_cfg.telegram.bot_token
        if (not token.strip() and raw_cfg.telegram.bot_token.strip().startswith("${"))
        else bot_token
    )
    updated_tg = raw_cfg.telegram.model_copy(
        update={
            "enabled": True,
            "bot_token": saved_bot_token,
            "owner_chat_id": owner_chat_id,
            "base_url": base_url,
            "proxy_url": eff_proxy,
        }
    )
    updated_imsg = (
        raw_cfg.imessage
        if keep_imessage
        else raw_cfg.imessage.model_copy(update={"enabled": False})
    )
    updated_cfg = raw_cfg.model_copy(
        update={
            "personal_workspace_dir": personal_ws,
            "personal_session_ttl_s": personal_ttl,
            "ack_on_receive": ack_on_receive,
            "telegram": updated_tg,
            "imessage": updated_imsg,
        }
    )
    saved_path = save_config(updated_cfg, target_cfg)
    WatchpostStore(Path(updated_cfg.db_path).expanduser())

    test_sent = False
    if send_test:
        is_zh = updated_cfg.language.lower().startswith("zh")
        ping_text = (
            "✅ Watchpost 已接入 Telegram，直接发消息即可对话，发送 /status 可查看运行状态。"
            if is_zh
            else "✅ Watchpost is connected on Telegram. Send any message to chat or /status for daemon status."
        )
        ntf = TelegramNotifier(updated_tg, language=updated_cfg.language, client=client)
        try:
            tok = await ntf.send_message(ping_text)
            test_sent = bool(tok)
        finally:
            await ntf.aclose()

    reloaded = False
    if not no_reload and int(updated_cfg.api_port or 0) > 0 and client is None:
        try:
            tok_file = api_token_path(updated_cfg.db_path)
            headers: dict[str, str] = {}
            if tok_file.is_file():
                bearer = tok_file.read_text(encoding="utf-8", errors="replace").strip()
                if bearer:
                    headers["Authorization"] = f"Bearer {bearer}"
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as local_http:
                resp = await local_http.post(
                    f"http://127.0.0.1:{int(updated_cfg.api_port)}/api/reload",
                    headers=headers,
                )
                reloaded = resp.status_code == 200
        except Exception:
            reloaded = False

    return {
        "ok": True,
        "config_path": str(saved_path),
        "bot_username": bot_username,
        "owner_chat_id": owner_chat_id,
        "profile": profile,
        "imessage_enabled": bool(updated_imsg.enabled),
        "test_sent": test_sent,
        "reloaded": reloaded,
    }


def _handle_channels(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
    action = args.channels_cmd
    if action == "list":
        cfg = load_raw_config(cfg_path)
        if args.json:
            print(json.dumps([c.model_dump(mode="json") for c in cfg.channels], indent=2, ensure_ascii=False))
        elif not cfg.channels:
            print("No channels configured yet. Use `watchpost channels add` to enroll one.")
        else:
            for ch in cfg.channels:
                state = "enabled" if ch.enabled else "disabled"
                print(f"- {ch.id} [{ch.adapter}/{ch.chat_type.value}, {ch.trigger_mode.value}, {ch.autonomy_mode.value}, {state}]: {ch.name} ({ch.external_chat_id})")
        return 0

    if action == "list-remote":
        cfg = load_config(cfg_path)
        adapter_obj: Any = None
        if args.adapter == "feishu":
            adapter_obj = FeishuAdapter(cfg.feishu)
        elif args.adapter == "slack":
            adapter_obj = SlackAdapter(cfg.slack)
        elif args.adapter == "discord":
            adapter_obj = DiscordAdapter(cfg.discord)
        elif args.adapter == "telegram":
            adapter_obj = TelegramInboundAdapter(cfg.telegram)
        elif args.adapter == "dingtalk":
            inbox = (
                Path(cfg.dingtalk.inbox_path).expanduser()
                if cfg.dingtalk.inbox_path
                else Path(cfg.db_path).expanduser().parent / "webhooks" / "dingtalk.jsonl"
            )
            adapter_obj = DingTalkAdapter(cfg.dingtalk, inbox_path=inbox)
        elif args.adapter == "wecom":
            inbox = (
                Path(cfg.wecom.inbox_path).expanduser()
                if cfg.wecom.inbox_path
                else Path(cfg.db_path).expanduser().parent / "webhooks" / "wecom.jsonl"
            )
            adapter_obj = WeComAdapter(cfg.wecom, inbox_path=inbox)
        else:
            ext_cfg = next((e for e in cfg.external_adapters if e.name == args.adapter), None)
            if ext_cfg is not None:
                adapter_obj = ExternalCommandAdapter(ext_cfg)
        if adapter_obj is None:
            print(f"Unsupported or unconfigured adapter for remote discovery: {args.adapter!r}.")
            return 2

        async def _discover() -> list[dict[str, str]]:
            try:
                return await adapter_obj.list_available_chats()
            finally:
                aclose = getattr(adapter_obj, "aclose", None)
                if callable(aclose):
                    await aclose()

        chats = asyncio.run(_discover())
        if args.json:
            print(json.dumps(chats, indent=2, ensure_ascii=False))
        elif not chats:
            print(f"No chats returned by {args.adapter}.")
        else:
            for item in chats:
                print(f"- {item['external_chat_id']} [{item['chat_type']}] {item['name']}")
        return 0

    if action == "add":
        cfg = load_raw_config(cfg_path) if cfg_path.is_file() else WatchpostConfig()
        cid = args.id.strip() or _derive_channel_id(args.adapter, args.chat_id)
        new_ch = ChannelConfig(
            id=cid,
            adapter=args.adapter,
            external_chat_id=args.chat_id,
            name=args.name,
            chat_type=ChatType(args.chat_type),
            trigger_mode=TriggerMode(args.trigger),
            autonomy_mode=AutonomyMode(args.autonomy),
            persona_text=args.persona,
            whisper_prompt=args.whisper_prompt,
            workspace_dir=args.workspace,
        )
        updated = [c for c in cfg.channels if c.id != cid]
        updated.append(new_ch)
        cfg = cfg.model_copy(update={"channels": updated})
        save_config(cfg, cfg_path)
        print(f"Enrolled channel {cid} ({new_ch.name}) in {cfg_path}")
        return 0

    if action == "remove":
        cfg = load_raw_config(cfg_path)
        before = len(cfg.channels)
        remaining = [c for c in cfg.channels if c.id != args.id]
        if len(remaining) == before:
            print(f"Channel not found: {args.id}")
            return 1
        cfg = cfg.model_copy(update={"channels": remaining})
        save_config(cfg, cfg_path)
        print(f"Removed channel {args.id} from {cfg_path}")
        return 0

    return 1


def _render_launchd_plist(
    *,
    label: str,
    python_bin: str,
    config_path: Path,
    log_path: Path,
    path_env: str,
) -> str:
    args = [python_bin, "-m", "watchpost", "run", "--config", str(config_path), "--log-file", str(log_path)]
    args_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in args)
    out_log = log_path.with_suffix(".stdout.log")
    err_log = log_path.with_suffix(".stderr.log")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{_xml_escape(label)}</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>EnvironmentVariables</key>\n    <dict>\n"
        f"        <key>PATH</key>\n        <string>{_xml_escape(path_env)}</string>\n"
        "    </dict>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "    <key>KeepAlive</key>\n    <true/>\n"
        f"    <key>StandardOutPath</key>\n    <string>{_xml_escape(str(out_log))}</string>\n"
        f"    <key>StandardErrorPath</key>\n    <string>{_xml_escape(str(err_log))}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _render_systemd_unit(
    *,
    python_bin: str,
    config_path: Path,
    log_path: Path,
    path_env: str,
) -> str:
    cmd = " ".join(
        shlex.quote(p)
        for p in (python_bin, "-m", "watchpost", "run", "--config", str(config_path), "--log-file", str(log_path))
    )
    return (
        "[Unit]\n"
        "Description=Watchpost headless local coding agent bridge\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=PATH={path_env}\n"
        f"ExecStart={cmd}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _handle_service(args: argparse.Namespace) -> int:
    target = str(getattr(args, "target", "auto") or "auto").strip().lower()
    if target == "auto":
        target = "launchd" if sys.platform == "darwin" else "systemd"
    cfg_path = Path(args.config).expanduser().resolve() if args.config else default_config_path().resolve()
    default_log = (
        Path.home() / "Library" / "Logs" / "watchpost.log"
        if target == "launchd"
        else Path.home() / ".local" / "state" / "watchpost" / "watchpost.log"
    )
    log_path = Path(args.log_file).expanduser().resolve() if args.log_file else default_log
    label = str(getattr(args, "label", "dev.watchpost.daemon") or "dev.watchpost.daemon").strip()
    path_env = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    if target == "launchd":
        unit_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        content = _render_launchd_plist(
            label=label,
            python_bin=sys.executable,
            config_path=cfg_path,
            log_path=log_path,
            path_env=path_env,
        )
    else:
        unit_name = f"{label}.service" if not label.endswith(".service") else label
        unit_path = Path.home() / ".config" / "systemd" / "user" / unit_name
        content = _render_systemd_unit(
            python_bin=sys.executable,
            config_path=cfg_path,
            log_path=log_path,
            path_env=path_env,
        )

    if args.service_cmd == "install":
        if args.print_only:
            print(content, end="")
            return 0
        log_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(unit_path, content, mode=0o644)
        print(f"Installed service definition ({target}): {unit_path}")
        if target == "launchd":
            uid = os.getuid()
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(unit_path)], check=False)
            res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(unit_path)], check=False)
            return int(res.returncode)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        res = subprocess.run(["systemctl", "--user", "enable", "--now", unit_path.name], check=False)
        return int(res.returncode)

    if args.service_cmd == "uninstall":
        if not args.print_only:
            if target == "launchd":
                uid = os.getuid()
                subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(unit_path)], check=False)
            else:
                subprocess.run(["systemctl", "--user", "disable", "--now", unit_path.name], check=False)
            unit_path.unlink(missing_ok=True)
            if target == "systemd":
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print(f"Uninstalled service ({target}): {unit_path}")
        return 0

    if args.service_cmd == "status":
        print(f"Service ({target}): {unit_path} ({'installed' if unit_path.is_file() else 'not installed'})")
        if args.print_only or not unit_path.is_file():
            return 0 if unit_path.is_file() else 1
        if target == "launchd":
            res = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False)
            return int(res.returncode)
        res = subprocess.run(["systemctl", "--user", "status", unit_path.name, "--no-pager"], check=False)
        return int(res.returncode)

    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watchpost",
        description="Headless local background bridge for your coding agent — zero-copy-paste head-starts delivered to your phone",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize Watchpost SQLite WAL database")
    p_init.add_argument("--db", default="./data/watchpost.db", help="Path to SQLite database")

    p_init_cfg = sub.add_parser("init-config", help="Create starter config.json with env placeholders (0600)")
    p_init_cfg.add_argument("--config", default="", help="Path to config.json")
    p_init_cfg.add_argument("--force", action="store_true", help="Overwrite existing config.json")
    p_init_cfg.add_argument("--interactive", "-i", action="store_true", help="Run the interactive setup wizard")

    p_setup = sub.add_parser("setup", help="Interactive setup wizard for Watchpost config, DB, and checks")
    p_setup.add_argument("--config", default="", help="Path to config.json")
    p_setup.add_argument("--force", action="store_true", help="Overwrite existing config.json")
    p_setup.add_argument("--non-interactive", action="store_true", help="Use defaults/flags without prompting")
    p_setup.add_argument("--workspace", default="", help="Workspace directory for agent tasks")
    p_setup.add_argument("--lang", default="", choices=["", "zh", "en"], help="Reply language")
    p_setup.add_argument("--backend", default="", choices=["", "antigravity", "claude_code", "codex", "gemini_cli", "mock"])
    p_setup.add_argument("--backup", default=None, help="Backup failover backend (or 'none')")
    p_setup.add_argument(
        "--platform",
        default="",
        choices=[
            "",
            "feishu-lark-cli",
            "feishu-api",
            "slack",
            "discord",
            "telegram",
            "dingtalk",
            "wecom",
            "external-plugin",
            "none",
        ],
    )
    p_setup.add_argument(
        "--notifier",
        default="",
        choices=[
            "",
            "imessage",
            "telegram",
            "slack-dm",
            "discord-dm",
            "ntfy",
            "bark",
            "pushover",
            "wecom",
            "external-notifier",
            "none",
        ],
    )
    p_setup.add_argument("--no-quiet-hours", action="store_true", help="Disable Quiet Hours")
    p_setup.add_argument("--quiet-start", default="23:00")
    p_setup.add_argument("--quiet-end", default="08:00")
    p_setup.add_argument("--api-port", type=int, default=8787)
    p_setup.add_argument("--db", default="", help="Optional SQLite DB path override")
    p_setup.add_argument("--media-dir", default="", help="Optional media directory override")

    p_run = sub.add_parser("run", help="Start continuous headless Watchpost daemon")
    p_run.add_argument("--config", default="", help="Path to config.json")
    p_run.add_argument("--once", action="store_true", help="Run a single poll + owner-action cycle and exit")
    p_run.add_argument("--seed", action="store_true", help="Seed uninitialized cursors without triggering old backlog")
    p_run.add_argument("--api-port", type=int, default=0, help="Optional localhost JSON API port override")
    p_run.add_argument("--log-file", default="", help="Optional log file path in addition to stdout")

    p_hook = sub.add_parser("hook-stop", help="Consume Antigravity Stop hook JSON on stdin into SQLite")
    p_hook.add_argument("--config", default="", help="Path to config.json")
    p_hook.add_argument("--db", default="", help="Direct path to Watchpost SQLite DB")

    p_doc = sub.add_parser("doctor", help="Verify config, storage, agent CLI, and phone/chat setup")
    p_doc.add_argument("--config", default="", help="Path to config.json")
    p_doc.add_argument("--live", action="store_true", help="Also verify live Telegram Bot API connectivity")
    p_doc.add_argument("--json", action="store_true", help="Print JSON output")

    p_ch = sub.add_parser("channels", help="Inspect, discover, enroll, or remove monitored channels")
    ch_sub = p_ch.add_subparsers(dest="channels_cmd", required=True)

    p_ch_list = ch_sub.add_parser("list", help="List enrolled channels in config.json")
    p_ch_list.add_argument("--config", default="", help="Path to config.json")
    p_ch_list.add_argument("--json", action="store_true")

    p_ch_remote = ch_sub.add_parser("list-remote", help="List discoverable remote chats from an official adapter")
    p_ch_remote.add_argument("--config", default="", help="Path to config.json")
    p_ch_remote.add_argument("--adapter", default="feishu")
    p_ch_remote.add_argument("--json", action="store_true")

    p_ch_add = ch_sub.add_parser("add", help="Enroll or update a monitored channel in config.json")
    p_ch_add.add_argument("--config", default="", help="Path to config.json")
    p_ch_add.add_argument("--id", default="", help="Optional channel ID override")
    p_ch_add.add_argument("--adapter", default="feishu")
    p_ch_add.add_argument("--chat-id", required=True)
    p_ch_add.add_argument("--name", required=True)
    p_ch_add.add_argument("--chat-type", default="group", choices=["p2p", "group"])
    p_ch_add.add_argument("--trigger", default="mention_owner", choices=["all", "mention_owner"])
    p_ch_add.add_argument(
        "--autonomy",
        default="diagnose_only",
        choices=["assistant_only", "diagnose_only", "auto_fix"],
    )
    p_ch_add.add_argument("--persona", default="")
    p_ch_add.add_argument("--whisper-prompt", default="")
    p_ch_add.add_argument("--workspace", default="", help="Optional per-channel workspace directory")

    p_ch_rm = ch_sub.add_parser("remove", help="Remove an enrolled channel from config.json")
    p_ch_rm.add_argument("--config", default="", help="Path to config.json")
    p_ch_rm.add_argument("--id", required=True)

    p_plg = sub.add_parser("plugins", help="List, scaffold starter code for, or check external plugins")
    plg_sub = p_plg.add_subparsers(dest="plugins_cmd", required=True)

    p_plg_list = plg_sub.add_parser("list", help="List configured external adapters and notifiers")
    p_plg_list.add_argument("--config", default="", help="Path to config.json")
    p_plg_list.add_argument("--json", action="store_true")

    p_plg_scaf = plg_sub.add_parser("scaffold", help="Generate a starter Watchpost plugin via watchpost.sdk")
    p_plg_scaf.add_argument("--kind", required=True, choices=["channel", "notifier"])
    p_plg_scaf.add_argument("--name", required=True, help="Plugin slug (lowercase [a-z][a-z0-9_-]*)")
    p_plg_scaf.add_argument("--dest", required=True, help="Output directory")
    p_plg_scaf.add_argument("--force", action="store_true")
    p_plg_scaf.add_argument("--json", action="store_true")

    p_plg_chk = plg_sub.add_parser("check", help="Run conformance checks on an external plugin executable")
    p_plg_chk.add_argument("--kind", required=True, choices=["channel", "notifier"])
    p_plg_chk.add_argument("--cmd", required=True, nargs="+", help="Plugin executable and optional arguments")
    p_plg_chk.add_argument("--test-fetch", action="store_true", help="Also validate sample fetch-recent messages")
    p_plg_chk.add_argument("--timeout", type=float, default=15.0)
    p_plg_chk.add_argument("--json", action="store_true")

    p_tg_setup = sub.add_parser(
        "telegram-setup",
        help="1-command Telegram onboarding: verify bot, upload Watchpost avatar, set / commands, pair owner chat_id, save 0600 config",
    )
    p_tg_setup.add_argument("--config", default="", help="Path to config.json")
    p_tg_setup.add_argument("--token", default="", help="Telegram bot token from @BotFather (or reads config / TELEGRAM_BOT_TOKEN)")
    p_tg_setup.add_argument("--chat-id", default="", help="Optional owner chat_id override (skips interactive Start wait)")
    p_tg_setup.add_argument("--proxy-url", default=None, help="Optional HTTP/SOCKS proxy URL override")
    p_tg_setup.add_argument("--avatar", default="", help="Optional custom bot avatar image path (defaults to bundled Watchpost avatar)")
    p_tg_setup.add_argument("--no-avatar", action="store_true", help="Skip uploading the bot profile avatar")
    p_tg_setup.add_argument("--wait", type=float, default=120.0, help="Seconds to wait for you to tap Start in Telegram (0 to skip)")
    p_tg_setup.add_argument("--keep-imessage", action="store_true", help="Keep iMessage enabled instead of switching exclusively to Telegram")
    p_tg_setup.add_argument("--no-reload", action="store_true", help="Do not call POST /api/reload on the running daemon")
    p_tg_setup.add_argument("--test-message", action="store_true", help="Send a test confirmation message to the paired Telegram chat")

    p_tg = sub.add_parser("telegram-whoami", help="Discover recent Telegram chat IDs talking to your bot")
    p_tg.add_argument("--config", default="", help="Path to config.json")
    p_tg.add_argument("--token", default="", help="Override Telegram bot token")
    p_tg.add_argument("--proxy-url", default=None, help="Optional HTTP/SOCKS proxy URL override")

    p_skill = sub.add_parser("install-skill", help="Install or remove the guided Watchpost Agent Skill")
    p_skill.add_argument("--target", default="claude", choices=sorted(SKILL_TARGETS))
    p_skill.add_argument("--dest", default="", help="Optional custom skill directory path")
    p_skill.add_argument("--uninstall", action="store_true", help="Remove installed skill instead of installing")

    p_svc = sub.add_parser("service", help="Install, inspect, or remove a persistent background user service")
    svc_sub = p_svc.add_subparsers(dest="service_cmd", required=True)
    for svc_action in ("install", "uninstall", "status"):
        sp = svc_sub.add_parser(svc_action, help=f"{svc_action.capitalize()} Watchpost background service")
        sp.add_argument("--config", default="", help="Path to config.json")
        sp.add_argument("--log-file", default="", help="Log file path")
        sp.add_argument("--label", default="dev.watchpost.daemon", help="LaunchAgent label or systemd unit stem")
        sp.add_argument("--target", default="auto", choices=["auto", "launchd", "systemd"])
        sp.add_argument("--print-only", action="store_true", help="Print service unit without running launchctl/systemctl")

    p_sb = sub.add_parser("sandbox", help="Interactive sandbox where you simulate colleagues + phone replies")
    p_sb.add_argument(
        "--backend",
        default="mock",
        choices=["mock", "codex", "claude_code", "gemini_cli", "antigravity"],
        help="Agent backend to run ('mock' for instant offline testing, or real local CLI like 'codex')",
    )
    p_sb.add_argument("--cwd", default=".", help="Workspace directory the agent inspects")
    p_sb.add_argument("--dir", default="", help="Optional persistent sandbox directory (defaults to temporary)")
    p_sb.add_argument("--mode", default="dm", choices=["dm", "group"], help="Initial channel mode")
    p_sb.add_argument(
        "--autonomy",
        default="diagnose_only",
        choices=["assistant_only", "diagnose_only", "auto_fix"],
    )
    p_sb.add_argument("--lang", default="en", help="Output language ('en' or 'zh')")
    p_sb.add_argument("--persona", default="", help="Optional custom channel persona text")

    p_sbs = sub.add_parser("sandbox-send", help="Append one external message into a persistent --dir sandbox")
    p_sbs.add_argument("--dir", required=True, help="Sandbox directory matching `watchpost sandbox --dir`")
    p_sbs.add_argument("--mode", default="dm", choices=["dm", "group"])
    p_sbs.add_argument("--sender", default="Alex")
    p_sbs.add_argument("--text", default="")
    p_sbs.add_argument("--at-owner", action="store_true")
    p_sbs.add_argument("--reply-to", default="")
    p_sbs.add_argument("--img", default="")

    p_sbui = sub.add_parser("sandbox-ui", help="Start 2-column local Web Simulator UI (IME-safe + live debounce)")
    p_sbui.add_argument(
        "--backend",
        default="mock",
        choices=["mock", "codex", "claude_code", "gemini_cli", "antigravity"],
        help="Agent backend ('mock' for offline demo, or real CLI like 'codex')",
    )
    p_sbui.add_argument("--cwd", default=".", help="Workspace directory the agent inspects")
    p_sbui.add_argument("--dir", default="", help="Optional persistent sandbox directory (defaults to temporary)")
    p_sbui.add_argument("--host", default="127.0.0.1")
    p_sbui.add_argument("--port", type=int, default=8799)
    p_sbui.add_argument(
        "--autonomy",
        default="diagnose_only",
        choices=["assistant_only", "diagnose_only", "auto_fix"],
    )
    p_sbui.add_argument("--debounce", type=float, default=5.0, help="Burst debounce window in seconds")
    p_sbui.add_argument("--lang", default="en", help="Output language ('en' or 'zh')")

    p_demo = sub.add_parser("demo", help="Run offline head-start simulation scenario")
    p_demo.add_argument("--db", default="", help="Optional persistent DB path (defaults to temporary)")
    p_demo.add_argument("--lang", default="en", help="Output language ('en' or 'zh')")

    p_serve = sub.add_parser("serve", help="Start headless Watchpost JSON control API server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--db", default="./data/watchpost.db")
    p_serve.add_argument("--lang", default="en")

    args = parser.parse_args(argv)

    if args.command == "init":
        store = WatchpostStore(Path(args.db))
        print(f"Initialized Watchpost SQLite WAL database at {store.db_path}")
        return 0

    if args.command == "init-config":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        if args.interactive:
            rc, _saved = run_setup_wizard(config_path=cfg_path, force=args.force, interactive=True)
            return rc
        try:
            saved = init_config_file(cfg_path, overwrite=args.force)
        except FileExistsError as exc:
            print(str(exc))
            return 1
        print(f"Wrote starter Watchpost config (0600) to {saved}")
        return 0

    if args.command == "setup":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        defaults = {
            "workspace": args.workspace,
            "lang": args.lang,
            "backend": args.backend,
            "backup": args.backup,
            "platform": args.platform,
            "notifier": args.notifier,
            "quiet_hours": not args.no_quiet_hours,
            "quiet_start": args.quiet_start,
            "quiet_end": args.quiet_end,
            "api_port": args.api_port,
            "db_path": args.db,
            "media_dir": args.media_dir,
        }
        rc, _saved = run_setup_wizard(
            config_path=cfg_path,
            force=args.force,
            interactive=not args.non_interactive,
            defaults=defaults,
        )
        return rc

    if args.command == "run":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        return asyncio.run(
            _run_daemon_cli(
                cfg_path,
                once=args.once,
                seed=args.seed,
                api_port=args.api_port,
                log_file=args.log_file,
            )
        )

    if args.command == "hook-stop":
        return _handle_hook_stop(args)

    if args.command == "doctor":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        report: dict[str, Any] = run_doctor(cfg_path, live=args.live)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"Watchpost Doctor ({report['config_path']}) — {'OK' if report['ok'] else 'ISSUES FOUND'}")
            for name, info in report["checks"].items():
                mark = "PASS" if info["ok"] else "FAIL"
                print(f"  [{mark}] {name}: {info['detail']}")
            print(f"  Channels enrolled: {len(report['channels'])}")
        return 0 if report["ok"] else 1

    if args.command == "channels":
        return _handle_channels(args)

    if args.command == "plugins":
        if args.plugins_cmd == "list":
            cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
            cfg = load_raw_config(cfg_path)
            payload = {
                "external_adapters": [e.model_dump(mode="json") for e in cfg.external_adapters],
                "external_notifiers": [e.model_dump(mode="json") for e in cfg.external_notifiers],
            }
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print("External Channel Plugins:")
                for item in cfg.external_adapters:
                    print(f"  - {item.name}: {' '.join(item.command)}")
                if not cfg.external_adapters:
                    print("  (none)")
                print("External Notifier Plugins:")
                for item in cfg.external_notifiers:
                    print(f"  - {item.name}: {' '.join(item.command)}")
                if not cfg.external_notifiers:
                    print("  (none)")
            return 0

        if args.plugins_cmd == "scaffold":
            try:
                res = scaffold_plugin(
                    kind=args.kind,
                    name=args.name,
                    dest=Path(args.dest),
                    force=args.force,
                )
            except (FileExistsError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"Scaffolded {res['kind']} plugin '{res['name']}':")
                print(f"  script: {res['script_path']}")
                print(f"  readme: {res['readme_path']}")
            return 0

        if args.plugins_cmd == "check":
            res = asyncio.run(
                check_plugin(
                    kind=args.kind,
                    command=list(args.cmd),
                    test_fetch=args.test_fetch,
                    timeout_s=args.timeout,
                )
            )
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"Plugin Check ({res['kind']}) — {'OK' if res['ok'] else 'FAILED'}")
                for chk in res["checks"]:
                    mark = "PASS" if chk["ok"] else "FAIL"
                    print(f"  [{mark}] {chk['name']}: {chk['detail']}")
            return 0 if res["ok"] else 1
        return 1

    if args.command == "telegram-setup":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        try:
            res = asyncio.run(
                run_telegram_setup(
                    config_path=cfg_path,
                    token=args.token,
                    chat_id=args.chat_id,
                    proxy_url=args.proxy_url,
                    avatar_path=args.avatar,
                    set_avatar=not args.no_avatar,
                    wait_s=args.wait,
                    keep_imessage=args.keep_imessage,
                    no_reload=args.no_reload,
                    send_test=args.test_message,
                )
            )
        except Exception as exc:
            print(f"Error: {redact_secrets(str(exc))}", file=sys.stderr)
            return 1
        prof = res["profile"]
        print(
            f"Saved Telegram setup ({res['config_path']}, 0600): "
            f"owner_chat_id={res['owner_chat_id']}, "
            f"avatar={prof['avatar']}, commands={prof['commands']}, "
            f"imessage_enabled={res['imessage_enabled']}, reloaded={res['reloaded']}"
        )
        if not res["reloaded"]:
            print(f"Next: start Watchpost with `watchpost run --config {res['config_path']}`")
        return 0

    if args.command == "service":
        return _handle_service(args)

    if args.command == "telegram-whoami":
        token = args.token.strip()
        base_url = "https://api.telegram.org"
        proxy_url = "" if args.proxy_url is None else args.proxy_url.strip()
        if not token:
            cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
            cfg = load_config(cfg_path)
            token = cfg.telegram.bot_token
            base_url = cfg.telegram.base_url
            if args.proxy_url is None:
                proxy_url = cfg.telegram.proxy_url
        chats = asyncio.run(detect_owner_chat_id(token, base_url=base_url, proxy_url=proxy_url))
        print(json.dumps(chats, indent=2, ensure_ascii=False))
        return 0

    if args.command == "install-skill":
        dest = Path(args.dest).expanduser() if args.dest else None
        if args.uninstall:
            removed = uninstall_skill(target=args.target, dest=dest)
            print("Removed Watchpost skill." if removed else "Watchpost skill was not installed there.")
            return 0
        installed = install_skill(target=args.target, dest=dest)
        print(f"Installed Watchpost skill to {installed}")
        return 0

    if args.command == "sandbox":
        return asyncio.run(
            run_interactive_sandbox(
                sandbox_dir=Path(args.dir).expanduser() if args.dir else None,
                backend_name=args.backend,
                workspace_dir=args.cwd,
                initial_mode=args.mode,
                autonomy=AutonomyMode(args.autonomy),
                language=args.lang,
                persona_text=args.persona,
            )
        )

    if args.command == "sandbox-send":
        adapter = LocalSandboxAdapter(Path(args.dir).expanduser())
        chat_id = "sandbox_group" if args.mode == "group" else "sandbox_dm"
        msg = adapter.append_message(
            chat_id,
            sender_name=args.sender,
            text=args.text,
            reply_to_id=args.reply_to,
            mentions_owner=args.at_owner,
            image_path=Path(args.img).expanduser() if args.img else None,
        )
        print(f"Appended {msg.message_id} into {chat_id} ({adapter.inbox_path})")
        return 0

    if args.command == "sandbox-ui":
        if args.dir:
            app = create_sandbox_ui_app(
                Path(args.dir).expanduser(),
                backend_name=args.backend,
                workspace_dir=args.cwd,
                autonomy=AutonomyMode(args.autonomy),
                debounce_s=args.debounce,
                language=args.lang,
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return 0
        with tempfile.TemporaryDirectory(prefix="watchpost-sandbox-ui-") as tmp:
            app = create_sandbox_ui_app(
                Path(tmp),
                backend_name=args.backend,
                workspace_dir=args.cwd,
                autonomy=AutonomyMode(args.autonomy),
                debounce_s=args.debounce,
                language=args.lang,
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

    if args.command == "demo":
        target = Path(args.db) if args.db else None
        return asyncio.run(_run_demo(target, language=args.lang))

    if args.command == "serve":
        app = create_app(Path(args.db), config=EngineConfig(language=args.lang))
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
