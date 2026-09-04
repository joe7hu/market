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
});
