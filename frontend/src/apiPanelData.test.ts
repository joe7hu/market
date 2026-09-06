import { describe, expect, it } from "vitest";

import { emptyPanelData, mergeSnapshot } from "./apiPanelData";

describe("mergeSnapshot", () => {
  it("retains distinct opportunities for the same ticker across pages", () => {
    const first = mergeSnapshot(emptyPanelData(), { tables: { opportunities_ranked: { rows: [{ ticker: "AAA", opportunity_episode_id: "first" }], count: 2 } } });
    const result = mergeSnapshot(first, { tables: { opportunities_ranked: { rows: [{ ticker: "AAA", opportunity_episode_id: "first" }, { ticker: "AAA", opportunity_episode_id: "second" }], count: 2 } } }, { append: true });
    expect(result.opportunitiesRanked.rows?.map((row) => row.opportunity_episode_id)).toEqual(["first", "second"]);
  });

  it("keeps independently loaded scopes and appends paged rows", () => {
    const first = mergeSnapshot(emptyPanelData(), { scope: "market", tables: { quotes: { rows: [{ ticker: "AAA" }], count: 1 } } });
    const result = mergeSnapshot(first, { scope: "calendar", tables: { quotes: { rows: [{ ticker: "CCC" }], count: 2 } } }, { append: true });
    expect(result.quotes.rows).toEqual([{ ticker: "AAA" }, { ticker: "CCC" }]);
    expect(result.scopeStatus.market.state).toBe("ready");
    expect(result.scopeStatus.calendar.state).toBe("ready");
  });

  it("clears omitted Phase 4 objects when a scoped response rolls to a new identity", () => {
    const existing = mergeSnapshot(emptyPanelData(), {
      scope: "portfolio",
      tables: {
        portfolio_allocation: { rows: [{ allocation_id: "allocation:old" }] },
        portfolio_allocation_items: { rows: [{ action: "BUY" }] },
        paper_execution_observations: { rows: [{ order_id: "old" }] },
        book_attribution: { rows: [{ allocation_id: "allocation:old" }] },
        portfolio_scenario_artifact: { rows: [{ allocation_id: "allocation:old", scenario_artifact_id: "scenario:old" }] },
        execution_model_snapshot: { rows: [{ allocation_id: "allocation:old", execution_model_snapshot_id: "execution:old" }] },
      },
    });
    const next = mergeSnapshot(existing, { scope: "portfolio", tables: { portfolio_allocation: { rows: [{ allocation_id: "allocation:new" }] } } });
    expect(next.portfolioAllocation.rows?.[0].allocation_id).toBe("allocation:new");
    for (const key of ["portfolioAllocationItems", "portfolioScenarioArtifact", "executionModelSnapshot", "portfolioIntegrated", "paperExecutionObservations", "bookAttribution"]) expect(next[key]).toBeUndefined();
  });

  it("retains tables explicitly deferred by the bounded dashboard", () => {
    const existing = mergeSnapshot(emptyPanelData(), { tables: { fundamentals: { rows: [{ symbol: "AAA", value: 1 }], count: 1 } } });
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
  it("accepts explicit unavailable authority at the snapshot interface and clears old actions", () => {
    const existing = { ...emptyPanelData(), portfolioAllocation: { rows: [{ allocation_id: "old" }] }, portfolioAllocationItems: { rows: [{ action: "BUY" }] }, paperExecutionObservations: { rows: [{ order_id: "old" }] }, bookAttribution: { rows: [{ allocation_id: "old" }] } };
    for (const first of [emptyPanelData(), existing]) {
      const result = mergeSnapshot(first, snapshot);
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
      const result = mergeSnapshot(existing, invalid);
      expect(result.errors.portfolio).toBeTruthy();
      expect(result.scopeStatus.opportunities.state).toBe("failed");
    }
  });
});

it("recovers the current response after historical failures without retaining allocation actions", () => {
  const valid = mergeSnapshot(emptyPanelData(), {
    scope: "portfolio",
    tables: { portfolio_allocation: { rows: [{ allocation_id: "old" }] }, portfolio_allocation_items: { rows: [{ action: "BUY" }] } },
  });
  const failed = mergeSnapshot(valid, {
    scope: "today", status: { ready: false, metadata: { snapshot_error: "timeout" } },
  });
  expect(failed.scopeStatus.today.state).toBe("failed");
  expect(failed.portfolioAllocationItems.rows?.[0].action).toBe("BUY");
  failed.scopeStatus.research = { state: "stale" };
  const result = mergeSnapshot(failed, {
    scope: "opportunities",
    status: { ready: true, source: "postgresql", metadata: { database: "postgresql", phase4_authority: "unavailable", phase4_shared_allocation_id: null } },
    portfolio_integrated: null,
    tables: { opportunities_ranked: { rows: [{ ticker: "AAA" }], count: 806 } },
  });
  expect(result.scopeStatus.today.state).toBe("failed");
  expect(result.scopeStatus.research.state).toBe("stale");
  expect(result.scopeStatus.opportunities.state).toBe("ready");
  expect(result.opportunitiesRanked.count).toBe(806);
  expect(result.portfolioAllocation).toBeUndefined();
  expect(result.portfolioAllocationItems).toBeUndefined();
  expect(result.errors.portfolio).toBeUndefined();
});
