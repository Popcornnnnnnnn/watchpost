"""Plugin Protocol contracts for Watchpost adapters, backends, notifiers, and media processors."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from watchpost.models import (
    AgentResult,
    ChannelConfig,
    MediaArtifact,
    NormalizedMessage,
    OwnerAction,
    ThreadRecord,
)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Ingests normalized messages and media from a team messaging platform."""

    name: str

    async def list_available_chats(self) -> list[dict[str, str]]:
        """Discover selectable DMs/groups for one-click onboarding."""
        ...

    async def fetch_recent(self, channel: ChannelConfig, *, limit: int = 50) -> list[NormalizedMessage]:
        """Fetch recent messages in chronological order."""
        ...

    async def fetch_by_ids(self, channel: ChannelConfig, message_ids: list[str]) -> list[NormalizedMessage]:
        """Fetch historical messages by ID to complete reply chains."""
        ...

    async def download_media(
        self,
        channel: ChannelConfig,
        message: NormalizedMessage,
        dest_dir: Path,
    ) -> list[MediaArtifact]:
        """Download or decode attachments for high-confidence Bucket-1 messages."""
        ...


@runtime_checkable
class AgentBackend(Protocol):
    """Executes AI triage and investigation inside a local/self-hosted agent runtime."""

    name: str

    async def start_thread(
        self,
        title: str,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        channel: ChannelConfig | None = None,
    ) -> tuple[str, AgentResult]:
        """Start a new agent thread and return (backend_thread_ref, result)."""
        ...

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel: ChannelConfig | None = None,
    ) -> AgentResult:
        """Steer an existing thread with follow-up chat context or owner guidance."""
        ...


@runtime_checkable
class NotifierAdapter(Protocol):
    """Pushes short phone-friendly summaries and draft alerts to the owner."""

    name: str

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
    ) -> list[str]:
        ...

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        ...

    async def poll_owner_actions(self) -> list[OwnerAction]:
        ...


@runtime_checkable
class MediaProcessor(Protocol):
    """Processes media locally (e.g., offline Whisper speech-to-text)."""

    def transcribe_audio(self, audio_path: Path, *, prompt: str = "", language: str = "zh") -> tuple[str, str]:
        """Return (transcript_text, error_message)."""
        ...
