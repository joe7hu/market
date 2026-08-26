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
  it("fails closed to no trade when the current plan is missing", () => {
    expect(projectCapitalAction(decision, [])).toMatchObject({
      action: "NO_TRADE",
      expression: "CASH",
      rankReason: "trade_plan_missing",
    });
  });

  it("uses the exact complete plan for the displayed action", () => {
    const plan: RowRecord = {
      ticker: "AAA",
      decision_revision: "revision:AAA",
      opportunity_episode_id: "episode:AAA",
      trade_plan_id: "trade-plan.v1:aaa",
      eligibility: "ACTIONABLE",
      action: "BUY",
      selected_expression_kind: "STOCK",
      entry_limit: 100,
      quantity: 10,
      planned_loss: 50,
    };

    expect(projectCapitalAction(decision, [plan])).toMatchObject({
      action: "BUY",
      expression: "STOCK",
      price: "100",
      quantity: "10",
      loss: "50",
      planId: "trade-plan.v1:aaa",
      rankReason: "",
    });
  });
});
