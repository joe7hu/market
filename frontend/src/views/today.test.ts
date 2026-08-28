import { describe, expect, it } from "vitest";

import type { TodayResponse } from "@/api/panel";

const response: TodayResponse = {
  status: { ready: true, message: "test", source: "test" },
  count: 1,
  actions: [{
    projection_identity: "capital:ticker-decision:decision:AAA",
    source_authority: "ticker-decision:decision:AAA",
    source: "capital_action",
    title: "AAA capital action",
    lifecycle_state: "blocked",
    primary_blocker: "trade_plan_missing",
    next_action: "Refresh the ticker decision and trade plan.",
    ticker: "AAA",
    action: "NO_TRADE",
    owned: false,
    rationale: "Cash is selected.",
    policy_version: "risk-policy.v2:legacy",
    selected_expression: "CASH",
  }],
};

describe("Today Action Queue", () => {
  it("uses the generated backend item and its stable identity", () => {
    expect(response.actions?.[0]).toMatchObject({
      projection_identity: "capital:ticker-decision:decision:AAA",
      source_authority: "ticker-decision:decision:AAA",
      action: "NO_TRADE",
      selected_expression: "CASH",
      primary_blocker: "trade_plan_missing",
    });
  });
});
