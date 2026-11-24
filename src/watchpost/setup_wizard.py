"""Interactive environment-aware setup wizard for Watchpost (`watchpost setup`)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from watchpost.config import (
    DEFAULT_DB_PATH,
    DEFAULT_MEDIA_DIR,
    BackendConfig,
    BarkConfig,
    DingTalkConfig,
    DiscordConfig,
    FeishuConfig,
    IMessageConfig,
    NtfyConfig,
    PushoverConfig,
    SlackConfig,
    TelegramConfig,
    WatchpostConfig,
    WeComConfig,
    save_config,
)
from watchpost.daemon import run_doctor
from watchpost.models import QuietHoursConfig
from watchpost.store import WatchpostStore


def detect_environment() -> dict[str, Any]:
    """Detect local OS, agent CLIs, chat CLI tools, and Whisper binaries."""
    ag_bin = shutil.which("agentapi")
    if not ag_bin:
        cand = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi"
        if cand.is_file():
            ag_bin = str(cand)

    detected_backends: dict[str, str] = {}
    for key, path in (
        ("antigravity", ag_bin),
        ("claude_code", shutil.which("claude")),
        ("codex", shutil.which("codex")),
        ("gemini_cli", shutil.which("gemini")),
    ):
        if path:
            detected_backends[key] = path

    rec_backend = next(iter(detected_backends), "claude_code")
    rec_backup = "codex" if ("codex" in detected_backends and rec_backend != "codex") else ""
    lark_cli = shutil.which("lark-cli")
    rec_platform = "feishu-lark-cli" if lark_cli else "feishu-api"
    rec_notifier = "telegram"

    return {
        "platform": sys.platform,
        "workspace_dir": str(Path.cwd()),
        "backends": detected_backends,
        "recommended_backend": rec_backend,
        "recommended_backup": rec_backup,
        "lark_cli": lark_cli or "",
        "whisper_cli": shutil.which("whisper-cli") or "",
        "ffmpeg": shutil.which("ffmpeg") or "",
        "recommended_platform": rec_platform,
        "recommended_notifier": rec_notifier,
    }


def _ask(
    prompt: str,
    default: str,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
) -> str:
    if not interactive:
        return default
    raw = input_fn(f"{prompt} [{default}]: ").strip()
    return raw or default


def _ask_yes_no(
    prompt: str,
    default: bool,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
) -> bool:
    if not interactive:
        return default
    hint = "Y/n" if default else "y/N"
    raw = input_fn(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "true"}


def run_setup_wizard(
    *,
    config_path: Path,
    force: bool = False,
    interactive: bool = True,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
    defaults: dict[str, Any] | None = None,
) -> tuple[int, Path | None]:
    """Guide first-time setup and write a 0600 `config.json` using env-var placeholders."""
    ask_fn = input_fn if input_fn is not None else (lambda prompt: input(prompt))
    say_fn = print_fn if print_fn is not None else print
    target_cfg = Path(config_path).expanduser()
    opts = dict(defaults or {})

    if target_cfg.exists() and not force:
        if not interactive:
            say_fn(f"Config already exists: {target_cfg} (pass --force to overwrite)")
            return 1, None
        overwrite = _ask_yes_no(
            f"Config already exists at {target_cfg}. Overwrite?",
            False,
            interactive=True,
            input_fn=ask_fn,
        )
        if not overwrite:
            say_fn("Setup cancelled.")
            return 1, None

    env_info = detect_environment()
    if interactive:
        found_clis = ", ".join(env_info["backends"].keys()) or "none (mock available for offline testing)"
        say_fn("Watchpost Interactive Setup")
        say_fn(f"  Detected agent CLIs: {found_clis}")
        say_fn(f"  Detected lark-cli: {'yes' if env_info['lark_cli'] else 'no'}")

    ws_default = str(opts.get("workspace") or env_info["workspace_dir"])
    workspace_dir = _ask("Workspace directory", ws_default, interactive=interactive, input_fn=ask_fn)

    lang_default = str(opts.get("lang") or "zh")
    lang_raw = _ask("Reply language (zh/en)", lang_default, interactive=interactive, input_fn=ask_fn).lower()
    language = "en" if lang_raw.startswith("en") else "zh"

    valid_backends = {"antigravity", "claude_code", "codex", "gemini_cli", "mock"}
    be_default = str(opts.get("backend") or env_info["recommended_backend"])
    backend_preset = _ask(
        "Primary agent backend (antigravity/claude_code/codex/gemini_cli/mock)",
        be_default,
        interactive=interactive,
        input_fn=ask_fn,
    ).strip()
    if backend_preset not in valid_backends:
        say_fn(f"Unsupported backend: {backend_preset!r}")
        return 1, None

    backup_default = str(
        opts["backup"] if opts.get("backup") is not None else env_info["recommended_backup"]
    )
    backup_preset = _ask(
        "Optional backup failover backend (e.g. codex, or none)",
        backup_default or "none",
        interactive=interactive,
        input_fn=ask_fn,
    ).strip()
    if backup_preset.lower() == "none" or backup_preset == backend_preset:
        backup_preset = ""

    plat_default = str(opts.get("platform") or env_info["recommended_platform"])
    platform_choice = _ask(
        "Work chat source (feishu-lark-cli/feishu-api/slack/discord/telegram/dingtalk/wecom/external-plugin/none)",
        plat_default,
        interactive=interactive,
        input_fn=ask_fn,
    ).strip().lower()

    ntf_default = str(opts.get("notifier") or env_info["recommended_notifier"])
    notifier_choice = _ask(
        "Owner phone/desktop entrance (imessage/telegram/slack-dm/discord-dm/ntfy/bark/pushover/wecom/external-notifier/none)",
        ntf_default,
        interactive=interactive,
        input_fn=ask_fn,
    ).strip().lower()

    quiet_default = bool(opts.get("quiet_hours", True))
    quiet_enabled = _ask_yes_no(
        "Enable Quiet Hours (silent overnight diagnosis + morning briefing)?",
        quiet_default,
        interactive=interactive,
        input_fn=ask_fn,
    )
    quiet_start = str(opts.get("quiet_start") or "23:00")
    quiet_end = str(opts.get("quiet_end") or "08:00")
    if quiet_enabled and interactive:
        quiet_start = _ask("Quiet Hours start (HH:MM)", quiet_start, interactive=True, input_fn=ask_fn)
        quiet_end = _ask("Quiet Hours end (HH:MM)", quiet_end, interactive=True, input_fn=ask_fn)

    port_default = str(opts.get("api_port", 8787))
    port_raw = _ask(
        "Local JSON control API port (0 to disable)",
        port_default,
        interactive=interactive,
        input_fn=ask_fn,
    )
    try:
        api_port = max(0, min(int(port_raw), 65535))
    except ValueError:
        api_port = 8787

    if platform_choice == "feishu-lark-cli":
        feishu_cfg = FeishuConfig(
            enabled=True,
            auth_mode="lark_cli",
            lark_bin=env_info["lark_cli"] or "lark-cli",
            owner_open_id="${FEISHU_OWNER_OPEN_ID}",
        )
    elif platform_choice == "feishu-api":
        feishu_cfg = FeishuConfig(
            enabled=True,
            auth_mode="tenant",
            app_id="${FEISHU_APP_ID}",
            app_secret="${FEISHU_APP_SECRET}",
            owner_open_id="${FEISHU_OWNER_OPEN_ID}",
        )
    else:
        feishu_cfg = FeishuConfig(enabled=False)

    tg_cfg = TelegramConfig(
        enabled=(notifier_choice == "telegram"),
        inbound_enabled=(platform_choice == "telegram"),
        bot_token="${TELEGRAM_BOT_TOKEN}" if (notifier_choice == "telegram" or platform_choice == "telegram") else "",
        owner_chat_id="${TELEGRAM_OWNER_CHAT_ID}" if notifier_choice == "telegram" else "",
        owner_username="${TELEGRAM_OWNER_USERNAME}" if platform_choice == "telegram" else "",
    )

    imsg_cfg = (
        IMessageConfig(
            enabled=True,
            owner_handle="${WATCHPOST_IMESSAGE_HANDLE}",
            personal_workspace_dir=workspace_dir,
            personal_session_ttl_s=86400,
            ack_on_receive=False,
        )
        if notifier_choice == "imessage"
        else IMessageConfig(enabled=False)
    )

    slack_cfg = (
        SlackConfig(
            enabled=(platform_choice == "slack"),
            dm_enabled=(notifier_choice == "slack-dm"),
            bot_token="${SLACK_BOT_TOKEN}",
            owner_user_id="${SLACK_OWNER_USER_ID}",
        )
        if (platform_choice == "slack" or notifier_choice == "slack-dm")
        else SlackConfig(enabled=False)
    )

    discord_cfg = (
        DiscordConfig(
            enabled=(platform_choice == "discord"),
            dm_enabled=(notifier_choice == "discord-dm"),
            bot_token="${DISCORD_BOT_TOKEN}",
            owner_user_id="${DISCORD_OWNER_USER_ID}",
        )
        if (platform_choice == "discord" or notifier_choice == "discord-dm")
        else DiscordConfig(enabled=False)
    )

    dingtalk_cfg = (
        DingTalkConfig(
            enabled=True,
            app_key="${DINGTALK_APP_KEY}",
            app_secret="${DINGTALK_APP_SECRET}",
            webhook_secret="${DINGTALK_WEBHOOK_SECRET}",
            owner_user_id="${DINGTALK_OWNER_USER_ID}",
        )
        if platform_choice == "dingtalk"
        else DingTalkConfig(enabled=False)
    )

    wecom_cfg = (
        WeComConfig(
            enabled=(platform_choice == "wecom"),
            notify_enabled=(notifier_choice == "wecom"),
            corp_id="${WECOM_CORP_ID}",
            corp_secret="${WECOM_CORP_SECRET}",
            callback_token="${WECOM_CALLBACK_TOKEN}" if platform_choice == "wecom" else "",
            owner_user_id="${WECOM_OWNER_USER_ID}",
        )
        if (platform_choice == "wecom" or notifier_choice == "wecom")
        else WeComConfig(enabled=False)
    )

    ntfy_cfg = (
        NtfyConfig(enabled=True, topic="${NTFY_TOPIC}", reply_topic="${NTFY_REPLY_TOPIC}")
        if notifier_choice == "ntfy"
        else NtfyConfig(enabled=False)
    )

    bark_cfg = (
        BarkConfig(enabled=True, device_key="${BARK_DEVICE_KEY}")
        if notifier_choice == "bark"
        else BarkConfig(enabled=False)
    )

    pushover_cfg = (
        PushoverConfig(enabled=True, app_token="${PUSHOVER_APP_TOKEN}", user_key="${PUSHOVER_USER_KEY}")
        if notifier_choice == "pushover"
        else PushoverConfig(enabled=False)
    )

    db_path = str(opts.get("db_path") or DEFAULT_DB_PATH)
    media_dir = str(opts.get("media_dir") or DEFAULT_MEDIA_DIR)
    codex_sandbox = "read-only" if (backend_preset == "codex" or backup_preset == "codex") else ""

    cfg = WatchpostConfig(
        db_path=db_path,
        media_dir=media_dir,
        workspace_dir=workspace_dir,
        personal_workspace_dir=workspace_dir,
        personal_session_ttl_s=86400,
        ack_on_receive=False,
        language=language,
        api_port=api_port,
        quiet_hours=QuietHoursConfig(
            enabled=quiet_enabled,
            start=quiet_start,
            end=quiet_end,
        ),
        backend=BackendConfig(
            preset=backend_preset,
            backup_preset=backup_preset,
            codex_sandbox=codex_sandbox,
        ),
        feishu=feishu_cfg,
        telegram=tg_cfg,
        imessage=imsg_cfg,
        slack=slack_cfg,
        discord=discord_cfg,
        dingtalk=dingtalk_cfg,
        wecom=wecom_cfg,
        ntfy=ntfy_cfg,
        bark=bark_cfg,
        pushover=pushover_cfg,
    )

    saved_path = save_config(cfg, target_cfg)
    WatchpostStore(Path(db_path).expanduser())
    report = run_doctor(saved_path)

    say_fn(f"\nSaved Watchpost config (0600): {saved_path}")
    say_fn("Doctor checks:")
    for check_name, info in report.get("checks", {}).items():
        mark = "PASS" if info.get("ok") else "FAIL"
        say_fn(f"  [{mark}] {check_name}: {info.get('detail')}")

    say_fn("\nNext steps:")
    if platform_choice == "feishu-api":
        say_fn("  export FEISHU_APP_ID=... FEISHU_APP_SECRET=... FEISHU_OWNER_OPEN_ID=...")
    elif platform_choice == "feishu-lark-cli":
        say_fn("  export FEISHU_OWNER_OPEN_ID=ou_...   # run `lark-cli auth login` if needed")
    elif platform_choice == "slack":
        say_fn("  export SLACK_BOT_TOKEN=xoxb-... SLACK_OWNER_USER_ID=U...")
    elif platform_choice == "discord":
        say_fn("  export DISCORD_BOT_TOKEN=... DISCORD_OWNER_USER_ID=...")
    elif platform_choice == "telegram":
        say_fn("  export TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_USERNAME=...")
    elif platform_choice == "dingtalk":
        say_fn("  export DINGTALK_APP_KEY=... DINGTALK_APP_SECRET=... DINGTALK_WEBHOOK_SECRET=... DINGTALK_OWNER_USER_ID=...")
    elif platform_choice == "wecom":
        say_fn("  export WECOM_CORP_ID=... WECOM_CORP_SECRET=... WECOM_CALLBACK_TOKEN=... WECOM_OWNER_USER_ID=...")
    elif platform_choice == "external-plugin":
        say_fn("  watchpost plugins scaffold --kind channel --name my_chat --dest ./plugins/my_chat")

    if notifier_choice == "imessage":
        say_fn("  export WATCHPOST_IMESSAGE_HANDLE='+15550100'")
    elif notifier_choice == "telegram":
        say_fn(f"  watchpost telegram-setup --config {saved_path}")
    elif notifier_choice == "slack-dm":
        say_fn("  export SLACK_BOT_TOKEN=xoxb-... SLACK_OWNER_USER_ID=U...")
    elif notifier_choice == "discord-dm":
        say_fn("  export DISCORD_BOT_TOKEN=... DISCORD_OWNER_USER_ID=...")
    elif notifier_choice == "ntfy":
        say_fn("  export NTFY_TOPIC=... NTFY_REPLY_TOPIC=...")
    elif notifier_choice == "bark":
        say_fn("  export BARK_DEVICE_KEY=...")
    elif notifier_choice == "pushover":
        say_fn("  export PUSHOVER_APP_TOKEN=... PUSHOVER_USER_KEY=...")
    elif notifier_choice == "wecom":
        say_fn("  export WECOM_CORP_ID=... WECOM_CORP_SECRET=... WECOM_AGENT_ID=1000002 WECOM_OWNER_USER_ID=...")
    elif notifier_choice == "external-notifier":
        say_fn("  watchpost plugins scaffold --kind notifier --name my_push --dest ./plugins/my_push")
    say_fn(f"  watchpost run --config {saved_path}")
    return 0, saved_path
