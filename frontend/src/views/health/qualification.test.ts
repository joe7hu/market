import { describe, expect, it } from "vitest";
import type { DecisionFunnel, RefreshJob } from "@/api/panel";
import { alphaEvaluation, blockerLabel, decisionRefreshKey, funnelStageState } from "./qualification";

type Stage = NonNullable<DecisionFunnel["stages"]>[number];
const stage = (reasons: string[] = []) => ({ stage: "alpha_validation", reached_count: 33, count: 0, top_blockers: reasons.map(reason => ({ reason, count: 33 })) } as Stage);
const job = (overrides: Record<string, unknown> = {}) => ({ job_name: "run_stock_alpha_walk_forward", status: "skipped", started_at: "2026-09-22T21:00:00Z", summary: { observations: 14, complete: false, reason: "repeated_control_observations_unavailable" }, ...overrides } as RefreshJob);

describe("qualification is not service availability", () => {
  it("shows genuine alpha qualification without calling it a service outage", () => {
    expect(funnelStageState(stage(["alpha_strategy_revision_missing"]))).toBe("qualifying");
    expect(blockerLabel("alpha_strategy_revision_missing")).toBe("Stock strategy not qualified");
    expect(alphaEvaluation([job()])?.observations).toBe(14);
    expect(alphaEvaluation([job()])?.label).toBe("Collecting evidence");
  });
  it("does not hide a timeout alongside a qualification blocker", () => {
    expect(funnelStageState(stage(["alpha_strategy_revision_missing", "statement_timeout"]))).toBe("blocked");
    expect(alphaEvaluation([job({ status: "failed", error: "QueryCanceled: statement timeout" })])?.failed).toBe(true);
  });
  it("keeps a partial evaluator attempt partial", () => {
    expect(alphaEvaluation([job({ status: "partial" })])?.label).toBe("Evaluation partial");
  });
  it("does not assume every missing forecast is ordinary evidence accumulation", () => {
    expect(funnelStageState(stage(["forecast_missing"]))).toBe("blocked");
  });
  it("does not report an unresolved stage as passed merely because blockers were omitted", () => {
    expect(funnelStageState(stage())).toBe("blocked");
    expect(funnelStageState({ ...stage(), count: 33 })).toBe("passed");
  });
  it("does not count stopped-upstream stages as additional failures", () => {
    expect(funnelStageState({ ...stage(["forecast_missing"]), reached_count: 0 })).toBe("not_reached");
  });
  it("selects the latest attempt rather than a convenient prior success", () => {
    const latest = job({ started_at: "2026-09-23T01:00:00Z", status: "failed", error: "timeout" });
    expect(alphaEvaluation([latest, job()])?.label).toBe("Evaluator failed");
  });
  it("does not invent zero observations when the summary is absent", () => {
    expect(alphaEvaluation([job({ summary: {} })])?.observations).toBeNull();
    expect(alphaEvaluation([])).toBeNull();
  });
});

describe("canonical completion refresh key", () => {
  it("changes when the same decision job completes", () => {
    const running = job({ id: "decision-1", job_name: "refresh_decision_models", status: "running" });
    expect(decisionRefreshKey([running]) === decisionRefreshKey([{ ...running, status: "partial", finished_at: "2026-09-22T21:01:00Z" }])).toBe(false);
  });
  it("ignores irrelevant jobs and old attempts", () => {
    const current = job();
    expect(decisionRefreshKey([current, job({ started_at: "2026-09-21T21:00:00Z" }), job({ job_name: "other_job" })])).toBe(decisionRefreshKey([current]));
  });
});
