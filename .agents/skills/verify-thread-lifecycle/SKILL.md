---
name: verify-thread-lifecycle
description: Formally verify Watchpost thread lifecycle, pending_go approval gates, workspace turn locking, and crash recovery invariants with TLA+ and TLC. Run during any major update or when changing src/watchpost/engine.py, src/watchpost/store.py, or src/watchpost/backends/.
---

# Verify Watchpost Thread Lifecycle

Checks the TLA+ model in `formal/WatchpostThread.tla` against the invariants governing inbound message steering, owner `go` approval gating, per-thread and workspace read/write locking, and crash recovery.

## Coverage & Sync Record

- `sync_commit`: `131b853`
- **Covered files & symbols:**
  - `src/watchpost/engine.py`: `_AsyncRWLock`, `_get_thread_lock`, `_acquire_turn`, `_process_trigger_batch`, `steer_from_owner`, `dismiss_go`, `handle_owner_action`
  - `src/watchpost/store.py`: `create_or_update_thread`, `dismiss_thread_go`, `claim_pending_go`, `set_thread_status`, `dismiss_all_pending_go`, `recover_stale_active_threads`
- **Check for code drift since last sync:**
  ```sh
  git diff 131b853..HEAD -- src/watchpost/engine.py src/watchpost/store.py
  ```

If covered files changed after `sync_commit`, follow the installed `maintain-formal-verification-skill` before interpreting a pass. Only move `sync_commit` forward after confirming `formal/WatchpostThread.tla` accurately describes `HEAD` and passes a mutation check.

## Verified Properties (Invariants)

1. **`NoPlaceholderSteer`:** A follow-up batch never invokes `steer_thread` with an initial `pending_<id>` placeholder ref, and a committed non-placeholder ref is never overwritten back to a placeholder.
2. **`NoConcurrentWriteAndRead`:** A writable `approved_go` turn never runs concurrently with a read-only owner steer or inbound follow-up turn on the same thread/workspace.
3. **`NoZombieAwaitingGo`:** A thread never stays in `status = awaiting_go` once `pending_go` has been cleared at any point, including immediately upon `claim_pending_go` (which atomically transitions `status` to `active`).
4. **`AtMostOneApprovedGo`:** Concurrent or repeated `go` taps trigger at most one writable `approved_go` execution.

## Bounds, Assumptions & Out-of-Scope

- **Bounds:** Single thread, 2 inbound channel batches (`inb1`, `inb2` covering placeholder steer check, real steer, and fresh start), `hadRealRef` tracking, 1 owner `go` attempt (`OwnerClaimGo`, `OwnerGoFailAfterClaim`, `OwnerGoStartWrite`, `goExecCount <= 2`) plus duplicate tap check (`SecondGoAttempt`), 1 owner follow-up steer (`ownerSteer`), and at most 1 daemon crash/restart (`restarted`).
- **Out of scope:** Quiet-hours digest queuing (`quiet_digest_queue`), external adapter cursor/debouncer timing (`BurstDebouncer`), personal direct-chat threads (`personal_direct`), and `FallbackBackend` failover internals.

## Verified Status at `131b853`

- **Model check (`WatchpostThread`):** TLC explores **75 states / 149 steps** (12 chart states, 29 arrows). All 4 invariants (`NoPlaceholderSteer`, `NoConcurrentWriteAndRead`, `NoZombieAwaitingGo`, `AtMostOneApprovedGo`) and `NoDeadlock` **hold** (`exit 0`).
- **Mutation proof:** Removing `ThreadLockFree` / `WorkspaceReadAllowed` from `OwnerSteerStart` fails `NoConcurrentWriteAndRead` in 5 steps (`exit 1`).
- **History:** Baseline violations discovered at `c54ddfb` (`NoPlaceholderSteer`, `NoConcurrentWriteAndRead`, `NoZombieAwaitingGo`) were fixed in `131b853` with regression tests in `tests/test_formal_state_regressions.py` and `tests/test_v040_launch_readiness.py`.

Treat any failure in these invariants or `NoDeadlock` as an immediate release blocker.

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
