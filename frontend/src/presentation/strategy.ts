import { humanize, statusLabel } from "./labels";
import { learningProgress } from "./lifecycle";

export function strategyName(value: Record<string, any>, fallback = "Strategy registry"): string {
  return String(value.name || value.strategy_name || value.deployed_version || value.strategy_key || fallback);
}

export function strategyStatus(value: unknown): string {
  return statusLabel(value, "Not started");
}

export function strategyTone(value: unknown): "good" | "warn" | "bad" | "info" | "muted" {
  const raw = String(value ?? "");
  return raw === "active" || raw === "promoted" ? "good" : raw === "candidate" || raw === "collecting_outcomes" ? "warn" : ["rejected", "misconfigured", "invalid_parameters", "unsupported_parameters", "implementation_version_mismatch", "parameter_lineage_mismatch"].includes(raw) ? "bad" : "info";
}

export function strategyProgress(lane: Record<string, any>): { historical: ReturnType<typeof learningProgress>; forward: ReturnType<typeof learningProgress>; paper: ReturnType<typeof learningProgress> } {
  const progress = lane.progress ?? lane.evidence_progress ?? {};
  return {
    historical: learningProgress(progress.historical_completed ?? lane.historical_completed ?? null, progress.historical_required ?? lane.historical_required ?? 0),
    forward: learningProgress(progress.forward_completed ?? lane.forward_completed ?? lane.matched_outcomes ?? 0, "forward_required" in progress ? progress.forward_required : lane.forward_required ?? 30),
    paper: learningProgress(progress.paper_completed ?? lane.paper_completed ?? 0, "paper_required" in progress ? progress.paper_required : lane.paper_required ?? 20),
  };
}

export function strategySteps(lane: Record<string, any>): Array<{ label: string; value: string; detail?: string; state: "complete" | "current" | "pending" }> {
  const progress = strategyProgress(lane);
  return [
    { label: "Active", value: strategyName({ deployed_version: lane.deployed_version }, "No active strategy"), state: lane.deployed_version ? "complete" : "pending" },
    { label: "Historical validation", value: progress.historical.label, detail: progress.historical.detail, state: lane.progress?.historical_verdict === "pass" ? "complete" : "current" },
    { label: "Forward test", value: progress.forward.label, detail: progress.forward.detail, state: lane.progress?.forward_verdict === "pass" ? "complete" : "current" },
    { label: "Paper evidence", value: progress.paper.label, detail: progress.paper.detail, state: lane.progress?.paper_verdict === "pass" ? "complete" : "pending" },
  ];
}

export function strategyChangeLabel(value: unknown): string {
  return humanize(value, "Strategy change");
}
