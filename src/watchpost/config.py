"""Configuration loader, environment placeholder expansion, and 0600 persistence for Watchpost."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from watchpost.models import ChannelConfig, QuietHoursConfig
from watchpost.security import atomic_write_text

_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_DB_PATH = "~/.local/share/watchpost/watchpost.db"
DEFAULT_MEDIA_DIR = "~/.local/share/watchpost/media"


class BackendConfig(BaseModel):
    preset: str = "claude_code"
    backup_preset: str = ""
    start_cmd: list[str] = Field(default_factory=list)
    steer_cmd: list[str] = Field(default_factory=list)
    timeout_s: float = 300.0
    backup_timeout_s: float = 300.0
    antigravity_bin: str = "agentapi"
    antigravity_model: str = ""
    antigravity_profile: str = ""
    codex_sandbox: str = ""


class WhisperConfig(BaseModel):
    enabled: bool = False
    whisper_bin: str = "whisper-cli"
    model_path: str = ""
    ffmpeg_bin: str = "ffmpeg"


class FeishuConfig(BaseModel):
    enabled: bool = False
    auth_mode: Literal["tenant", "user", "lark_cli"] = "tenant"
    app_id: str = ""
    app_secret: str = ""
    user_access_token: str = ""
    lark_bin: str = "lark-cli"
    owner_open_id: str = ""
    owner_name: str = ""
    base_url: str = "https://open.feishu.cn/open-apis"


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    owner_chat_id: str = ""
    inbound_enabled: bool = False
    inbound_bot_token: str = ""
    owner_username: str = ""
    owner_user_id: str = ""
    base_url: str = "https://api.telegram.org"
    poll_timeout_s: int = 0
    proxy_url: str = ""


class SlackConfig(BaseModel):
    enabled: bool = False
    dm_enabled: bool = False
    bot_token: str = ""
    owner_user_id: str = ""
    owner_dm_channel_id: str = ""
    base_url: str = "https://slack.com/api"
    fetch_thread_replies: bool = True


class DiscordConfig(BaseModel):
    enabled: bool = False
    dm_enabled: bool = False
    bot_token: str = ""
    owner_user_id: str = ""
    owner_dm_channel_id: str = ""
    guild_ids: list[str] = Field(default_factory=list)
    base_url: str = "https://discord.com/api/v10"


class DingTalkConfig(BaseModel):
    enabled: bool = False
    app_key: str = ""
    app_secret: str = ""
    robot_code: str = ""
    webhook_secret: str = ""
    owner_user_id: str = ""
    inbox_path: str = ""
    allow_unsigned_webhooks: bool = False
    base_url: str = "https://api.dingtalk.com"


class WeComConfig(BaseModel):
    enabled: bool = False
    notify_enabled: bool = False
    corp_id: str = ""
    corp_secret: str = ""
    agent_id: int = 0
    callback_token: str = ""
    owner_user_id: str = ""
    owner_name: str = ""
    inbox_path: str = ""
    allow_unsigned_webhooks: bool = False
    base_url: str = "https://qyapi.weixin.qq.com/cgi-bin"


class NtfyConfig(BaseModel):
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    topic: str = ""
    reply_topic: str = ""
    token: str = ""
    priority: str = "default"
    tags: list[str] = Field(default_factory=list)
    notify_on_start: bool = False


class BarkConfig(BaseModel):
    enabled: bool = False
    server_url: str = "https://api.day.app"
    device_key: str = ""
    group: str = "Watchpost"
    sound: str = ""
    icon: str = ""
    level: str = "active"
    auto_copy: bool = False
    notify_on_start: bool = False


class PushoverConfig(BaseModel):
    enabled: bool = False
    app_token: str = ""
    user_key: str = ""
    device: str = ""
    sound: str = ""
    priority: int = 0
    notify_on_start: bool = False
    base_url: str = "https://api.pushover.net"


class IMessageConfig(BaseModel):
    enabled: bool = False
    owner_handle: str = ""
    allowed_handles: list[str] = Field(default_factory=list)
    chat_db_path: str = "~/Library/Messages/chat.db"
    personal_workspace_dir: str = ""
    personal_session_ttl_s: int = 3600
    ack_on_receive: bool = True
    dry_run: bool = False


class ExternalAdapterConfig(BaseModel):
    name: str
    command: list[str] = Field(default_factory=list)
    timeout_s: float = 45.0
    env: dict[str, str] = Field(default_factory=dict)


class ExternalNotifierConfig(BaseModel):
    name: str
    command: list[str] = Field(default_factory=list)
    timeout_s: float = 30.0
    env: dict[str, str] = Field(default_factory=dict)


class WatchpostConfig(BaseModel):
    db_path: str = DEFAULT_DB_PATH
    media_dir: str = DEFAULT_MEDIA_DIR
    workspace_dir: str = "."
    personal_workspace_dir: str = ""
    personal_session_ttl_s: int = 0
    ack_on_receive: bool | None = None
    language: str = "en"
    poll_interval_s: float = 10.0
    owner_poll_interval_s: float = 3.0
    debounce_s: float = 25.0
    max_debounce_s: float = 60.0
    api_port: int = 0
    webhook_port: int = 0
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    cross_channel_window_s: int = 7200
    cross_channel_max_items: int = 5
    backend: BackendConfig = Field(default_factory=BackendConfig)
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    imessage: IMessageConfig = Field(default_factory=IMessageConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    wecom: WeComConfig = Field(default_factory=WeComConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    bark: BarkConfig = Field(default_factory=BarkConfig)
    pushover: PushoverConfig = Field(default_factory=PushoverConfig)
    external_adapters: list[ExternalAdapterConfig] = Field(default_factory=list)
    external_notifiers: list[ExternalNotifierConfig] = Field(default_factory=list)
    channels: list[ChannelConfig] = Field(default_factory=list)


def default_config_path() -> Path:
    override = os.environ.get("WATCHPOST_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "watchpost" / "config.json"


def expand_env_placeholders(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively expand `${ENV_VAR}` placeholders using `env` (or `os.environ`)."""
    environ = env if env is not None else os.environ
    if isinstance(value, str):
        return _ENV_PLACEHOLDER_RE.sub(lambda m: environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env_placeholders(item, environ) for item in value]
    if isinstance(value, dict):
        return {k: expand_env_placeholders(v, environ) for k, v in value.items()}
    return value


