import { humanize, statusMessage } from "./labels";

export function actionLabel(value: unknown): string {
  const raw = String(value ?? "");
  const labels: Record<string, string> = {
    REVIEW: "Review evidence",
    RETRY_RESOLUTION: "Retry outcome check",
    review: "Review evidence",
    retry_resolution: "Retry outcome check",
    deterministic_policy_gates_only: "Deterministic policy gates only",
    advisory_only: "Advisory only",
  };
  return labels[raw] ?? humanize(raw, "Review");
}

export function nextAction(blockers: unknown, fallback = "Continue collecting evidence."): string {
  if (!Array.isArray(blockers) || blockers.length === 0) return fallback;
  return statusMessage(blockers[0]);
}

export function eventLabel(value: unknown): string {
  const raw = String(value ?? "");
  const labels: Record<string, string> = {
    paper_entry: "Entry filled",
    paper_exit: "Exit filled",
    paper_exit_partial: "Partial exit",
    strategy_activation: "Strategy activated",
    strategy_rollback: "Strategy rolled back",
    strategy_mutation_proposal: "Strategy improvement proposed",
    prompt_activation: "Prompt activated",
  };
  if (raw.startsWith("paper_exit:")) return raw.includes("partial") ? "Partial exit" : "Exit filled";
  return labels[raw] ?? humanize(raw, "Research event");
}
