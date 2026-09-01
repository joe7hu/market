import { describe, expect, it } from "vitest";

import { dedupeOpportunityEpisodes, EXPRESSION_KINDS, shouldLoadScreener } from "./opportunities";

describe("opportunity decision surface", () => {
  it("keeps one row per episode while preserving the first published row", () => {
    const rows = dedupeOpportunityEpisodes([
      { episode_id: "ep-1", symbol: "NVDA", horizon: "TACTICAL" },
      { episode_id: "ep-1", symbol: "NVDA", horizon: "TACTICAL", action: "BUY" },
      { episode_id: "ep-2", symbol: "MSFT", horizon: "FUNDAMENTAL" },
    ]);

    expect(rows).toHaveLength(2);
    expect(rows[0]?.action).toBeUndefined();
    expect(rows.map((row) => row.episode_id)).toEqual(["ep-1", "ep-2"]);
  });

  it("defines the complete expression comparison contract", () => {
    expect(EXPRESSION_KINDS).toEqual(["stock", "option/spread", "CSP", "crypto", "hedge", "cash"]);
  });

  it("loads the selected screener once even when shared data has rows", () => {
    expect(shouldLoadScreener("screener", false)).toBe(true);
    expect(shouldLoadScreener("screener", true)).toBe(false);
    expect(shouldLoadScreener("episodes", false)).toBe(false);
  });
});
