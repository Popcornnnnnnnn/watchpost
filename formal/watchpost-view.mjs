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
  const phBug = v.inb2 === "running_steer_ph" || (v.inb1 === "done" && v.backendRef === "placeholder");
  const rwBug =
    v.ownerGo === "running_write" &&
    (v.ownerSteer === "running_ro" || v.inb2 === "running_steer_ph" || v.inb2 === "running_steer_real");
  const zombieBug = v.status === "awaiting_go" && !v.pendingGo && v.ownerGo !== "claimed_gap";
  return { phBug, rwBug, zombieBug };
};

export const state = (v) => {
  const { phBug, rwBug, zombieBug } = classify(v);

  if (phBug && rwBug) return "Placeholder steer + read/write race";
  if (phBug) return "Placeholder ref steered or saved";
  if (rwBug) return "Writable go + read-only turn running together";
  if (zombieBug) return "Zombie awaiting_go (pending_go cleared)";

  if (v.status === "none") return "No thread yet";
  if (v.inb1 === "running_start") return "Initial triage running";
  if (v.ownerGo === "claimed_gap") return "Go claimed, preparing turn";
  if (v.ownerGo === "running_write") return "Approved fix running";
  if (v.ownerSteer === "running_ro" || v.inb2 === "running_steer_real") {
    return v.pendingGo ? "Follow-up turn (go still pending)" : "Follow-up turn running";
  }
  if (v.status === "awaiting_go") return "Awaiting go approval";
  if (v.status === "error") return "Error after restart";
  return "Completed";
};

export const group = (v) => {
  const { phBug, rwBug, zombieBug } = classify(v);
  if (phBug || rwBug || zombieBug) return "broken";
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
  if (action === "DaemonRestart" && toS === "Error after restart" && fromS !== "Approved fix running") {
    return null;
  }
  if (action === "DaemonRestart" && toS === "Completed") return null;
  if (fromS === "Placeholder steer + read/write race" && toS === "Error after restart") return null;

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
    case "OwnerGoFailInGap":
      return "prep fails before turn";
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
