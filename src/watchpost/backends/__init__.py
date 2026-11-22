"""Pluggable agent backend implementations."""

from watchpost.backends.antigravity import AntigravityBackend
from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.backends.fallback import FallbackBackend
from watchpost.backends.mock import MockAgentBackend

__all__ = ["AntigravityBackend", "CLIAgentBackend", "FallbackBackend", "MockAgentBackend"]
