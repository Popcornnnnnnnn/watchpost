"""Domain models and enums for Watchpost."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AutonomyMode(str, Enum):
    assistant_only = "assistant_only"
    diagnose_only = "diagnose_only"
    auto_fix = "auto_fix"


class ChatType(str, Enum):
    p2p = "p2p"
    group = "group"


class TriggerMode(str, Enum):
    all = "all"
    mention_owner = "mention_owner"


class TriageMode(str, Enum):
    mode_1_qa = "mode_1_qa"
    mode_2_advice = "mode_2_advice"
    mode_3_fyi = "mode_3_fyi"
    mode_4_bug = "mode_4_bug"


class ThreadStatus(str, Enum):
    active = "active"
    awaiting_go = "awaiting_go"
    completed = "completed"
    error = "error"


class MediaRef(BaseModel):
    kind: str  # "image" | "audio" | "file" | "video_cover"
    key: str
    ext: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class MediaArtifact(BaseModel):
    kind: str  # "image" | "audio" | "file"
    key: str = ""
    path: str = ""
    transcript: str = ""
    error: str = ""


class Mention(BaseModel):
    id: str
    name: str = ""
    key: str = ""


class NormalizedMessage(BaseModel):
    message_id: str
    channel_id: str = ""
    position: int = 0
    create_ms: int = 0
    create_time: str = ""
    sender_id: str = ""
    sender_name: str = "Unknown"
    sender_type: str = "user"
    is_owner: bool = False
    is_bot: bool = False
    deleted: bool = False
    msg_type: str = "text"  # "text" | "post" | "image" | "audio" | "file" | "sticker" | "system"
    raw_content: str = ""
    clean_text: str = ""
    reply_to_id: str = ""
    reply_preview: str = ""
    mentions: list[Mention] = Field(default_factory=list)
    mentions_owner: bool = False
    mentions_all: bool = False
    media_refs: list[MediaRef] = Field(default_factory=list)
    artifacts: list[MediaArtifact] = Field(default_factory=list)


class ChannelConfig(BaseModel):
    id: str
    adapter: str
    external_chat_id: str
    name: str
    chat_type: ChatType = ChatType.p2p
    trigger_mode: TriggerMode = TriggerMode.all
    autonomy_mode: AutonomyMode = AutonomyMode.diagnose_only
    ignore_bots: bool = True
    persona_file: str = ""
    persona_text: str = ""
    whisper_prompt: str = ""
    same_sender_window_s: int = 300
    handoff_window_s: int = 43200
    background_window_s: int = 64800
    group_sender_ttl_s: int = 900
    group_reply_ttl_s: int = 86400
    session_ttl_s: int = 3600
    max_catchup_age_s: int = 7200
    cursor_pos: int = 0
    cursor_ms: int = 0
    workspace_dir: str = ""
    enabled: bool = True


class GroupContext(BaseModel):
    direct_messages: list[NormalizedMessage] = Field(default_factory=list)
    background_messages: list[NormalizedMessage] = Field(default_factory=list)
    reply_chain_ids: list[str] = Field(default_factory=list)
    link_reasons: dict[str, str] = Field(default_factory=dict)
    all_lookup: dict[str, NormalizedMessage] = Field(default_factory=dict)


class AgentResult(BaseModel):
    raw_text: str
    summary_bubbles: list[str] = Field(default_factory=list)
    suggested_reply: str = ""
    needs_attention: str = ""
    triage_mode: TriageMode = TriageMode.mode_1_qa
    error: str = ""


class ThreadRecord(BaseModel):
    id: str
    channel_id: str
    backend: str
    backend_thread_ref: str
    sender_id: str = ""
    sender_name: str = ""
    title: str = ""
    status: ThreadStatus = ThreadStatus.active
    root_msg_id: str = ""
    message_ids: list[str] = Field(default_factory=list)
    triage_mode: TriageMode = TriageMode.mode_1_qa
    pending_go: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0


class ThreadEvent(BaseModel):
    id: int = 0
    thread_id: str
    kind: str  # "inbound" | "agent_reply" | "owner_steer" | "owner_approve" | "error"
    summary_bubbles: list[str] = Field(default_factory=list)
    suggested_reply: str = ""
    needs_attention: str = ""
    raw_text: str = ""
    draft_copied: bool = False
    created_at: float = 0.0


class OwnerAction(BaseModel):
    notifier: str
    kind: str  # "steer" | "approve_go" | "status" | "list" | "help" | "new_personal" | "reset_personal"
    thread_id: str = ""
    text: str = ""
    reply_token: str = ""
    list_index: int = 0
    attachments: list[dict[str, str]] = Field(default_factory=list)


class EngineConfig(BaseModel):
    workspace_dir: Path = Field(default_factory=lambda: Path("."))
    language: str = "zh"
    debounce_s: float = 25.0
    max_debounce_s: float = 60.0
    media_retention_days: int = 14
    personal_workspace_dir: Path | None = None
    personal_session_ttl_s: int = 3600
    ack_on_receive: bool = True
