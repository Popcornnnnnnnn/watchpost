---
name: watchpost
description: Set up, manage, and cleanly remove Watchpost — the headless local background bridge that sends zero-copy-paste coding-agent head-starts to your phone. Use whenever the user says "set up Watchpost", wants incoming Feishu/Slack messages triaged automatically, wants to enroll/remove channels or personas, or wants to uninstall Watchpost.
---

# Watchpost Agent Skill

Watchpost runs silently in the background on the user's machine. It watches selected work chats, runs your local coding agent (`Claude Code`, `Codex`, `Gemini CLI`, `Antigravity`, or any custom CLI runner) against your workspace with your rules, and sends short status updates + **1-tap-copy reply drafts** strictly to your phone (**never auto-replying to external senders**).

---

## 1. Guided Setup Workflow

Follow these steps in order when a user asks to set up Watchpost. Ask concise questions instead of dumping raw config JSON on them.

### Step 1 — Check Environment & Initialize Config
Run:
```bash
watchpost doctor --json
```
If no config exists yet, create the starter file (`0600` permissions, `${ENV_VAR}` placeholders):
```bash
watchpost init-config
```
Default config path: `~/.config/watchpost/config.json`. Keep raw secrets in environment variables (or `0600` config) and never echo them back in summaries.

### Step 2 — Set Up the Familiar Phone Entrance (Pick 1)
1. **Telegram Bot (Recommended cross-platform):**
   - Ask the user to create a bot in Telegram with `@BotFather` (`/newbot`) and send `"hi"` to their new bot once.
   - Store the bot token (`TELEGRAM_BOT_TOKEN`), then run:
     ```bash
     watchpost telegram-whoami
     ```
   - Save `telegram.enabled = true`, `telegram.bot_token`, and discovered `telegram.owner_chat_id` into `~/.config/watchpost/config.json`.
2. **macOS iMessage (macOS only):**
   - Set `imessage.enabled = true` and `imessage.owner_handle` to the user's iMessage phone number (`+...`) or Apple ID email.
   - Remind the user that polling replies requires **Full Disk Access** (`~/Library/Messages/chat.db`) for the terminal/Python running `watchpost run`.

### Step 3 — Connect Read-Only Work Chat (`Feishu` / Lark)
- Guide the user to configure a read-only Feishu app with **no send permissions** (zero read-receipt leaks):
  - Scopes: `im:message.group_msg:readonly`, `im:message.p2p_msg:readonly`, `im:chat:readonly`, `im:resource`
- Save `feishu.enabled = true`, `feishu.app_id`, `feishu.app_secret` (or `FEISHU_APP_ID` / `FEISHU_APP_SECRET`), and `feishu.owner_open_id`.
- Discover candidate chats without copying IDs by hand:
  ```bash
  watchpost channels list-remote --adapter feishu
  ```

### Step 4 — Enroll 1–2 High-Signal Channels & Draft Rules
For each channel the user selects, ask 2 quick questions:
1. Should it watch **all messages** (best for 1-on-1 DMs) or **only `@me` mentions** (best for noisy groups)?
2. Which **autonomy gate** applies?
   - `assistant_only` — Q&A, doc checks, and drafting only. Never touches code or SSH.
   - `diagnose_only` *(Recommended default)* — Read-only repo/log/SSH checks + root-cause diagnosis + reply draft. Waits for explicit `"go"` from phone before making changes.
   - `auto_fix` — Can apply safe fixes/PRs directly within channel safety rules.

Draft a concise persona describing the sender/group, target repo or logs, domain vocabulary for Whisper, and preferred reply language, then enroll it:
```bash
watchpost channels add \
  --adapter feishu \
  --chat-id "<chat_id>" \
  --name "<friendly name>" \
  --chat-type group \
  --trigger mention_owner \
  --autonomy diagnose_only \
  --persona "Support escalation group for backend rollout checks."
```

### Step 5 — Verify & Start the Headless Daemon
1. Verify all health checks:
   ```bash
   watchpost doctor
   ```
2. Run a safe single poll cycle first:
   ```bash
   watchpost run --once --seed
   ```
3. Start continuous background watching:
   ```bash
   watchpost run
   ```

---

## 2. Day-to-Day Phone & CLI Controls

From the paired phone chat (`Telegram` or `iMessage`), the owner can reply directly:
- **`go` / `执行` / `approve`** — Approves a pending `diagnose_only` fix proposal.
- **`status` / `/status` / `状态`** — Checks monitored channels, threads, and pending `"go"` approvals.
- **`list` / `/list` / `列表`** — Lists recent Watchpost threads.
- **Any other text** (especially swipe-replying to a specific bubble) — Steers that investigating agent thread with extra context.

From CLI:
- `watchpost channels list` / `watchpost channels remove --id <id>`
- `watchpost doctor`

---

## 3. Clean Uninstall

If the user wants to remove Watchpost completely:
1. Stop any running `watchpost run` process.
2. Remove local state and config only after confirming with the user:
   - `~/.config/watchpost/`
   - `~/.local/share/watchpost/`
3. Uninstall this Agent Skill cleanly:
   ```bash
   watchpost install-skill --target claude --uninstall
   # or --target codex / --target antigravity
   ```

---

## 4. License & External Plugin Boundary

Watchpost core is **Apache-2.0**. Never import copyleft or unofficial desktop bridges (such as `wechat-mcp` under `GPL-3.0-or-later`) directly into Watchpost Python modules. Run external companions separately (for example over `wechat-mcp`'s local HTTP service on `127.0.0.1:8300`) as user-managed external plugins.
