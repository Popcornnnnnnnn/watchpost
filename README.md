# Watchpost

**Self-hosted, open-source B2B Field-Ops & Engineering Copilot (`Apache-2.0`).**

Watchpost passively monitors selected engineering DMs and `@me` support groups, filters out small talk and recalled noise, reconstructs overnight multi-person group handoffs, transcribes voice notes locally via Whisper, triages intent *before* running code/SSH checks, delegates diagnosis to your chosen agent backend (`Claude Code`, `Codex`, `Gemini CLI`, `Antigravity`, or direct LLM/MCP), and delivers crisp mobile summaries + **1-tap-copy draft replies** — **draft-only by default**.

## Why Self-Hosted?

Field engineering teams never want to upload private firmware repos, production SSH keys, internal telemetry, or customer voice notes to a third-party SaaS. Watchpost runs inside your own laptop, bastion host, or private Docker network so your code, audio, and credentials never leave your perimeter.

## Key Capabilities

- **Passive, Zero-Read-Receipt Monitoring:** Polls selected DMs and `@me` group threads while skipping owner turns, recalled messages, stickers, `@all` broadcasts, and pure acknowledgements (`OK`, `收到`).
- **2-Bucket Group Context Engine:** Pulls high-confidence clues (recursive `reply_to` chains, same-sender bursts, and overnight `Engineer A @Support -> Support @Lead` handoffs) with media decoded, while keeping unrelated group chatter text-only.
- **Offline Whisper Voice Transcription:** Transcribes field voice notes locally with domain vocabulary hints.
- **Smart 4-Mode Intent Triage:** Distinguishes quick status/estimate questions (`Mode 1`), coordination/advice (`Mode 2`), and FYI updates (`Mode 3`) from actual bug investigations (`Mode 4`).
- **Per-Channel Personas & Autonomy Gates:** Configure `assistant_only`, `diagnose_only` (wait for explicit `"go"` approval before edits/restarts), or opt-in `auto_fix`.
- **Pluggable Runtime & SQLite WAL Store:** Clean `ChannelAdapter`, `AgentBackend`, and `NotifierAdapter` protocols backed by a single-file SQLite WAL database.
- **Web Inbox Dashboard:** Responsive local UI on `:8787` for 1-tap copy drafts, `"go"` approvals, inline steering, channel toggles, and telemetry.

## Quickstart (Phase 1 — Core Engine & Offline Simulator)

Requirements: Python `3.11+` and [`uv`](https://docs.astral.sh/uv/).

```bash
cd watchpost
uv sync --all-extras
```

1. **Run the offline Field-Ops Simulator CLI demo:**
   ```bash
   uv run watchpost demo
   ```
   Replays a field-ops scenario (recalled noise, small talk, overnight `Sam @Alex` photo handoff → morning `Alex @Lead` diagnosis, and a rollout status Q&A) and prints short bubbles + 1-tap-copy drafts.

2. **Start the Web Inbox Dashboard:**
   ```bash
   uv run watchpost serve --host 127.0.0.1 --port 8787 --db ./data/watchpost.db
   ```
   Open <http://127.0.0.1:8787> and click **Run Field-Ops Demo** to test live triage, 1-tap draft copy, `"go"` approval, and follow-up steering.

3. **Run tests:**
   ```bash
   uv run pytest -v
   ```

## Phase 1 Status & Completion Criteria

- **Status:** Phase 1 Complete & Verified (`v0.1.0`)
- **Delivered in Phase 1:**
  - Core models & plugin `Protocol` contracts (`src/watchpost/ports.py`, `models.py`)
  - 5-table SQLite WAL state store (`src/watchpost/store.py`)
  - Noise/recall filters, 2-bucket group context engine, async burst debouncer, 4-mode prompt builder, CJK/EN short bubble splitter (`src/watchpost/`)
  - Pluggable backends (`MockAgentBackend`, `CLIAgentBackend`) & offline `SimulatorAdapter`
  - FastAPI REST API & zero-dependency responsive Web Inbox Dashboard (`src/watchpost/server.py`)
- **Next Steps (Phase 2):**
  - Official **Feishu (Lark)** & **Slack** `ChannelAdapter` implementations
  - `Dockerfile` + `docker-compose.yml` for 1-command server deployment
  - Channel onboarding search picker & `"Auto-Draft Persona"` endpoint
  - Direct LLM + MCP backend (`Phase 2/3`) & **WeCom** + mobile push notifiers (`Phase 3`)

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system architecture.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
