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

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { emptyPanelData } from "@/api/panel";
import { OpportunitiesPage, opportunityReason, screenerMetric } from "./opportunities";

it("shows evidence and a direct ticker review without diagnostics; formats actual metric units", () => {
  const row = { ...backendPayload[0], company_name: "NVIDIA", rationale: "Demand supports the research case. Full detail follows.", countercase: "Spending may slow.", selected_expression_kind: "CASH", blockers: ["cash_comparator", "forecast_missing"], primary_blocker: "cash_comparator" };
  const data = { ...emptyPanelData(), opportunitiesRanked: { rows: [row], count: 806, offset: 0, limit: 120 } };
  const html = renderToStaticMarkup(createElement(OpportunitiesPage, { data, loading: false, onOpenTicker: () => undefined, onRefresh: async () => undefined, onLoadScreener: async () => undefined, onLoadMore: async () => undefined }));
  expect(html).toContain("NVIDIA");
  expect(html).toContain("Demand supports the research case.");
  expect(html).toContain("Spending may slow.");
  expect(html).toContain("Review NVDA");
  expect(html).toContain("1 of 806 loaded");
  expect(html).toContain("Load more");
  expect(html).not.toMatch(/ep-1|rank-1|ticker-decision|cash_comparator|Not Applicable|Full detail follows/);
  expect(opportunityReason(row as unknown as OpportunityDecisionRow)).toBe("A supported return forecast is not available.");
  expect(screenerMetric(0.177, 100, "%")).toBe("17.7%");
  expect(screenerMetric(28.78, 1, "%")).toBe("28.78%");
  expect(screenerMetric(0, 100, "%")).toBe("0%");
  expect(screenerMetric(null, 100, "%")).toBe("—");
});
