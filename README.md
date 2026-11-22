# Watchpost

**Headless local background bridge for your coding agent — zero-copy-paste head-starts delivered to your phone (`Apache-2.0`, `v0.2.0`).**

Watchpost removes the manual copy-paste loop when urgent work or deliverables arrive in chat. It runs silently on your laptop, Mac mini, or bastion host, watches selected DMs and `@me` mentions (`Feishu` read-only with zero read receipts, plus offline simulation), applies your rules and autonomy gates, injects text, photos, and local **Whisper** voice transcripts directly into **your own coding agent** (`Claude Code`, `Codex`, `Gemini CLI`, `Antigravity`, or any custom CLI runner), and sends concise mobile bubbles + **1-tap-copy draft replies** strictly to *you* on your familiar phone chat (`Telegram` or macOS `iMessage`).

## Why Watchpost?

- **No Copy-Paste Bottleneck:** Start debugging or preparing a deliverable the instant a message lands — whether it is a 2-minute live incident or a 2-hour spec/report review.
- **No Heavy Platform UI:** No standalone web inbox or extra chat app to babysit. Install the bundled **Agent Skill** inside your existing coding agent and receive handoffs on your phone.
- **Bring Your Own Coding Agent:** Runs headless CLI agents against your private repos, local files, internal LAN tools, and read-only SSH — nothing sensitive leaves your perimeter to a third-party cloud SaaS.
- **Human-Only Handoff (Draft-Only Default):** External senders never talk directly to your AI. You review the short diagnosis or 70–80% draft on your phone, steer with follow-up replies, or approve `"go"` when ready.

## Key Capabilities (`v0.2.0`)

- **Read-Only Official Feishu Adapter (`adapters/feishu.py`):** Polls selected DMs and groups with **zero read-receipt leaks**, parses mentions/rich posts/files/images/audio, skips recalled (`deleted`) messages cleanly, and discovers chats via `watchpost channels list-remote`.
- **Familiar Phone Entrances (`notifiers/telegram.py`, `notifiers/imessage.py`):**
  - **Telegram Bot (Cross-platform):** Owner-locked `chat_id` verification, `"go"` approval, `/status`, `/list`, and reply-thread steering (`watchpost telegram-whoami` helps discover your `chat_id`).
  - **macOS iMessage:** Safe `argv` AppleScript delivery + read-only `~/Library/Messages/chat.db` polling with echo suppression, anti-backlog ROWID seeding, `attributedBody` fallback decoding, and swipe-reply binding.
- **Guided Setup via Installable Agent Skill (`install-skill`):** Install/remove `SKILL.md` directly into Claude Code, Codex, or Antigravity (`watchpost install-skill --target claude|codex|antigravity`).
- **2-Bucket Group Context + Burst Debouncing:** Separates Bucket-1 linked clues/media (recursive reply chains + overnight handoffs) from Bucket-2 text-only background chatter while coalescing multi-bubble bursts.
- **Offline Whisper ASR & 4-Mode Intent Triage:** Distinguishes quick status/estimate/doc questions (`Mode 1`), advice (`Mode 2`), and FYI (`Mode 3`) from deep investigations (`Mode 4`), enforcing `assistant_only`, `diagnose_only`, or `auto_fix` + `UNTRUSTED EXTERNAL CHAT INPUT` isolation.

## Quickstart (`v0.2.0`)

Requirements: Python `3.11+` and [`uv`](https://docs.astral.sh/uv/).

```bash
cd watchpost
uv sync --all-extras
```

### Option 1 — Guided Agent Skill Setup (Recommended)
Install the bundled skill into your agent harness (`claude`, `codex`, or `antigravity`), then ask your agent *"Set up Watchpost"*:
```bash
uv run watchpost install-skill --target claude
```

### Option 2 — Direct CLI Setup
1. **Create starter config (`~/.config/watchpost/config.json`, `0600`):**
   ```bash
   uv run watchpost init-config
   ```
2. **Set credentials via env vars (or edit `config.json`) & discover Telegram `chat_id`:**
   ```bash
   export TELEGRAM_BOT_TOKEN="..."
   uv run watchpost telegram-whoami
   ```
3. **Discover Feishu chats and enroll a channel:**
   ```bash
   export FEISHU_APP_ID="..."
   export FEISHU_APP_SECRET="..."
   export FEISHU_OWNER_OPEN_ID="ou_..."
   uv run watchpost channels list-remote --adapter feishu
   uv run watchpost channels add \
     --adapter feishu \
     --chat-id "oc_..." \
     --name "Support Group" \
     --chat-type group \
     --trigger mention_owner \
     --autonomy diagnose_only
   ```
4. **Verify health and run the headless daemon:**
   ```bash
   uv run watchpost doctor
   uv run watchpost run --once --seed
   uv run watchpost run
   ```

### Offline Demo, JSON Control API & Tests
```bash
# Run offline simulation scenario
uv run watchpost demo

# Start headless JSON control API (:8787, zero Web UI)
uv run watchpost serve --host 127.0.0.1 --port 8787 --db ./data/watchpost.db

# Run fast offline test suite
uv run pytest -v
```

## External Adapter & License Boundary

Watchpost core is **`Apache-2.0`** and ships only official-API adapters and phone entrances (`Feishu`, `Telegram`, macOS `iMessage`). Unofficial or copyleft desktop bridges—such as macOS local WeChat bridges like [`wechat-mcp`](https://github.com/Popcornnnnnnnn/wechat-mcp) (`GPL-3.0`)—remain separate out-of-process services integrated only over local HTTP (`:8300`) or CLI boundaries, never imported into the Watchpost Python package.

## Roadmap

- **Phase 1 — Headless Core Engine (`v0.1.0`, Done):** Protocol contracts, 5-table SQLite WAL store, noise/recall filters, 2-bucket context, debouncer, 4-mode triage, CJK/EN bubble splitter, Whisper ASR, generic `CLIAgentBackend`, offline simulator, and headless JSON control API.
- **Phase 2 — Real End-to-End Loop & Guided Skill (`v0.2.0`, Done):** Official read-only **Feishu** adapter, **Telegram** & macOS **iMessage** phone entrances, owner `"go"` / `/status` / `/list` / steer loop, continuous **`watchpost run`** daemon + **`doctor`** + **`channels`** CLI, and bundled **`watchpost` Agent Skill**.
- **Phase 3 — Breadth & Bastion Packaging (Planned):** **Slack** Socket Mode adapter, automated `Auto-Draft Persona` from chat history, optional `mode: digest` channel trigger for quiet groups, optional `wechat-mcp` (`:8300`) external plugin guide, and headless `Dockerfile` / `docker-compose.yml`.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system architecture.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
