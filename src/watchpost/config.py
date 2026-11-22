"""Configuration loader, environment placeholder expansion, and 0600 persistence for Watchpost."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from watchpost.models import ChannelConfig

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
    base_url: str = "https://api.telegram.org"
    poll_timeout_s: int = 0


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


class WatchpostConfig(BaseModel):
    db_path: str = DEFAULT_DB_PATH
    media_dir: str = DEFAULT_MEDIA_DIR
    workspace_dir: str = "."
    language: str = "zh"
    poll_interval_s: float = 10.0
    owner_poll_interval_s: float = 3.0
    debounce_s: float = 25.0
    max_debounce_s: float = 60.0
    api_port: int = 0
    backend: BackendConfig = Field(default_factory=BackendConfig)
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    imessage: IMessageConfig = Field(default_factory=IMessageConfig)
    external_adapters: list[ExternalAdapterConfig] = Field(default_factory=list)
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


def resolve_config(cfg: WatchpostConfig, *, env: Mapping[str, str] | None = None) -> WatchpostConfig:
    """Expand `${ENV_VAR}` placeholders, apply standard env fallbacks, and expand user paths."""
    environ = env if env is not None else os.environ
    expanded = expand_env_placeholders(cfg.model_dump(mode="python"), environ)
    resolved = WatchpostConfig.model_validate(expanded)

    feishu_updates: dict[str, Any] = {}
    if not resolved.feishu.app_id and environ.get("FEISHU_APP_ID"):
        feishu_updates["app_id"] = environ["FEISHU_APP_ID"]
    if not resolved.feishu.app_secret and environ.get("FEISHU_APP_SECRET"):
        feishu_updates["app_secret"] = environ["FEISHU_APP_SECRET"]
    if not resolved.feishu.user_access_token and environ.get("FEISHU_USER_ACCESS_TOKEN"):
        feishu_updates["user_access_token"] = environ["FEISHU_USER_ACCESS_TOKEN"]
    if not resolved.feishu.owner_open_id and environ.get("FEISHU_OWNER_OPEN_ID"):
        feishu_updates["owner_open_id"] = environ["FEISHU_OWNER_OPEN_ID"]

    tg_updates: dict[str, Any] = {}
    if not resolved.telegram.bot_token and environ.get("TELEGRAM_BOT_TOKEN"):
        tg_updates["bot_token"] = environ["TELEGRAM_BOT_TOKEN"]
    if not resolved.telegram.owner_chat_id and environ.get("TELEGRAM_OWNER_CHAT_ID"):
        tg_updates["owner_chat_id"] = environ["TELEGRAM_OWNER_CHAT_ID"]

    imsg_updates: dict[str, Any] = {}
    if not resolved.imessage.owner_handle and environ.get("WATCHPOST_IMESSAGE_HANDLE"):
        imsg_updates["owner_handle"] = environ["WATCHPOST_IMESSAGE_HANDLE"]

    return resolved.model_copy(
        update={
            "db_path": str(Path(resolved.db_path).expanduser()),
            "media_dir": str(Path(resolved.media_dir).expanduser()),
            "workspace_dir": str(Path(resolved.workspace_dir).expanduser()),
            "feishu": resolved.feishu.model_copy(update=feishu_updates) if feishu_updates else resolved.feishu,
            "telegram": resolved.telegram.model_copy(update=tg_updates) if tg_updates else resolved.telegram,
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


def load_raw_config(path: Path | str | None = None) -> WatchpostConfig:
    """Load config JSON without expanding env placeholders (safe for round-trip mutations)."""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Watchpost config not found: {cfg_path}")
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    return WatchpostConfig.model_validate(data)


def load_config(path: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> WatchpostConfig:
    """Load config JSON and resolve placeholders, env fallbacks, and paths."""
    raw = load_raw_config(path)
    return resolve_config(raw, env=env)


def save_config(cfg: WatchpostConfig, path: Path | str) -> Path:
    """Persist Watchpost config cleanly with restrictive 0600 file permissions."""
    cfg_path = Path(path).expanduser()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    cfg_path.write_text(payload, encoding="utf-8")
    os.chmod(cfg_path, 0o600)
    return cfg_path


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
