import { describe, expect, it } from "vitest";

import { emptyPanelData, mergePanelData } from "./apiPanelData";

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
});
