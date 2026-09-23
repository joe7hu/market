import type { DecisionFunnel, RefreshJob } from "@/api/panel";

type Stage = NonNullable<DecisionFunnel["stages"]>[number];
const asRecord = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
const LABELS: Record<string, string> = {
  alpha_strategy_revision_missing: "Stock strategy not qualified",
  repeated_control_observations_unavailable: "Repeated controls need evidence",
  forecast_missing: "Forecast not published",
  current_price: "Confirmed price required",
  trade_plan_missing: "Trade plan incomplete",
  trade_plan_expired: "Trade plan expired",
  cash_comparator: "Trade has not cleared the cash hurdle",
  insufficient_history: "Price history incomplete",
  scenario_evidence_missing: "Stress evidence required",
  stock_cash_comparator_missing: "Cash comparison not calculated",
};
const QUALIFICATION = new Set(["alpha_strategy_revision_missing", "repeated_control_observations_unavailable"]);
export const blockerLabel = (reason: string): string => LABELS[reason] ?? reason.replaceAll("_", " ").replace(/^./, value => value.toUpperCase());

export function funnelStageState(stage: Stage): "passed" | "qualifying" | "blocked" | "not_reached" {
  if (typeof stage.reached_count !== "number" || typeof stage.count !== "number" || stage.count < 0 || stage.reached_count < stage.count) return "blocked";
  if (stage.reached_count === 0) return "not_reached";
  const blockers = stage.top_blockers ?? [];
  if (stage.count === stage.reached_count && blockers.length === 0) return "passed";
  if (blockers.length > 0 && blockers.every(blocker => QUALIFICATION.has(blocker.reason))) return "qualifying";
  return "blocked";
}

export function alphaEvaluation(jobs: readonly RefreshJob[]) {
  const job = jobs.filter(row => row.job_name === "run_stock_alpha_walk_forward")
    .sort((left, right) => (Date.parse(right.started_at ?? "") || 0) - (Date.parse(left.started_at ?? "") || 0))[0];
  if (!job) return null;
  const summary = asRecord(job.summary);
  const count = summary.observations;
  const observations = typeof count === "number" && Number.isInteger(count) && count >= 0 ? count : null;
  const reason = typeof summary.reason === "string" ? summary.reason : null;
  const failed = job.status === "failed" || Boolean(job.error);
  const label = failed ? "Evaluator failed" : job.status === "running" ? "Evaluating"
    : job.status === "partial" ? "Evaluation partial"
      : reason === "repeated_control_observations_unavailable" ? "Collecting evidence"
        : summary.complete === true ? "Validation passed"
          : reason === "no_new_stock_alpha_evidence" ? "Evidence unchanged" : "Not qualified";
  return { job, observations, reason, label, failed, complete: summary.complete === true };
}

/** A completed canonical attempt invalidates the displayed funnel, not its job heartbeat. */
export function decisionRefreshKey(jobs: readonly RefreshJob[]): string {
  const latest = new Map<string, RefreshJob>();
  for (const job of jobs) {
    const name = job.job_name;
    if (name !== "refresh_decision_models" && name !== "run_stock_alpha_walk_forward") continue;
    const previous = latest.get(name);
    if (!previous || (Date.parse(job.started_at ?? "") || 0) > (Date.parse(previous.started_at ?? "") || 0)) latest.set(name, job);
  }
  return [...latest].sort(([a], [b]) => a.localeCompare(b)).map(([name, job]) =>
    `${name}:${job.id ?? ""}:${job.started_at ?? ""}:${job.status ?? ""}:${job.finished_at ?? ""}`).join("|");
}
