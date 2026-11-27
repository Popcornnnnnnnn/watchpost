# Changelog

All notable changes to Watchpost (`watchpost-agent`) are documented in this file.

## v0.4.1 — Thread & Approval Lifecycle Hardening

- **Per-Thread & Workspace Concurrency Safety:** Serialize per-thread turns (`_get_thread_lock`) and enforce writer-preferring shared/exclusive workspace turn locking (`_AsyncRWLock`) so approved writable `"go"` turns never overlap with read-only follow-ups and inbound batches never steer placeholder `pending_<id>` refs.
- **Atomic Approval Claims & Crash Recovery:** Make `claim_pending_go` atomically transition `pending_go = 0` and `status = 'active'`, format owner-action payloads before claiming with post-claim error recovery in `handle_owner_action`, prevent placeholder refs from overwriting committed non-placeholder refs in `create_or_update_thread`, and recover orphaned `awaiting_go` rows in `recover_stale_active_threads` and `dismiss_all_pending_go`.
- **Formal Verification & Tests:** Add TLA+/TLC thread-lifecycle verification (`formal/WatchpostThread.tla` + `.agents/skills/verify-thread-lifecycle`) and regression coverage (`tests/test_formal_state_regressions.py`).

## v0.4.0 — Launch Readiness & Security Hardening

### Security & Agent Safety
- **Local Control API Auth & Hardening:** Automatically generate a `0o600` installation bearer token (`api.token` next to SQLite DB) required on all `/api/*` endpoints (except signed `/api/webhooks/*` and unauthenticated `/healthz`). Enforce loopback `Host` header allowlist (`403` on DNS-rebinding attempts), reject browser cross-origin `Origin` headers (`403`), and add restrictive security headers (`nosniff`, `DENY`, `default-src 'none'`).
- **Strict Approval Gates:** Return `409 Conflict` on `POST /api/threads/{id}/approve` when `pending_go` is `False`; reject plain `"go"` / `wp:go:<id>` / `#N go` approvals when no thread has `pending_go`; remove overly broad `"同意"` approval token so casual agreement never triggers code edits.
- **Per-Channel CLI Sandbox Enforcement:** Enforce `read-only` / `workspace-write` (`codex` `--sandbox` & `-c sandbox_mode`), `plan` / `acceptEdits` (`claude_code`), and `plan` / `auto_edit` (`gemini_cli`) across both initial and steering turns based on channel autonomy and verified `"go"` approval. Refuse automatic backup failover on writable `auto_fix` or `approved_go` turns to prevent duplicate edits.
- **Process-Tree Timeout Termination:** Launch subprocesses in their own session (`start_new_session=True` on POSIX) and terminate the entire process group via `kill_process_tree` on timeout.
- **Prompt Injection Fencing:** Fence untrusted external chat inputs, reply previews, and cross-channel recall inside randomized per-prompt `<untrusted_chat_data_{nonce}>` blocks and neutralize forged `[Watchpost — ...]` / `The owner explicitly APPROVED` markers.
- **Secret Redaction & File Permissions:** Scrub Telegram `bot<id>:<secret>`, HTTP `Bearer` tokens, and Slack `xox*-` tokens across logs (`RedactingLogFilter`), HTTP exceptions, and error messages; tighten SQLite DB/WAL/SHM (`0o600`), parent dirs (`0o700`), config (`0o600`), and optional `.env` (`0o600`).

### Packaging, Legal & Onboarding
- Add canonical Apache-2.0 `LICENSE`.
- Publish distribution metadata as `watchpost-agent` (`0.4.0`) keeping the `watchpost` CLI entry point, excluding `site/` and `uv.lock` from source distributions, and converting the bundled Telegram avatar to JPEG (`avatar.jpg`).
- Default setup, CLI flags (`--lang`), and engine config to English (`en`) with `slack` fallback channel recommendation, strict platform/notifier validation, sibling `.env` credential support, and read-only `watchpost doctor` checks (placeholder resolution, DB permissions, daemon lock/port conflict, per-channel workspaces, and `--live` Telegram `getWebhookInfo` conflict check).
- Add GitHub Actions CI matrix (`ubuntu-latest` + `macos-latest`, Python 3.11–3.13) plus `CONTRIBUTING.md`, `SECURITY.md`, and issue templates.

### Telegram UX & Daemon Reliability
- Nonce-verified private chat pairing in `watchpost telegram-setup` (`https://t.me/<bot>?start=<nonce>`), bearer-authenticated hot reload, and preserved `${VAR}` placeholders in `config.json`.
- Bot-token-scoped update cursor (`telegram_owner_update_offset:<hash>`) with deferred commit after owner action dispatch, exponential backoff on HTTP `409`/`429`/`5xx`/transport failures, and explicit handling of `/start`, unknown `/cmd` vs POSIX file paths, bare `#N` usage hints, `/list` channel + relative age + 24h index expiry, and clear guidance when `personal_workspace_dir` is unset.
- Isolated `webhook_port` listener (`create_webhook_app`) exposing only `/healthz` and signed webhook endpoints, `fcntl.flock` single-instance lock (`watchpost.lock`), periodic media pruning (`prune_old_media`), `asyncio.to_thread` Whisper transcription, bounded concurrency (`max_concurrent_turns` + per-workspace write locks), debouncer error surfacing, and `watchpost service {install,status,uninstall}` for `launchd` and `systemd --user`.

## v0.3.0 — Multi-Platform Adapters, Hot Reload & Bridge Showcase
- Native inbound & outbound support for Feishu/Lark, Slack, Discord, Telegram (shared `TelegramUpdateHub`), DingTalk, WeCom, macOS iMessage, ntfy, Bark, and Pushover.
- 2-bucket group context engine, Quiet Hours + Morning Briefing, cross-channel incident memory, `watchpost.sdk` external plugin protocol, interactive Web `sandbox-ui`, and zero-downtime `POST /api/reload`.
