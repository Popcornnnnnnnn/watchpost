export const title = "Watchpost Thread and Approval Lifecycle";

export const colors = {
  setup: "blue",
  triage: "teal",
  approval: "yellow",
  writing: "purple",
  done: "tan",
  broken: "pink",
};

const classify = (v) => {
  const phBug = v.inb2 === "running_steer_ph" || (v.hadRealRef && v.backendRef === "placeholder");
  const rwBug =
    v.ownerGo === "running_write" &&
    (v.ownerSteer === "running_ro" ||
      v.inb2 === "running_steer_ph" ||
      v.inb2 === "running_steer_real" ||
      v.inb2 === "running_start_fresh");
  const zombieBug = v.status === "awaiting_go" && !v.pendingGo;
  const dupGoBug = v.goExecCount > 1;
  return { phBug, rwBug, zombieBug, dupGoBug };
};

export const state = (v) => {
  const { phBug, rwBug, zombieBug, dupGoBug } = classify(v);

  if (phBug && rwBug) return "Placeholder steer plus read/write race";
  if (phBug) return "Placeholder ref steered or saved";
  if (rwBug) return "Writable go plus read-only turn running together";
  if (zombieBug) return "Zombie awaiting_go (pending_go cleared)";
  if (dupGoBug) return "Duplicate go execution";

  if (v.status === "none") return "No thread yet";
  if (v.inb1 === "running_start") return "Initial triage running";
  if (v.ownerGo === "claimed_gap") {
    if (v.ownerSteer === "running_ro") return "Follow-up turn running (go queued)";
    if (v.inb2 === "running_steer_real" || v.inb2 === "running_start_fresh") {
      return "Inbound follow-up running (go queued)";
    }
    return "Go claimed, preparing turn";
  }
  if (v.ownerGo === "running_write") return "Approved fix running";
  if (v.inb2 === "running_start_fresh") return "Follow-up starting fresh thread";
  if (v.ownerSteer === "running_ro" || v.inb2 === "running_steer_real") {
    return v.pendingGo ? "Follow-up turn (go still pending)" : "Follow-up turn running";
  }
  if (v.status === "awaiting_go") return "Awaiting go approval";
  if (v.status === "error") return "Error after failure or restart";
  return "Completed";
};

export const group = (v) => {
  const { phBug, rwBug, zombieBug, dupGoBug } = classify(v);
  if (phBug || rwBug || zombieBug || dupGoBug) return "broken";
  if (v.status === "none") return "setup";
  if (v.inb1 === "running_start") return "triage";
  if (v.status === "awaiting_go" || v.ownerGo === "claimed_gap") return "approval";
  if (v.ownerGo === "running_write") return "writing";
  return "done";
};

export const step = (action, from, to) => {
  const fromS = state(from);
  const toS = state(to);
  if (action === "Done") return null;
  if (action === "DaemonRestart" && toS === "Completed") return null;
  if (fromS === "Placeholder steer plus read/write race" && toS === "Error after failure or restart") return null;

  switch (action) {
    case "Batch1Start":
      return "first message arrives";
    case "Batch1FinishAwaitingGo":
      return "diagnosis proposes fix";
    case "Batch2Start":
      return "new channel message arrives";
    case "Batch2Finish":
      return "follow-up finishes";
    case "OwnerSteerStart":
      return "owner sends follow-up";
    case "OwnerSteerFinish":
      return "owner follow-up finishes";
    case "OwnerClaimGo":
      return "owner taps go";
    case "OwnerGoStartWrite":
      return "fix turn starts";
    case "OwnerGoFailAfterClaim":
      return "post-claim prep fails";
    case "OwnerGoFinishWrite":
      return "fix turn finishes";
    case "OwnerDismissGo":
      return "owner taps skip";
    case "SecondGoAttempt":
      return "duplicate go tap";
    case "DaemonRestart":
      return "daemon restarts";
    default:
      return action;
  }
};
