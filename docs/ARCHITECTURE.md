# Watchpost Architecture

Watchpost separates **ingestion**, **context/triage**, **coding-agent execution**, and **phone delivery** behind narrow Python `Protocol` contracts so every channel, agent CLI harness, and phone entrance stays swappable without any Web UI or standalone chat frontend.

## System Topology

```mermaid
flowchart LR
  subgraph IN["Channel Adapters"]
    A1["Feishu Official API (Core)"]
    A2["Slack Socket Mode (Core)"]
    A3["Offline Simulator (Core)"]
    A4["External Plugins via HTTP/CLI\n(e.g. wechat-mcp GPL-3.0 on :8300)"]
  end

  subgraph CORE["Watchpost Headless Engine (Python + SQLite WAL)"]
    B1["Noise Filters & Burst Debouncer"]
    B2["2-Bucket Group Context Engine"]
    B3["Local Media & Whisper ASR"]
    B4["4-Mode Intent Triage & Personas"]
    B5["SQLite WAL Store & JSON Control API (:8787)"]
  end

  subgraph AGENT["Bring Your Own Coding Agent"]
    C1["CLIAgentBackend\n(Claude Code / Codex / Gemini / AGY)"]
    C2["MockAgentBackend (Tests & Demo)"]
  end

  subgraph PHONE["Familiar Phone Entrance (Draft-Only Default)"]
    D1["Telegram Bot"]
    D2["macOS iMessage"]
    D3["Feishu Self-DM"]
  end

  A1 --> B1
  A2 --> B1
  A3 --> B1
  A4 --> B1
  B1 --> B2
  B2 --> B3
  B3 --> B4
  B4 <--> B5
  B4 <--> C1
  B4 <--> C2
  B5 --> D1
  B5 --> D2
  B5 --> D3
  D1 -. "\"go\" / follow-up steer" .-> B5
  D2 -. "\"go\" / follow-up steer" .-> B5
  D3 -. "\"go\" / follow-up steer" .-> B5
```

## Module Map

- `src/watchpost/models.py`: Pydantic v2 domain models (`ChannelConfig`, `NormalizedMessage`, `GroupContext`, `ThreadRecord`, `ThreadEvent`, `AgentResult`, `OwnerAction`).
- `src/watchpost/ports.py`: `Protocol` interfaces (`ChannelAdapter`, `AgentBackend`, `NotifierAdapter`, `MediaProcessor`).
- `src/watchpost/store.py`: Atomic **SQLite WAL** persistence across 5 normalized tables (`channels`, `messages`, `threads`, `thread_events`, `outbound_bindings`).
- `src/watchpost/filters.py`: Trigger & noise suppression (`should_trigger`, `is_small_talk_only`), ignoring owner turns, system/call notices, recalled (`deleted`) events, `@all`, and isolated acks/stickers.
- `src/watchpost/context.py`: 2-bucket `@me` group context assembly (`assemble_group_context`) and sender/reply-chain session matching (`match_group_thread`).
- `src/watchpost/debouncer.py`: Keyed async `BurstDebouncer` coalescing screenshot + voice + text bursts up to `max_debounce_s`.
- `src/watchpost/prompts.py`: Smart 4-Mode Intent Triage + autonomy gate (`assistant_only` / `diagnose_only` / `auto_fix`) + `UNTRUSTED EXTERNAL CHAT INPUT` framing.
- `src/watchpost/bubbles.py`: Markdown stripper, bilingual heading extractor (`总结`/`Summary`, `建议回复`/`Suggested Reply`, `需要注意`/`Needs Attention`), and CJK/EN short sentence bubble splitter.
- `src/watchpost/media.py`: Local Whisper CLI processor (`ffmpeg` + `whisper-cli`) and media retention pruning.
- `src/watchpost/backends/cli_agent.py`: Generic async subprocess wrapper (`CLIAgentBackend`) supporting headless CLI harnesses (`claude_code`, `codex`, `gemini_cli`, `antigravity`, or custom start/steer commands).
- `src/watchpost/engine.py`: End-to-end orchestrator (`WatchpostEngine`).
- `src/watchpost/server.py`: Headless FastAPI JSON control API (`127.0.0.1:8787`, no Web UI), managed locally via an installable Agent Skill or CLI/API calls.

## Security & Licensing Boundaries

- **Prompt Injection Defense:** Incoming messages are wrapped explicitly as `UNTRUSTED EXTERNAL CHAT INPUT` and restricted strictly from issuing outbound channel sends.
- **Draft-Only By Default:** External messaging write tokens are never required; suggested drafts stay isolated until the owner copies or approves them.
- **Zero Egress By Architecture:** Media, voice audio, repos, and local logs remain on the owner's machine.
- **License Isolation (`Apache-2.0` Core vs. External Copyleft Bridges):** Watchpost core (`Apache-2.0`) only integrates official APIs and OS/phone entrances directly. External bridges under copyleft terms (such as `wechat-mcp`, `GPL-3.0`) run strictly out-of-process over local REST/CLI boundaries and are never imported into Watchpost's codebase.