def _apply_env_fallbacks(
    section: BaseModel,
    field_env_pairs: tuple[tuple[str, str], ...],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for field_name, env_name in field_env_pairs:
        cur = getattr(section, field_name, None)
        val = environ.get(env_name, "").strip()
        if not cur and val:
            updates[field_name] = val
    return updates


def resolve_config(cfg: WatchpostConfig, *, env: Mapping[str, str] | None = None) -> WatchpostConfig:
    """Expand `${ENV_VAR}` placeholders, apply standard env fallbacks, and expand user paths."""
    environ = env if env is not None else os.environ
    expanded = expand_env_placeholders(cfg.model_dump(mode="python"), environ)
    resolved = WatchpostConfig.model_validate(expanded)

    feishu_updates = _apply_env_fallbacks(
        resolved.feishu,
        (
            ("app_id", "FEISHU_APP_ID"),
            ("app_secret", "FEISHU_APP_SECRET"),
            ("user_access_token", "FEISHU_USER_ACCESS_TOKEN"),
            ("owner_open_id", "FEISHU_OWNER_OPEN_ID"),
        ),
        environ,
    )
    tg_updates = _apply_env_fallbacks(
        resolved.telegram,
        (
            ("bot_token", "TELEGRAM_BOT_TOKEN"),
            ("owner_chat_id", "TELEGRAM_OWNER_CHAT_ID"),
            ("inbound_bot_token", "TELEGRAM_INBOUND_BOT_TOKEN"),
            ("owner_username", "TELEGRAM_OWNER_USERNAME"),
            ("owner_user_id", "TELEGRAM_OWNER_USER_ID"),
        ),
        environ,
    )
    imsg_updates = _apply_env_fallbacks(
        resolved.imessage,
        (("owner_handle", "WATCHPOST_IMESSAGE_HANDLE"),),
        environ,
    )
    slack_updates = _apply_env_fallbacks(
        resolved.slack,
        (
            ("bot_token", "SLACK_BOT_TOKEN"),
            ("owner_user_id", "SLACK_OWNER_USER_ID"),
            ("owner_dm_channel_id", "SLACK_OWNER_DM_CHANNEL_ID"),
        ),
        environ,
    )
    discord_updates = _apply_env_fallbacks(
        resolved.discord,
        (
            ("bot_token", "DISCORD_BOT_TOKEN"),
            ("owner_user_id", "DISCORD_OWNER_USER_ID"),
            ("owner_dm_channel_id", "DISCORD_OWNER_DM_CHANNEL_ID"),
        ),
        environ,
    )
    dingtalk_updates = _apply_env_fallbacks(
        resolved.dingtalk,
        (
            ("app_key", "DINGTALK_APP_KEY"),
            ("app_secret", "DINGTALK_APP_SECRET"),
            ("robot_code", "DINGTALK_ROBOT_CODE"),
            ("webhook_secret", "DINGTALK_WEBHOOK_SECRET"),
            ("owner_user_id", "DINGTALK_OWNER_USER_ID"),
        ),
        environ,
    )
    if resolved.dingtalk.inbox_path:
        dingtalk_updates["inbox_path"] = str(Path(resolved.dingtalk.inbox_path).expanduser())

    wecom_updates = _apply_env_fallbacks(
        resolved.wecom,
        (
            ("corp_id", "WECOM_CORP_ID"),
            ("corp_secret", "WECOM_CORP_SECRET"),
            ("callback_token", "WECOM_CALLBACK_TOKEN"),
            ("owner_user_id", "WECOM_OWNER_USER_ID"),
        ),
        environ,
    )
    if not resolved.wecom.agent_id and environ.get("WECOM_AGENT_ID", "").strip():
        try:
            wecom_updates["agent_id"] = int(environ["WECOM_AGENT_ID"].strip())
        except ValueError:
            pass
    if resolved.wecom.inbox_path:
        wecom_updates["inbox_path"] = str(Path(resolved.wecom.inbox_path).expanduser())

    ntfy_updates = _apply_env_fallbacks(
        resolved.ntfy,
        (
            ("topic", "NTFY_TOPIC"),
            ("reply_topic", "NTFY_REPLY_TOPIC"),
            ("token", "NTFY_TOKEN"),
        ),
        environ,
    )
    if resolved.ntfy.server_url == "https://ntfy.sh" and environ.get("NTFY_SERVER_URL", "").strip():
        ntfy_updates["server_url"] = environ["NTFY_SERVER_URL"].strip()

    bark_updates = _apply_env_fallbacks(
        resolved.bark,
        (("device_key", "BARK_DEVICE_KEY"),),
        environ,
    )
    if resolved.bark.server_url == "https://api.day.app" and environ.get("BARK_SERVER_URL", "").strip():
        bark_updates["server_url"] = environ["BARK_SERVER_URL"].strip()

    pushover_updates = _apply_env_fallbacks(
        resolved.pushover,
        (
            ("app_token", "PUSHOVER_APP_TOKEN"),
            ("user_key", "PUSHOVER_USER_KEY"),
            ("device", "PUSHOVER_DEVICE"),
        ),
        environ,
    )

    return resolved.model_copy(
        update={
            "db_path": str(Path(resolved.db_path).expanduser()),
            "media_dir": str(Path(resolved.media_dir).expanduser()),
            "workspace_dir": str(Path(resolved.workspace_dir).expanduser()),
            "personal_workspace_dir": str(Path(resolved.personal_workspace_dir).expanduser())
            if resolved.personal_workspace_dir
            else "",
            "feishu": resolved.feishu.model_copy(update=feishu_updates) if feishu_updates else resolved.feishu,
            "telegram": resolved.telegram.model_copy(update=tg_updates) if tg_updates else resolved.telegram,
            "slack": resolved.slack.model_copy(update=slack_updates) if slack_updates else resolved.slack,
            "discord": resolved.discord.model_copy(update=discord_updates) if discord_updates else resolved.discord,
            "dingtalk": resolved.dingtalk.model_copy(update=dingtalk_updates) if dingtalk_updates else resolved.dingtalk,
            "wecom": resolved.wecom.model_copy(update=wecom_updates) if wecom_updates else resolved.wecom,
            "ntfy": resolved.ntfy.model_copy(update=ntfy_updates) if ntfy_updates else resolved.ntfy,
            "bark": resolved.bark.model_copy(update=bark_updates) if bark_updates else resolved.bark,
            "pushover": resolved.pushover.model_copy(update=pushover_updates) if pushover_updates else resolved.pushover,
            "imessage": resolved.imessage.model_copy(
                update={
                    **imsg_updates,
                    "chat_db_path": str(Path(resolved.imessage.chat_db_path).expanduser()),
                    "personal_workspace_dir": str(Path(resolved.imessage.personal_workspace_dir).expanduser())
                    if resolved.imessage.personal_workspace_dir
                    else "",
                }
            ),
            "whisper": resolved.whisper.model_copy(
                update={
                    "model_path": str(Path(resolved.whisper.model_path).expanduser())
                    if resolved.whisper.model_path
                    else "",
                }
            ),
            "external_adapters": [
                ext.model_copy(
                    update={
                        "command": [
                            str(Path(part).expanduser()) if part.startswith("~/") else part for part in ext.command
                        ]
                    }
                )
                for ext in resolved.external_adapters
            ],
            "external_notifiers": [
                ext.model_copy(
                    update={
                        "command": [
                            str(Path(part).expanduser()) if part.startswith("~/") else part for part in ext.command
                        ]
                    }
                )
                for ext in resolved.external_notifiers
            ],
            "channels": [
                _resolve_channel_entry(ch)
                for ch in resolved.channels
            ],
        }
    )


def _resolve_channel_entry(ch: ChannelConfig) -> ChannelConfig:
    ws_dir = str(Path(ch.workspace_dir).expanduser()) if ch.workspace_dir else ""
    p_file = str(Path(ch.persona_file).expanduser()) if ch.persona_file else ""
    p_text = ch.persona_text
    if p_file and not p_text.strip():
        p_path = Path(p_file)
        if p_path.is_file():
            p_text = p_path.read_text(encoding="utf-8", errors="replace").strip()
    return ch.model_copy(
        update={
            "workspace_dir": ws_dir,
            "persona_file": p_file,
            "persona_text": p_text,
        }
    )


def load_dotenv_file(path: Path | str) -> dict[str, str]:
    """Parse KEY=VALUE pairs from an optional .env file next to config.json."""
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        if key:
            parsed[key] = val
    return parsed


def load_raw_config(path: Path | str | None = None) -> WatchpostConfig:
    """Load config JSON without expanding env placeholders (safe for round-trip mutations)."""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Watchpost config not found: {cfg_path}")
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    return WatchpostConfig.model_validate(data)


def load_config(path: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> WatchpostConfig:
    """Load config JSON and resolve placeholders, optional .env values, env fallbacks, and paths."""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    raw = load_raw_config(cfg_path)
    if env is None:
        dotenv_vals = load_dotenv_file(cfg_path.parent / ".env")
        merged_env: Mapping[str, str] = {**dotenv_vals, **os.environ}
    else:
        merged_env = env
    return resolve_config(raw, env=merged_env)


def find_unresolved_placeholders(
    raw_cfg: WatchpostConfig,
    env: Mapping[str, str] | None = None,
    *,
    config_path: Path | str | None = None,
) -> list[tuple[str, str]]:
    """Return (section.field, ENV_NAME) for enabled sections with unset/empty ${VAR} placeholders."""
    if env is None:
        base_dir = Path(config_path).expanduser().parent if config_path else default_config_path().parent
        environ: Mapping[str, str] = {**load_dotenv_file(base_dir / ".env"), **os.environ}
    else:
        environ = env

    enabled_adapters = {c.adapter for c in raw_cfg.channels if c.enabled}
    sections: list[tuple[str, BaseModel, bool]] = [
        ("feishu", raw_cfg.feishu, bool(raw_cfg.feishu.enabled or "feishu" in enabled_adapters)),
        (
            "telegram",
            raw_cfg.telegram,
            bool(raw_cfg.telegram.enabled or raw_cfg.telegram.inbound_enabled or "telegram" in enabled_adapters),
        ),
        ("imessage", raw_cfg.imessage, bool(raw_cfg.imessage.enabled)),
        ("slack", raw_cfg.slack, bool(raw_cfg.slack.enabled or raw_cfg.slack.dm_enabled or "slack" in enabled_adapters)),
        ("discord", raw_cfg.discord, bool(raw_cfg.discord.enabled or raw_cfg.discord.dm_enabled or "discord" in enabled_adapters)),
        ("dingtalk", raw_cfg.dingtalk, bool(raw_cfg.dingtalk.enabled or "dingtalk" in enabled_adapters)),
        ("wecom", raw_cfg.wecom, bool(raw_cfg.wecom.enabled or raw_cfg.wecom.notify_enabled or "wecom" in enabled_adapters)),
        ("ntfy", raw_cfg.ntfy, bool(raw_cfg.ntfy.enabled)),
        ("bark", raw_cfg.bark, bool(raw_cfg.bark.enabled)),
        ("pushover", raw_cfg.pushover, bool(raw_cfg.pushover.enabled)),
    ]
    missing: list[tuple[str, str]] = []
    for sec_name, sec_model, active in sections:
        if not active:
            continue
        for fname, val in sec_model.model_dump(mode="python").items():
            if not isinstance(val, str):
                continue
            for m in _ENV_PLACEHOLDER_RE.finditer(val):
                env_name = m.group(1)
                if not str(environ.get(env_name, "")).strip():
                    missing.append((f"{sec_name}.{fname}", env_name))
    return missing


def save_config(cfg: WatchpostConfig, path: Path | str) -> Path:
    """Persist Watchpost config atomically with restrictive 0600 file permissions."""
    cfg_path = Path(path).expanduser()
    payload = json.dumps(cfg.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    return atomic_write_text(cfg_path, payload, mode=0o600)


def starter_template_config() -> WatchpostConfig:
    """Build a safe, placeholder-only starter config template."""
    return WatchpostConfig(
        feishu=FeishuConfig(
            enabled=False,
            auth_mode="tenant",
            app_id="${FEISHU_APP_ID}",
            app_secret="${FEISHU_APP_SECRET}",
            owner_open_id="${FEISHU_OWNER_OPEN_ID}",
        ),
        telegram=TelegramConfig(
            enabled=False,
            bot_token="${TELEGRAM_BOT_TOKEN}",
            owner_chat_id="${TELEGRAM_OWNER_CHAT_ID}",
        ),
        imessage=IMessageConfig(
            enabled=False,
            owner_handle="${WATCHPOST_IMESSAGE_HANDLE}",
        ),
    )


def init_config_file(path: Path | str | None = None, *, overwrite: bool = False) -> Path:
    """Create a starter Watchpost config file with safe env-var placeholders."""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    if cfg_path.exists() and not overwrite:
        raise FileExistsError(f"Config already exists: {cfg_path} (pass --force to overwrite)")
    return save_config(starter_template_config(), cfg_path)
