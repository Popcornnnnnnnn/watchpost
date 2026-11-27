\* TLA+ specification of Watchpost thread lifecycle, pending_go approval gates,
\* per-thread and workspace read/write locking, and daemon crash recovery.
\*
\* Code mapping (verified against WatchpostEngine + WatchpostStore):
\*   - Batch1Start / Batch1FinishAwaitingGo: src/watchpost/engine.py (_process_trigger_batch)
\*   - Batch2Start / Batch2Finish:           src/watchpost/engine.py (_process_trigger_batch, _acquire_turn) &
\*                                           src/watchpost/store.py (create_or_update_thread)
\*   - OwnerSteerStart / OwnerSteerFinish:   src/watchpost/engine.py (steer_from_owner, _get_thread_lock) &
\*                                           src/watchpost/store.py (set_thread_status)
\*   - OwnerClaimGo / OwnerGo*:              src/watchpost/engine.py (handle_owner_action, steer_from_owner) &
\*                                           src/watchpost/store.py (claim_pending_go)
\*   - OwnerDismissGo:                       src/watchpost/engine.py (dismiss_go) &
\*                                           src/watchpost/store.py (dismiss_thread_go)
\*   - DaemonRestart:                        src/watchpost/daemon.py (WatchpostDaemon.run_forever) &
\*                                           src/watchpost/store.py (recover_stale_active_threads)

---- MODULE WatchpostThread ----
EXTENDS Naturals

VARIABLES
    status,       \* "none", "active", "awaiting_go", "completed", "error"
    pendingGo,    \* BOOLEAN
    backendRef,   \* "none", "placeholder", "real"
    hadRealRef,   \* BOOLEAN (tracks whether a committed non-placeholder ref was ever obtained)
    inb1,         \* "idle", "running_start", "done"
    inb2,         \* "idle", "running_steer_ph", "running_steer_real", "running_start_fresh", "done"
    ownerGo,      \* "idle", "claimed_gap", "running_write", "done"
    ownerSteer,   \* "idle", "running_ro", "done"
    goExecCount,  \* 0..2
    restarted     \* BOOLEAN

vars == <<status, pendingGo, backendRef, hadRealRef, inb1, inb2, ownerGo, ownerSteer, goExecCount, restarted>>

\* Mirrors per-thread asyncio.Lock (_get_thread_lock) serialization across turns.
ThreadLockFree ==
    /\ inb1 /= "running_start"
    /\ inb2 \notin {"running_steer_ph", "running_steer_real", "running_start_fresh"}
    /\ ownerGo /= "running_write"
    /\ ownerSteer /= "running_ro"

\* Mirrors _AsyncRWLock shared/exclusive workspace gate (_acquire_turn).
WorkspaceReadAllowed ==
    ownerGo /= "running_write"

WorkspaceWriteAllowed ==
    /\ inb1 /= "running_start"
    /\ inb2 \notin {"running_steer_ph", "running_steer_real", "running_start_fresh"}
    /\ ownerSteer /= "running_ro"
    /\ ownerGo /= "running_write"

Init ==
    /\ status = "none"
    /\ pendingGo = FALSE
    /\ backendRef = "none"
    /\ hadRealRef = FALSE
    /\ inb1 = "idle"
    /\ inb2 = "idle"
    /\ ownerGo = "idle"
    /\ ownerSteer = "idle"
    /\ goExecCount = 0
    /\ restarted = FALSE

Batch1Start ==
    /\ inb1 = "idle"
    /\ status = "none"
    /\ ThreadLockFree
    /\ WorkspaceReadAllowed
    /\ status' = "active"
    /\ pendingGo' = FALSE
    /\ backendRef' = "placeholder"
    /\ inb1' = "running_start"
    /\ UNCHANGED <<hadRealRef, inb2, ownerGo, ownerSteer, goExecCount, restarted>>

Batch1FinishAwaitingGo ==
    /\ inb1 = "running_start"
    /\ status' = "awaiting_go"
    /\ pendingGo' = TRUE
    /\ backendRef' = "real"
    /\ hadRealRef' = TRUE
    /\ inb1' = "done"
    /\ UNCHANGED <<inb2, ownerGo, ownerSteer, goExecCount, restarted>>

Batch2Start ==
    /\ inb2 = "idle"
    /\ status /= "none"
    /\ ThreadLockFree
    /\ WorkspaceReadAllowed
    \* Re-reads thread inside _get_thread_lock; only calls steer_thread with a non-pending ref.
    /\ inb2' = IF backendRef = "real" THEN "running_steer_real" ELSE "running_start_fresh"
    /\ status' = "active"
    /\ pendingGo' = FALSE
    /\ UNCHANGED <<backendRef, hadRealRef, inb1, ownerGo, ownerSteer, goExecCount, restarted>>

Batch2Finish ==
    /\ inb2 \in {"running_steer_ph", "running_steer_real", "running_start_fresh"}
    /\ backendRef' = IF inb2 = "running_steer_ph" /\ ~hadRealRef THEN "placeholder" ELSE "real"
    /\ hadRealRef' = IF inb2 \in {"running_steer_real", "running_start_fresh"} THEN TRUE ELSE hadRealRef
    /\ status' = "completed"
    /\ pendingGo' = FALSE
    /\ inb2' = "done"
    /\ UNCHANGED <<inb1, ownerGo, ownerSteer, goExecCount, restarted>>

