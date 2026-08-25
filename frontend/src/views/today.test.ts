import { describe, expect, it } from "vitest";

import type { RowRecord } from "@/types";
import { projectCapitalAction } from "./today";

const decision: RowRecord = {
  ticker: "AAA",
  decision_revision: "revision:AAA",
  opportunity_episode_id: "episode:AAA",
  capital_action: { action: "BUY", owned: false, rationale: "Buy the stock." },
  selected_expression: { kind: "STOCK" },
};

describe("Today capital projection", () => {
  it("fails closed to cash when the current rank is missing", () => {
    expect(projectCapitalAction(decision, [])).toMatchObject({
      action: "AVOID",
      expression: "CASH",
      rankReason: "opportunity_rank_missing",
    });
  });

  it("uses the exact complete rank for the displayed action", () => {
    const rank: RowRecord = {
      ticker: "AAA",
      decision_revision: "revision:AAA",
      opportunity_episode_id: "episode:AAA",
      selected_expression_kind: "STOCK",
      evaluated_universe_complete: true,
      research_rank: 2,
      trade_rank: 1,
      trade_utility: 0.4,
    };

    expect(projectCapitalAction(decision, [rank])).toMatchObject({
      action: "BUY",
      expression: "STOCK",
      researchRank: 2,
      tradeRank: 1,
      rankReason: "",
    });
  });
});
