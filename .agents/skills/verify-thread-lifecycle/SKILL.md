---
name: verify-thread-lifecycle
description: Formally verify Watchpost thread lifecycle, pending_go approval gates, workspace turn locking, and crash recovery invariants with TLA+ and TLC. Run during any major update or when changing src/watchpost/engine.py, src/watchpost/store.py, or src/watchpost/backends/.
---

# Verify Watchpost Thread Lifecycle

Checks the TLA+ model in `formal/WatchpostThread.tla` against the invariants governing inbound message steering, owner `go` approval gating, workspace read/write locking, and crash recovery.

## Coverage & Sync Record

- `sync_commit`: `c54ddfb`
- **Covered files & symbols:**
  - `src/watchpost/engine.py`: `_acquire_turn`, `_process_trigger_batch`, `steer_from_owner`, `dismiss_go`, `handle_owner_action`
  - `src/watchpost/store.py`: `create_or_update_thread`, `dismiss_thread_go`, `claim_pending_go`, `set_thread_status`, `dismiss_all_pending_go`, `recover_stale_active_threads`
- **Check for code drift since last sync:**
  ```sh
  git diff c54ddfb..HEAD -- src/watchpost/engine.py src/watchpost/store.py
  ```

If covered files changed after `sync_commit`, follow the installed `maintain-formal-verification-skill` before interpreting a pass. Only move `sync_commit` forward after confirming `formal/WatchpostThread.tla` accurately describes `HEAD` and passes a mutation check.

## Verified Properties (Invariants)

1. **`NoPlaceholderSteer`:** A follow-up batch never invokes `steer_thread` with an initial `pending_<id>` placeholder ref, and a finished initial thread never ends up persisted with a placeholder ref.
2. **`NoConcurrentWriteAndRead`:** A writable `approved_go` turn never runs concurrently with a read-only owner steer or inbound follow-up turn on the same thread/workspace.
3. **`NoZombieAwaitingGo`:** A thread never stays in `status = awaiting_go` once `pending_go` has been cleared outside the normal handoff into `steer_from_owner`.
4. **`AtMostOneApprovedGo`:** Concurrent or repeated `go` taps trigger at most one writable `approved_go` execution.

## Bounds, Assumptions & Out-of-Scope

- **Bounds:** Single thread, 2 inbound channel batches (`inb1`, `inb2`), 1 owner `go` attempt (`ownerGo`, `goExecCount <= 2`) plus duplicate tap check (`SecondGoAttempt`), 1 owner follow-up steer (`ownerSteer`), and at most 1 daemon crash/restart (`restarted`).
- **Out of scope:** Quiet-hours digest queuing (`quiet_digest_queue`), external adapter cursor/debouncer timing (`BurstDebouncer`), personal direct-chat threads (`personal_direct`), and `FallbackBackend` failover internals.

## Known Baseline Findings at `c54ddfb`

At `sync_commit` `c54ddfb`, TLC explores **170 states / 416 transitions**:
- **Holds:** `AtMostOneApprovedGo`, `NoDeadlock`
- **Baseline bugs (reproduced against `WatchpostEngine` + `WatchpostStore`):**
  - `NoPlaceholderSteer` (2 steps): `_process_trigger_batch` stores `pending_<thread_id>` before `start_thread` and a concurrent follow-up batch steers with it and overwrites the real ref.
  - `NoZombieAwaitingGo` (4 steps): `claim_pending_go` clears `pending_go` in `handle_owner_action` before `_format_owner_action_message`; an error or restart before `steer_from_owner` leaves `status = awaiting_go` with `pending_go = 0`, ignored by `recover_stale_active_threads` and `dismiss_all_pending_go`.
  - `NoConcurrentWriteAndRead` (5 steps): `_acquire_turn` leaves `write_lock=False` on read-only turns, allowing concurrent read/write backend execution in the same workspace.

Treat new failures or regressions in `AtMostOneApprovedGo` or `NoDeadlock` as immediate blockers; confirm whether baseline bugs were fixed before updating the expected outcome.

## How to Run

Run `formal/WatchpostThread.tla` with the installed `statechart` skill (writing output to a fresh folder under `/tmp`, never inside the repository):

- Model: `formal/WatchpostThread.tla`
- Config: `formal/WatchpostThread.cfg`
- View: `formal/watchpost-view.mjs`
- Output: `/tmp/watchpost-statechart-<run>` (must be a non-existent or empty directory)

*Contributor fallback (without the `statechart` skill):* run standard TLC (`java -cp tla2tools.jar tlc2.TLC -config formal/WatchpostThread.cfg formal/WatchpostThread.tla`).

## Interpreting Results & Upkeep Rules

- **Never weaken an invariant** or shrink bounds solely to make TLC pass.
- **Replay every violation:** If TLC reports a broken invariant, replay the exact sequence of actions against `WatchpostEngine` and `WatchpostStore` in an isolated test using temporary SQLite DBs under `/tmp`. If the sequence cannot occur in real Python execution, fix the TLA+ model (model drift) rather than product code.
- **Separate changes:** Keep verification updates (`formal/`, this skill) in their own clean commit separate from product bug fixes.
