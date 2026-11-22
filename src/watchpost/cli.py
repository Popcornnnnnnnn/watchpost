"""Watchpost CLI entry point (`init`, `init-config`, `run`, `doctor`, `channels`, `telegram-whoami`, `install-skill`, `demo`, `serve`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn

from watchpost import __version__
from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.adapters.local_sandbox import LocalSandboxAdapter
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
from watchpost.notifiers.telegram import detect_owner_chat_id
from watchpost.sandbox import run_interactive_sandbox
from watchpost.sandbox_ui import create_sandbox_ui_app
from watchpost.server import create_app
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


async def _run_daemon_cli(config_path: Path, *, once: bool, seed: bool, api_port: int = 0) -> int:
    cfg = load_config(config_path)
    if api_port > 0:
        cfg = cfg.model_copy(update={"api_port": api_port})
    engine = build_engine_from_config(cfg)
    daemon = WatchpostDaemon(engine, cfg)
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

    p_run = sub.add_parser("run", help="Start continuous headless Watchpost daemon")
    p_run.add_argument("--config", default="", help="Path to config.json")
    p_run.add_argument("--once", action="store_true", help="Run a single poll + owner-action cycle and exit")
    p_run.add_argument("--seed", action="store_true", help="Seed uninitialized cursors without triggering old backlog")
    p_run.add_argument("--api-port", type=int, default=0, help="Optional localhost JSON API port override")

    p_hook = sub.add_parser("hook-stop", help="Consume Antigravity Stop hook JSON on stdin into SQLite")
    p_hook.add_argument("--config", default="", help="Path to config.json")
    p_hook.add_argument("--db", default="", help="Direct path to Watchpost SQLite DB")

    p_doc = sub.add_parser("doctor", help="Verify config, storage, agent CLI, and phone/chat setup")
    p_doc.add_argument("--config", default="", help="Path to config.json")
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

    p_tg = sub.add_parser("telegram-whoami", help="Discover recent Telegram chat IDs talking to your bot")
    p_tg.add_argument("--config", default="", help="Path to config.json")
    p_tg.add_argument("--token", default="", help="Override Telegram bot token")

    p_skill = sub.add_parser("install-skill", help="Install or remove the guided Watchpost Agent Skill")
    p_skill.add_argument("--target", default="claude", choices=sorted(SKILL_TARGETS))
    p_skill.add_argument("--dest", default="", help="Optional custom skill directory path")
    p_skill.add_argument("--uninstall", action="store_true", help="Remove installed skill instead of installing")

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
    p_sb.add_argument("--lang", default="zh", help="Output language ('zh' or 'en')")
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
    p_sbui.add_argument("--lang", default="zh", help="Output language ('zh' or 'en')")

    p_demo = sub.add_parser("demo", help="Run offline head-start simulation scenario")
    p_demo.add_argument("--db", default="", help="Optional persistent DB path (defaults to temporary)")
    p_demo.add_argument("--lang", default="zh", help="Output language ('zh' or 'en')")

    p_serve = sub.add_parser("serve", help="Start headless Watchpost JSON control API server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--db", default="./data/watchpost.db")
    p_serve.add_argument("--lang", default="zh")

    args = parser.parse_args(argv)

    if args.command == "init":
        store = WatchpostStore(Path(args.db))
        print(f"Initialized Watchpost SQLite WAL database at {store.db_path}")
        return 0

    if args.command == "init-config":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        try:
            saved = init_config_file(cfg_path, overwrite=args.force)
        except FileExistsError as exc:
            print(str(exc))
            return 1
        print(f"Wrote starter Watchpost config (0600) to {saved}")
        return 0

    if args.command == "run":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        return asyncio.run(
            _run_daemon_cli(
                cfg_path,
                once=args.once,
                seed=args.seed,
                api_port=args.api_port,
            )
        )

    if args.command == "hook-stop":
        return _handle_hook_stop(args)

    if args.command == "doctor":
        cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
        report: dict[str, Any] = run_doctor(cfg_path)
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

    if args.command == "telegram-whoami":
        token = args.token.strip()
        base_url = "https://api.telegram.org"
        if not token:
            cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
            cfg = load_config(cfg_path)
            token = cfg.telegram.bot_token
            base_url = cfg.telegram.base_url
        chats = asyncio.run(detect_owner_chat_id(token, base_url=base_url))
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
