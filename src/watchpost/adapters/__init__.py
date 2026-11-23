"""Channel adapters for Watchpost."""

from watchpost.adapters.dingtalk import DingTalkAdapter
from watchpost.adapters.discord import DiscordAdapter
from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.adapters.local_sandbox import LocalSandboxAdapter
from watchpost.adapters.simulator import SimulatorAdapter
from watchpost.adapters.slack import SlackAdapter
from watchpost.adapters.telegram import TelegramInboundAdapter, TelegramUpdateHub
from watchpost.adapters.wecom import WeComAdapter

__all__ = [
    "DingTalkAdapter",
    "DiscordAdapter",
    "ExternalCommandAdapter",
    "FeishuAdapter",
    "LocalSandboxAdapter",
    "SimulatorAdapter",
    "SlackAdapter",
    "TelegramInboundAdapter",
    "TelegramUpdateHub",
    "WeComAdapter",
]
