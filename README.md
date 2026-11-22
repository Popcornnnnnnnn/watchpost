# Watchpost

**Headless local background bridge for your coding agent — zero-copy-paste head-starts delivered to your phone (`Apache-2.0`).**

Watchpost removes the manual copy-paste loop when urgent work or deliverables arrive in chat. It runs invisibly on your laptop, Mac mini, or bastion host, watches selected DMs and `@me` mentions (`Feishu`, `Slack`, etc.) without leaking read receipts, applies your rules and autonomy gates, injects text, photos, and local **Whisper** voice transcripts directly into **your own coding agent** (`Claude Code`, `Codex`, `Gemini CLI`, `Antigravity`, or any custom CLI runner), and sends concise mobile bubbles + **1-tap-copy draft replies** only to *you* on your familiar phone chat (`Telegram`, macOS `iMessage`, or `Feishu self-DM`).

## Why Watchpost?

- **No Copy-Paste Bottleneck:** Start debugging or preparing a deliverable the instant a message lands — whether it is a 2-minute live incident or a 2-hour spec/report review.
- **No Heavy Platform UI:** No standalone web inbox or extra chat app to babysit. Manage Watchpost through an installable **Agent Skill** inside your existing coding agent and receive handoffs on your phone.
- **Bring Your Own Coding Agent:** Runs headless CLI agents against your private repos, local files, internal LAN tools, and read-only SSH — nothing sensitive leaves your perimeter to a third-party cloud SaaS.
- **Human-Only Handoff (Draft-Only Default):** External senders never talk directly to your AI. You review the short diagnosis or 70–80% draft on your phone and copy-paste the reply yourself.

## Inbound Work Modes

1. **Live Firefighting (Minutes):** Field engineer or on-call alert arrives with screenshots or voice clips → immediate local diagnosis, incremental follow-up steering, and a paste-ready reply draft.
2. **Deliverable Head-Start (Hours):** A teammate or manager drops specs, PDFs, or logs for an afternoon check → the same event-triggered engine starts immediately so a fact-grounded draft is waiting before you even open your laptop.
3. **Quiet Tracking & Digests (Planned Config Option):** Silent action-item extraction or periodic `mode: digest` summaries for low-urgency groups, kept secondary to real-time head-starts.

## Key Capabilities (Phase 1 Core)

- **Passive Noise & Recall Filtering:** Skips owner turns, system/call notices, recalled (`deleted`) messages, pure acknowledgements/stickers (`OK`, `收到`), and `@all` broadcasts unless tied to an active thread.
- **2-Bucket Group Context Engine:** Pulls high-confidence clues (recursive `reply_to` chains, same-sender bursts, and overnight `Engineer A @Support -> Support @Lead` handoffs) with media decoded, while keeping unrelated group chatter text-only.
- **Offline Whisper Voice Transcription:** Transcribes field voice notes locally via `ffmpeg` + `whisper-cli` (`ggml` models) with per-channel domain vocabulary hints.
- **Smart 4-Mode Intent Triage:** Distinguishes quick status/estimate/doc questions (`Mode 1`), advice/coordination (`Mode 2`), and FYI updates (`Mode 3`) from deep investigations (`Mode 4`).
- **Per-Channel Personas & Autonomy Gates:** Configure `assistant_only`, `diagnose_only` (wait for explicit `"go"` approval before edits or restarts), or opt-in `auto_fix`, with `UNTRUSTED EXTERNAL CHAT INPUT` boundaries on every prompt.
- **Headless Daemon & SQLite WAL Store:** Clean `ChannelAdapter`, `AgentBackend`, `NotifierAdapter`, and `MediaProcessor` protocols backed by a single-file SQLite WAL database and a pure-JSON local control API on `:8787` (zero Web UI).

## Quickstart (Phase 1 — Core Engine & Offline Simulator)

Requirements: Python `3.11+` and [`uv`](https://docs.astral.sh/uv/).

```bash
cd watchpost
uv sync --all-extras
```

1. **Run the offline simulation CLI demo:**
   ```bash
   uv run watchpost demo
   ```
   Replays a realistic work scenario (recalled noise, small talk, overnight `Sam @Alex` photo handoff → morning `Alex @Lead` diagnosis, and a rollout status Q&A) and prints mobile bubbles + 1-tap-copy drafts.

2. **Start the headless local JSON control API (`:8787`):**
   ```bash
   uv run watchpost serve --host 127.0.0.1 --port 8787 --db ./data/watchpost.db
   ```
   Test endpoints with `curl` (e.g., `GET /`, `POST /api/simulate/run`, `GET /api/threads`, `POST /api/threads/{id}/steer`, `POST /api/threads/{id}/approve`).

3. **Run tests:**
   ```bash
   uv run pytest -v
   ```

## External Adapter & License Boundary

Watchpost core is **`Apache-2.0`** and ships only official-API adapters and phone notifiers (`Feishu`, `Slack`, `Telegram`, macOS `iMessage`). Unofficial or copyleft desktop bridges—such as macOS local WeChat bridges like `wechat-mcp` (`GPL-3.0`)—remain separate out-of-process services integrated only over local HTTP (`:8300`) or CLI boundaries, never imported into the Watchpost Python package.

## Roadmap

- **Phase 1 — Headless Core Engine (`v0.1.0`, Done):** Protocol contracts, 5-table SQLite WAL store, noise/recall filters, 2-bucket group context engine, async burst debouncer, 4-mode intent triage, CJK/EN short bubble splitter, local Whisper runner, generic `CLIAgentBackend`, offline `SimulatorAdapter`, and headless FastAPI JSON control API (`src/watchpost/server.py`).
- **Phase 2 — Guided Skill + Real Loop (Next, post-dogfooding):** Official **Feishu** adapter (zero read receipts), **Telegram** & macOS **iMessage** phone notifiers, and installable **`watchpost` Agent Skill** (conversational onboarding, phone pairing, persona drafting, and clean removal).
- **Phase 3 — Breadth & Packaging:** **Slack** Socket Mode adapter, `Auto-Draft Persona` from history, optional `mode: digest` channel trigger, optional `wechat-mcp` (`:8300`) external plugin guide, and headless `Dockerfile` / `docker-compose.yml`.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system architecture.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
