import { describe, expect, it } from "vitest";

import { emptyPanelData, mergePanelData, mergeSnapshot } from "./apiPanelData";

describe("mergePanelData", () => {
  it("keeps independently loaded scopes and appends paged rows", () => {
    const first = emptyPanelData();
    const merged = mergePanelData(first, {
      ...emptyPanelData(),
      quotes: { rows: [{ ticker: "AAA" }], count: 1 },
      portfolioAllocation: { rows: [{ allocation_id: "allocation:shared" }] },
      scopeStatus: { today: { state: "ready" } },
    });
    const final = mergePanelData(merged, {
      ...emptyPanelData(),
      opportunitiesRanked: { rows: [{ ticker: "BBB" }], count: 1 },
      quotes: { rows: [{ ticker: "CCC" }], count: 2 },
      portfolioAllocation: { rows: [{ allocation_id: "allocation:shared" }] },
      scopeStatus: { opportunities: { state: "ready" } },
    }, { append: true });

    expect(final.quotes.rows).toEqual([{ ticker: "AAA" }, { ticker: "CCC" }]);
    expect(final.opportunitiesRanked.rows).toEqual([{ ticker: "BBB" }]);
    expect(final.scopeStatus).toEqual({ today: { state: "ready" }, opportunities: { state: "ready" } });
  });

  it("clears omitted Phase 4 objects when a scoped response rolls to a new identity", () => {
    const existing = mergePanelData(emptyPanelData(), {
      ...emptyPanelData(),
      portfolioAllocation: { rows: [{ allocation_id: "allocation:old" }] },
      portfolioScenarioArtifact: { rows: [{ allocation_id: "allocation:old", scenario_artifact_id: "scenario:old" }] },
      executionModelSnapshot: { rows: [{ allocation_id: "allocation:old", execution_model_snapshot_id: "execution:old" }] },
      portfolioIntegrated: { allocation_id: "allocation:old", input_cutoff: "2026-09-02T15:00:00Z", status: "cash_only", actions: [], scenario_artifact_id: "scenario:old", execution_model_snapshot_id: "execution:old" } as any,
      scopeStatus: { portfolio: { state: "ready" } },
    });
    const next = mergePanelData(existing, {
      ...emptyPanelData(),
      portfolioAllocation: { rows: [{ allocation_id: "allocation:new" }] },
      scopeStatus: { portfolio: { state: "ready" } },
    });

    expect(next.portfolioAllocation.rows?.[0].allocation_id).toBe("allocation:new");
    expect(next.portfolioScenarioArtifact).toBeUndefined();
    expect(next.executionModelSnapshot).toBeUndefined();
    expect(next.portfolioIntegrated).toBeUndefined();
  });

  it("retains tables explicitly deferred by the bounded dashboard", () => {
    const existing = mergePanelData(emptyPanelData(), {
      ...emptyPanelData(),
      fundamentals: { rows: [{ symbol: "AAA", value: 1 }], count: 1 },
    });
    const next = mergeSnapshot(existing, {
      scope: "dashboard",
      status: { metadata: { dashboard_deferred_models: ["fundamentals"] } },
      tables: { fundamentals: { rows: [], count: 0 } },
    });

    expect(next.fundamentals.rows).toEqual([{ symbol: "AAA", value: 1 }]);
  });
});

describe("research without allocation authority", () => {
  const status = { ready: true, source: "postgresql", metadata: { database: "postgresql", phase4_authority: "unavailable", phase4_shared_allocation_id: null } };
  const snapshot = { scope: "opportunities", status, portfolio_integrated: null, tables: { opportunities_ranked: { rows: [{ ticker: "AAA", research_rank: 1 }], count: 806 } } };
  it("accepts explicit unavailable authority through both production merge stages and clears old actions", () => {
    const existing = { ...emptyPanelData(), portfolioAllocation: { rows: [{ allocation_id: "old" }] }, portfolioAllocationItems: { rows: [{ action: "BUY" }] }, paperExecutionObservations: { rows: [{ order_id: "old" }] }, bookAttribution: { rows: [{ allocation_id: "old" }] } };
    for (const first of [emptyPanelData(), existing]) {
      const incoming = mergeSnapshot(first, snapshot);
      const result = mergePanelData(existing, incoming);
      expect(result.opportunitiesRanked.count).toBe(806);
      expect(result.opportunitiesRanked.rows?.[0].ticker).toBe("AAA");
      expect(result.portfolioAllocation).toBeUndefined();
      expect(result.portfolioAllocationItems).toBeUndefined();
      expect(result.paperExecutionObservations).toBeUndefined();
      expect(result.bookAttribution).toBeUndefined();
      expect(result.portfolioIntegrated).toBeUndefined();
      expect(result.scopeStatus.opportunities.state).toBe("ready");
    }
  });
  it.each([
    { ...snapshot, status: { ready: true } },
    { ...snapshot, status: { ...status, metadata: { ...status.metadata, snapshot_error: "unavailable" } } },
    { ...snapshot, tables: { ...snapshot.tables, portfolio_allocation_items: { rows: [{ action: "BUY" }] } } },
    { ...snapshot, tables: { ...snapshot.tables, portfolio_allocation: { rows: [{ allocation_id: "contradiction" }] } } },
  ])("rejects missing, failed, or contradictory authority", (invalid) => {
    for (const existing of [emptyPanelData(), mergeSnapshot(emptyPanelData(), snapshot)]) {
      const result = mergePanelData(existing, mergeSnapshot(existing, invalid));
      expect(result.errors.portfolio).toBeTruthy();
      expect(result.scopeStatus.opportunities.state).toBe("failed");
    }
  });
});
