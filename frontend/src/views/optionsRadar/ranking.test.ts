import { describe, expect, it } from "vitest";

import {
  compareGroupedOpportunities,
  compareTodayOptionActions,
} from "./helpers";

describe("options publication ranking", () => {
  it("uses research rank for Radar and keeps ties deterministic", () => {
    const rows = [
      { ticker: "ZZZ", research_rank: 2, score: 1000 },
      { ticker: "AAA", research_rank: 1, score: 1 },
      { ticker: "BBB", research_rank: 2, score: 9999 },
    ];

    expect([...rows].sort(compareGroupedOpportunities).map((row) => row.ticker)).toEqual([
      "AAA", "BBB", "ZZZ",
    ]);
  });

  it("does not synthesize Radar rank from legacy score fields", () => {
    const ranked = { ticker: "AAA", research_rank: 2, score: 1 };
    const legacyOnly = { ticker: "BBB", score: 9999, rank_score: 9999, risk_adjusted_expectancy: 9999 };

    expect(compareGroupedOpportunities(ranked, legacyOnly)).toBeLessThan(0);
  });

  it("uses trade rank only for actionable Today ordering", () => {
    const rows = [
      { ticker: "AAA", research_rank: 1, trade_rank: 2, score: 9999 },
      { ticker: "BBB", research_rank: 2, trade_rank: 1, score: 1 },
      { ticker: "CCC", research_rank: 3, score: 100000 },
    ];

    expect([...rows].sort(compareTodayOptionActions).map((row) => row.ticker)).toEqual([
      "BBB", "AAA", "CCC",
    ]);
  });
});
