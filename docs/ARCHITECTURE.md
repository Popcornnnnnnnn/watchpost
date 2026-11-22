# Watchpost Architecture

Watchpost separates **ingestion**, **context/triage**, **coding-agent execution**, and **phone delivery** behind narrow Python `Protocol` contracts so every channel, agent CLI harness, and phone entrance stays swappable without any Web UI or standalone chat frontend.

## System Topology

```mermaid
flowchart LR
  subgraph IN["Channel Adapters"]
    A1["Feishu Official API (Core Read-Only)"]
    A2["Slack Socket Mode (Planned)"]
    A3["Offline Simulator (Core)"]
    A4["External Plugins via HTTP/CLI\n(e.g. wechat-mcp GPL-3.0 on :8300)"]
  end

  subgraph CORE["Watchpost Headless Engine (Python + SQLite WAL)"]
    B1["Noise Filters & Burst Debouncer"]
    B2["2-Bucket Group Context Engine"]
    B3["Local Media & Whisper ASR"]
    B4["4-Mode Triage & Personas"]
    B5["SQLite WAL Store & WatchpostDaemon"]
  end

  subgraph AGENT["Bring Your Own Coding Agent"]
    C1["CLIAgentBackend\n(Claude Code / Codex / Gemini / AGY)"]
    C2["MockAgentBackend (Tests & Demo)"]
  end

  subgraph PHONE["Familiar Phone Entrance (Draft-Only Default)"]
    D1["Telegram Bot"]
    D2["macOS iMessage"]
  end

  A1 --> B1
  A2 -.-> B1
  A3 --> B1
  A4 -.-> B1
  B1 --> B2
  B2 --> B3
  B3 --> B4
  B4 <--> B5
  B4 <--> C1
  B4 <--> C2
  B5 --> D1
  B5 --> D2
  D1 -. "\"go\" / /status / steer" .-> B5
  D2 -. "\"go\" / /status / steer" .-> B5
```

## Module Map (`v0.2.0`)

- `src/watchpost/config.py`: Pydantic v2 configuration schema (`WatchpostConfig`, `FeishuConfig`, `TelegramConfig`, `IMessageConfig`, `BackendConfig`, `WhisperConfig`), `${ENV_VAR}` expansion, env-var fallbacks, and `0600` JSON persistence.
- `src/watchpost/models.py`: Pydantic v2 domain models (`ChannelConfig`, `NormalizedMessage`, `GroupContext`, `ThreadRecord`, `ThreadEvent`, `AgentResult`, `OwnerAction`).
- `src/watchpost/ports.py`: `Protocol` interfaces (`ChannelAdapter`, `AgentBackend`, `NotifierAdapter`, `MediaProcessor`).
- `src/watchpost/store.py`: Atomic **SQLite WAL** persistence across 5 normalized tables (`channels`, `messages`, `threads`, `thread_events`, `outbound_bindings`).
- `src/watchpost/adapters/feishu.py`: Read-only official Feishu Open API adapter (`FeishuAdapter`) supporting tenant/user auth, paginated chat discovery, rich-post/mention/media normalization, recall skipping, and path-safe resource downloads.
- `src/watchpost/notifiers/telegram.py` & `imessage.py`: Owner-authenticated phone entrances (`TelegramNotifier` via Bot API and `IMessageNotifier` via `argv` `osascript` + read-only `chat.db` polling with echo suppression and `attributedBody` decoding).
- `src/watchpost/filters.py`, `context.py`, `debouncer.py`: Noise/recall filtering, 2-bucket group context assembly, and async burst debouncing.
- `src/watchpost/prompts.py` & `bubbles.py`: Smart 4-Mode Intent Triage, autonomy gates, `UNTRUSTED EXTERNAL CHAT INPUT` isolation, Markdown stripping, and CJK/EN short sentence splitting.
- `src/watchpost/media.py`: Local Whisper CLI processor (`ffmpeg` + `whisper-cli`) and media retention pruning.
- `src/watchpost/backends/cli_agent.py`: Generic async subprocess wrapper (`CLIAgentBackend`) supporting `claude_code`, `codex`, `gemini_cli`, `antigravity`, or custom headless CLI commands.
- `src/watchpost/engine.py` & `daemon.py`: Orchestrator (`WatchpostEngine`), owner-action dispatcher (`handle_owner_action`), continuous daemon (`WatchpostDaemon`), and environment checker (`run_doctor`).
- `src/watchpost/skills/watchpost/SKILL.md` & `skill_installer.py`: Bundled cross-harness Agent Skill for guided setup, channel onboarding, and clean uninstall (`watchpost install-skill`).
- `src/watchpost/server.py`: Headless FastAPI JSON control API (`127.0.0.1:8787`, zero Web UI).

## Security & Licensing Boundaries

- **Prompt Injection Defense:** Incoming messages are wrapped explicitly as `UNTRUSTED EXTERNAL CHAT INPUT` and restricted strictly from issuing outbound channel sends.
- **Draft-Only By Default:** External messaging write tokens are never required; suggested drafts stay isolated until the owner copies or approves them.
- **Zero Egress By Architecture:** Media, voice audio, repos, and local logs remain on the owner's machine.
- **License Isolation (`Apache-2.0` Core vs. External Copyleft Bridges):** Watchpost core (`Apache-2.0`) only integrates official APIs and OS/phone entrances directly. External bridges under copyleft terms (such as `wechat-mcp`, `GPL-3.0`) run strictly out-of-process over local REST/CLI boundaries and are never imported into Watchpost's codebase.
