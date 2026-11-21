"""Pluggable agent backend implementations."""

from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.backends.mock import MockAgentBackend

__all__ = ["CLIAgentBackend", "MockAgentBackend"]
