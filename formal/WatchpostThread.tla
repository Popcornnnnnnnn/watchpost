\* TLA+ specification of Watchpost thread lifecycle, pending_go approval gates,
\* workspace write locks, and daemon crash recovery.
\*
\* Code mapping:
\*   - Batch1Start / Batch1FinishAwaitingGo: src/watchpost/engine.py (_process_trigger_batch)
\*   - Batch2Start / Batch2Finish:           src/watchpost/engine.py (_process_trigger_batch) &
\*                                           src/watchpost/store.py (create_or_update_thread)
\*   - OwnerSteerStart / OwnerSteerFinish:   src/watchpost/engine.py (steer_from_owner) &
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
    inb1,         \* "idle", "running_start", "done"
    inb2,         \* "idle", "running_steer_ph", "running_steer_real", "done"
    ownerGo,      \* "idle", "claimed_gap", "running_write", "done"
    ownerSteer,   \* "idle", "running_ro", "done"
    goExecCount,  \* 0..2
    restarted     \* BOOLEAN

vars == <<status, pendingGo, backendRef, inb1, inb2, ownerGo, ownerSteer, goExecCount, restarted>>

Init ==
    /\ status = "none"
    /\ pendingGo = FALSE
    /\ backendRef = "none"
    /\ inb1 = "idle"
    /\ inb2 = "idle"
    /\ ownerGo = "idle"
    /\ ownerSteer = "idle"
    /\ goExecCount = 0
    /\ restarted = FALSE

Batch1Start ==
    /\ inb1 = "idle"
    /\ status = "none"
    /\ status' = "active"
    /\ pendingGo' = FALSE
    /\ backendRef' = "placeholder"
    /\ inb1' = "running_start"
    /\ UNCHANGED <<inb2, ownerGo, ownerSteer, goExecCount, restarted>>

Batch1FinishAwaitingGo ==
    /\ inb1 = "running_start"
    /\ status' = "awaiting_go"
    /\ pendingGo' = TRUE
    /\ backendRef' = "real"
    /\ inb1' = "done"
    /\ UNCHANGED <<inb2, ownerGo, ownerSteer, goExecCount, restarted>>

Batch2Start ==
    /\ inb2 = "idle"
    /\ status /= "none"
    /\ inb2' = IF backendRef = "placeholder" THEN "running_steer_ph" ELSE "running_steer_real"
    /\ status' = "active"
    /\ pendingGo' = FALSE
    /\ UNCHANGED <<backendRef, inb1, ownerGo, ownerSteer, goExecCount, restarted>>

Batch2Finish ==
    /\ inb2 \in {"running_steer_ph", "running_steer_real"}
    /\ backendRef' = IF inb2 = "running_steer_ph" THEN "placeholder" ELSE "real"
    /\ status' = "completed"
    /\ pendingGo' = FALSE
    /\ inb2' = "done"
    /\ UNCHANGED <<inb1, ownerGo, ownerSteer, goExecCount, restarted>>

OwnerSteerStart ==
    /\ ownerSteer = "idle"
    /\ backendRef = "real"
    /\ status \in {"active", "awaiting_go", "completed"}
    /\ status' = "active"
    /\ ownerSteer' = "running_ro"
    /\ UNCHANGED <<pendingGo, backendRef, inb1, inb2, ownerGo, goExecCount, restarted>>

OwnerSteerFinish ==
    /\ ownerSteer = "running_ro"
    /\ status' = IF pendingGo THEN "awaiting_go" ELSE "completed"
    /\ ownerSteer' = "done"
    /\ UNCHANGED <<pendingGo, backendRef, inb1, inb2, ownerGo, goExecCount, restarted>>

OwnerClaimGo ==
    /\ ownerGo = "idle"
    /\ pendingGo = TRUE
    /\ pendingGo' = FALSE
    /\ ownerGo' = "claimed_gap"
    /\ UNCHANGED <<status, backendRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

OwnerGoStartWrite ==
    /\ ownerGo = "claimed_gap"
    /\ goExecCount < 2
    /\ status' = "active"
    /\ ownerGo' = "running_write"
    /\ goExecCount' = goExecCount + 1
    /\ UNCHANGED <<pendingGo, backendRef, inb1, inb2, ownerSteer, restarted>>

OwnerGoFailInGap ==
    /\ ownerGo = "claimed_gap"
    /\ ownerGo' = "done"
    /\ UNCHANGED <<status, pendingGo, backendRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

OwnerGoFinishWrite ==
    /\ ownerGo = "running_write"
    /\ status' = IF pendingGo THEN "awaiting_go" ELSE "completed"
    /\ ownerGo' = "done"
    /\ UNCHANGED <<pendingGo, backendRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

OwnerDismissGo ==
    /\ pendingGo = TRUE
    /\ ownerGo = "idle"
    /\ pendingGo' = FALSE
    /\ status' = IF status = "awaiting_go" THEN "completed" ELSE status
    /\ ownerGo' = "done"
    /\ UNCHANGED <<backendRef, inb1, inb2, ownerSteer, goExecCount, restarted>>

SecondGoAttempt ==
    /\ ownerGo /= "idle"
    /\ pendingGo = TRUE
    /\ goExecCount < 2
    /\ goExecCount' = goExecCount + 1
    /\ UNCHANGED <<status, pendingGo, backendRef, inb1, inb2, ownerGo, ownerSteer, restarted>>

DaemonRestart ==
    /\ ~restarted
    /\ (inb1 = "running_start"
        \/ inb2 \in {"running_steer_ph", "running_steer_real"}
        \/ ownerGo \in {"claimed_gap", "running_write"}
        \/ ownerSteer = "running_ro")
    /\ status' = IF status = "active" THEN "error" ELSE status
    /\ pendingGo' = IF status = "active" THEN FALSE ELSE pendingGo
    /\ inb1' = IF inb1 = "running_start" THEN "done" ELSE inb1
    /\ inb2' = IF inb2 \in {"running_steer_ph", "running_steer_real"} THEN "done" ELSE inb2
    /\ ownerGo' = IF ownerGo \in {"claimed_gap", "running_write"} THEN "done" ELSE ownerGo
    /\ ownerSteer' = IF ownerSteer = "running_ro" THEN "done" ELSE ownerSteer
    /\ restarted' = TRUE
    /\ UNCHANGED <<backendRef, goExecCount>>

Done ==
    /\ inb1 /= "running_start"
    /\ inb2 \notin {"running_steer_ph", "running_steer_real"}
    /\ ownerGo \notin {"claimed_gap", "running_write"}
    /\ ownerSteer /= "running_ro"
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
    \/ OwnerGoFailInGap
    \/ OwnerGoFinishWrite
    \/ OwnerDismissGo
    \/ SecondGoAttempt
    \/ DaemonRestart
    \/ Done

NoPlaceholderSteer ==
    /\ inb2 /= "running_steer_ph"
    /\ ~(inb1 = "done" /\ backendRef = "placeholder")

NoConcurrentWriteAndRead ==
    ~(ownerGo = "running_write" /\ (ownerSteer = "running_ro" \/ inb2 \in {"running_steer_ph", "running_steer_real"}))

NoZombieAwaitingGo ==
    ~(status = "awaiting_go" /\ ~pendingGo /\ ownerGo /= "claimed_gap")

AtMostOneApprovedGo ==
    goExecCount <= 1

====
