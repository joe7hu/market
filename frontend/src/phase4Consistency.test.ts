import { describe, expect, it } from "vitest";

import { emptyPanelData, mergeSnapshot } from "./apiPanelData";

describe("Phase 4 workspace consistency", () => {
  it("keeps immutable allocation, action, and forecast IDs across all five scopes", () => {
    const tables = {
      portfolio_allocation: { rows: [{ allocation_id: "allocation:abc", action_ids: ["action:x"], forecast_ids: ["forecast:x"] }] },
      portfolio_allocation_items: { rows: [{ allocation_item_id: "allocation-item:x", action_id: "action:x", strategy_forecast_id: "forecast:x" }] },
    };
    const data = ["today", "opportunities", "portfolio", "research", "health"].reduce(
      (current, scope) => mergeSnapshot(current, { scope, tables }), emptyPanelData(),
    );
    expect(data.portfolioAllocation.rows?.[0].allocation_id).toBe("allocation:abc");
    expect(data.portfolioAllocationItems.rows?.[0].action_id).toBe("action:x");
    expect(data.portfolioAllocationItems.rows?.[0].strategy_forecast_id).toBe("forecast:x");
  });
});