OwnerSteerStart ==
    /\ ownerSteer = "idle"
    /\ backendRef = "real"
    /\ status \in {"active", "awaiting_go", "completed"}
    /\ ThreadLockFree
    /\ WorkspaceReadAllowed
    /\ status' = "active"
    /\ ownerSteer' = "running_ro"
    /\ UNCHANGED <<pendingGo, backendRef, hadRealRef, inb1, inb2, ownerGo, goExecCount, restarted>>

OwnerSteerFinish ==
    /\ ownerSteer = "running_ro"
    /\ status' = IF pendingGo THEN "awaiting_go" ELSE "completed"
    /\ ownerSteer' = "done"
    /\ UNCHANGED <<pendingGo, backendRef, hadRealRef, inb1, inb2, ownerGo, goExecCount, restarted>>

OwnerClaimGo ==
    /\ ownerGo = "idle"
    /\ pendingGo = TRUE
    /\ pendingGo' = FALSE
    /\ status' = "active"
    /\ ownerGo' = "claimed_gap"
    /\ UNCHANGED <<backendRef, hadRealRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

OwnerGoStartWrite ==
    /\ ownerGo = "claimed_gap"
    /\ goExecCount < 2
    /\ ThreadLockFree
    /\ WorkspaceWriteAllowed
    /\ status' = "active"
    /\ ownerGo' = "running_write"
    /\ goExecCount' = goExecCount + 1
    /\ UNCHANGED <<pendingGo, backendRef, hadRealRef, inb1, inb2, ownerSteer, restarted>>

OwnerGoFailAfterClaim ==
    /\ ownerGo = "claimed_gap"
    /\ status' = "error"
    /\ ownerGo' = "done"
    /\ UNCHANGED <<pendingGo, backendRef, hadRealRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

OwnerGoFinishWrite ==
    /\ ownerGo = "running_write"
    /\ status' = IF pendingGo THEN "awaiting_go" ELSE "completed"
    /\ ownerGo' = "done"
    /\ UNCHANGED <<pendingGo, backendRef, hadRealRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

OwnerDismissGo ==
    /\ pendingGo = TRUE
    /\ ownerGo = "idle"
    /\ pendingGo' = FALSE
    /\ status' = IF status = "awaiting_go" THEN "completed" ELSE status
    /\ ownerGo' = "done"
    /\ UNCHANGED <<backendRef, hadRealRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

SecondGoAttempt ==
    /\ ownerGo /= "idle"
    /\ pendingGo = TRUE
    /\ goExecCount < 2
    /\ goExecCount' = goExecCount + 1
    /\ UNCHANGED <<status, pendingGo, backendRef, hadRealRef, inb1, inb2, ownerGo, ownerSteer, restarted>>

DaemonRestart ==
    /\ ~restarted
    /\ (inb1 = "running_start"
        \/ inb2 \in {"running_steer_ph", "running_steer_real", "running_start_fresh"}
        \/ ownerGo \in {"claimed_gap", "running_write"}
        \/ ownerSteer = "running_ro")
    /\ status' = IF status = "active" \/ (status = "awaiting_go" /\ ~pendingGo) THEN "error" ELSE status
    /\ pendingGo' = IF status = "active" \/ (status = "awaiting_go" /\ ~pendingGo) THEN FALSE ELSE pendingGo
    /\ inb1' = IF inb1 = "running_start" THEN "done" ELSE inb1
    /\ inb2' = IF inb2 \in {"running_steer_ph", "running_steer_real", "running_start_fresh"} THEN "done" ELSE inb2
    /\ ownerGo' = IF ownerGo \in {"claimed_gap", "running_write"} THEN "done" ELSE ownerGo
    /\ ownerSteer' = IF ownerSteer = "running_ro" THEN "done" ELSE ownerSteer
    /\ restarted' = TRUE
    /\ UNCHANGED <<backendRef, hadRealRef, goExecCount>>

Done ==
    /\ ThreadLockFree
    /\ ownerGo /= "claimed_gap"
    /\ status \in {"completed", "error", "awaiting_go"}
    /\ UNCHANGED vars

Next ==
    \/ Batch1Start
    \/ Batch1FinishAwaitingGo
    \/ Batch2Start
    \/ Batch2Finish
    \/ OwnerSteerStart
    \/ OwnerSteerFinish
    \/ OwnerClaimGo
    \/ OwnerGoStartWrite
    \/ OwnerGoFailAfterClaim
    \/ OwnerGoFinishWrite
    \/ OwnerDismissGo
    \/ SecondGoAttempt
    \/ DaemonRestart
    \/ Done

NoPlaceholderSteer ==
    /\ inb2 /= "running_steer_ph"
    /\ ~(hadRealRef /\ backendRef = "placeholder")

NoConcurrentWriteAndRead ==
    ~(ownerGo = "running_write" /\ (ownerSteer = "running_ro" \/ inb2 \in {"running_steer_ph", "running_steer_real", "running_start_fresh"}))

NoZombieAwaitingGo ==
    ~(status = "awaiting_go" /\ ~pendingGo)

AtMostOneApprovedGo ==
    goExecCount <= 1

====
