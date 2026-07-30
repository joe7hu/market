import { describe, expect, it } from "vitest";

import type { OptionsDecisionBrief } from "@/api";
import { blockerCopy, decisionPresentation, summaryNumber } from "./decisionDesk";

function brief(overrides: Partial<OptionsDecisionBrief> = {}): OptionsDecisionBrief {
  return {
    symbol: "QQQ",
    lane: "thesis",
    mode: "paper",
    analysis_run_id: "run-1",
    as_of: "2026-07-29T16:00:00-04:00",
    state: "COLLECTING",
    summary: { relative_values: 12_214 },
    readiness: {
      capture: { capture_state: "complete", completeness: 1, capture_generation_id: 255, complete_captures: 201 },
      underlying: { group_count: 66, groups_with_missing_underlying: 25, groups_with_inconsistent_underlying: 0 },
      analysis: { eligible_groups: 12, fit_attempts: 12, succeeded_groups: 12, solver_failures: 0 },
      thesis: { eligible: false, revision: null, invalidation: null },
      calibration: [],
      canary: {
        observed_regular_session_dates: 8,
        qualified_regular_sessions: 4,
        required_regular_sessions: 5,
        canary_revision: "r3",
        canary_started_at: null,
        disqualification_reasons: [],
      },
      top_blockers: [],
      next_required_action: "run_qqq_thesis_monitor",
    },
    strongest_candidate: null,
    paper_only: true,
    ...overrides,
  };
}

describe("decision desk presentation", () => {
  it("turns a generic collecting state into the actual next decision", () => {
    const result = decisionPresentation(brief());
    expect(result.title).toBe("No trade — QQQ thesis pending");
    expect(result.action).toBe("thesis");
    expect(result.actionLabel).toBe("Open QQQ thesis monitor");
  });

  it("shows the canary gap after the thesis gate is satisfied", () => {
    const current = brief();
    current.readiness.thesis = { eligible: true, revision: "v2", invalidation: "QQQ closes below support" };
    expect(decisionPresentation(current).title).toBe("Wait — 1 qualified session remaining");
  });

  it("treats an automated neutral thesis as an intentional no-trade view", () => {
    const current = brief();
    current.readiness.thesis = {
      eligible: false,
      present: true,
      revision: "1",
      direction: "neutral",
      blocker: "thesis_direction_required",
      invalidation: "Reassess when directional evidence improves",
    };
    const result = decisionPresentation(current);
    expect(result.title).toBe("No trade — QQQ thesis is neutral");
    expect(result.detail).toContain("Thesis Monitor will reassess");
  });

  it("humanizes blocker codes and reads numeric funnel fields", () => {
    expect(blockerCopy("illiquid_spread").label).toBe("Bid/ask spread too wide");
    expect(blockerCopy("new_policy_gate").label).toBe("New policy gate");
    expect(summaryNumber(brief(), "relative_values")).toBe(12_214);
    expect(summaryNumber(brief(), "missing")).toBe(0);
  });
});
