"""Channel adapters for Watchpost."""

from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.adapters.feishu import FeishuAdapter
from watchpost.adapters.local_sandbox import LocalSandboxAdapter
from watchpost.adapters.simulator import SimulatorAdapter

__all__ = ["ExternalCommandAdapter", "FeishuAdapter", "LocalSandboxAdapter", "SimulatorAdapter"]
