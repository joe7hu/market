import { describe, expect, it } from "vitest";

import { emptyPanelData, mergeSnapshot } from "./apiPanelData";
import { buildPortfolioPhase4Decision } from "./viewModels/portfolioPhase4";
import type { PanelData } from "./types";

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

  it("reads canonical persisted sizing and scenario keys without zero fallbacks", () => {
    const data = mergeSnapshot(emptyPanelData(), {
      scope: "portfolio",
      tables: {
        portfolio_allocation: { rows: [{ allocation_id: "allocation:abc", canonical_portfolio: {
          allocation_id: "allocation:abc", input_cutoff: "2026-09-02T15:00:00Z", status: "available",
          actions: [{ allocation_id: "allocation:abc", allocation_item_id: "item:x", ticker: "ABC", disposition: "selected", strategy_forecast_id: "forecast:x", action_id: "action:x", expression: { kind: "STOCK" }, invalidation: { reason: "stop" }, missing_data: [], blockers: [], target_weight: 0.2, current_weight: 0.1, marginal_book_utility: 0.3, current_mrc: 0.12, proposed_mrc: 0.23, funding_source: "CASH:acct:1", sizing_trace: {} }],
          scenario_artifact_id: "scenario:x", execution_model_snapshot_id: null,
          scenario: { scenario_artifact_id: "scenario:x", allocation_id: "allocation:abc", scenarios: [{ probability: 1, returns: { ABC: 0.1 }, shocks: { ABC: -0.2 } }], tail_dependence: { "ABC|ABC": { probability: 1 } }, simultaneous_unwind: { probability: 0 } },
          execution: null, attribution_count: 0,
        } }] },
      },
    });
    const decision = buildPortfolioPhase4Decision(data as PanelData);
    expect(decision?.actions[0].currentMrc).toBe(0.12);
    expect(decision?.actions[0].proposedMrc).toBe(0.23);
    expect(decision?.actions[0].expression?.kind).toBe("STOCK");
    expect(decision?.actions[0].fundingSource).toBe("CASH:acct:1");
    expect(decision?.scenario?.scenarios[0].shocks).toEqual({ ABC: -0.2 });
  });

  it("rejects malformed or incomplete Phase 4 table identity", () => {
    const malformed = mergeSnapshot(emptyPanelData(), {
      tables: { portfolio_allocation: { rows: [{ allocation_id: 42 }] } },
    });
    const missingAllocation = mergeSnapshot(emptyPanelData(), {
      tables: { execution_model_snapshot: { rows: [{ execution_model_snapshot_id: "execution:x" }] } },
    });
    expect(malformed.errors.portfolio).toContain("Invalid or missing Phase 4 snapshot identity");
    expect(missingAllocation.errors.portfolio).toContain("Invalid or missing Phase 4 snapshot identity");
  });

  it("rejects malformed integrated identity instead of funding an ambiguous view", () => {
    const data = mergeSnapshot(emptyPanelData(), {
      portfolio_integrated: { allocation_id: "", input_cutoff: "2026-09-02T15:00:00Z", status: "cash_only", actions: [] },
    } as any);
    expect(data.errors.portfolio).toContain("Invalid or missing Phase 4 snapshot identity");
  });
});
