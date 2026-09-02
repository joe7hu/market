import { describe, expect, it } from "vitest";

import type { OpportunityDecisionRow } from "./opportunities";
import { dedupeOpportunityEpisodes, EXPRESSION_KINDS, opportunityDecisionRows, shouldLoadScreener } from "./opportunities";

const backendPayload = [
  {
    availability_status: "available",
    blockers: [],
    decision_revision: "ticker-decision.v1:1",
    opportunity_episode_id: "ep-1",
    rank_id: "rank-1",
    ticker: "NVDA",
  },
  {
    availability_status: "available",
    blockers: [],
    decision_revision: "ticker-decision.v1:2",
    opportunity_episode_id: "ep-1",
    rank_id: "rank-2",
    ticker: "NVDA",
  },
  {
    availability_status: "policy_blocked",
    blockers: ["risk_policy_blocked"],
    decision_revision: "ticker-decision.v1:3",
    opportunity_episode_id: "ep-2",
    rank_id: "rank-3",
    ticker: "MSFT",
  },
] satisfies Array<Pick<OpportunityDecisionRow, "availability_status" | "blockers" | "decision_revision" | "opportunity_episode_id" | "rank_id" | "ticker">>;

describe("opportunity decision surface", () => {
  it("keeps one row per episode while preserving the first published row", () => {
    const rows = dedupeOpportunityEpisodes(opportunityDecisionRows({ rows: backendPayload }));

    expect(rows).toHaveLength(2);
    expect(rows[0]?.rank_id).toBe("rank-1");
    expect(rows.map((row) => row.opportunity_episode_id)).toEqual(["ep-1", "ep-2"]);
  });

  it("uses exact generated backend field names for decision rows", () => {
    expect(opportunityDecisionRows({ rows: backendPayload }).map((row) => row.ticker)).toEqual(["NVDA", "NVDA", "MSFT"]);
  });

  it("defines the complete expression comparison contract", () => {
    expect(EXPRESSION_KINDS).toEqual(["stock", "option/spread", "CSP", "crypto", "hedge", "cash"]);
  });

  it("loads the screener only for the selected view", () => {
    expect(shouldLoadScreener("screener")).toBe(true);
    expect(shouldLoadScreener("episodes")).toBe(false);
  });
});
