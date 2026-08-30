import { describe, expect, it } from "vitest";

import type { PanelData } from "@/types";
import { buildWatchlistViewModel, type WatchlistFilters } from "./watchlist";

const filters: WatchlistFilters = {
  query: "",
  minRating: 0,
  maxForwardPe: null,
  minRoic: null,
  sort: "rank",
};

describe("watchlist portfolio impact", () => {
  it("keeps the exact impact for the selected expression kind", () => {
    const stockImpact = { impact_id: "impact-stock", scenario_pnl: { market_down_20: -1200 } };
    const cashImpact = { impact_id: "impact-cash", scenario_pnl: { market_down_20: 0 } };
    const data = {
      watchlistWatched: { rows: [{ symbol: "NVDA", watch_state: "watched" }], count: 1 },
      tickerDecisions: { rows: [{
        ticker: "NVDA",
        selected_expression: { kind: "STOCK" },
        portfolio_impacts: { STOCK: { impact_id: "stale-impact" } },
      }], count: 1 },
      watchlistWatchedTickerDecisions: { rows: [{
        ticker: "NVDA",
        selected_expression: { kind: "STOCK" },
        portfolio_impacts: { STOCK: stockImpact, CASH: cashImpact },
      }], count: 1 },
    } as unknown as PanelData;

    const row = buildWatchlistViewModel(data, filters, {}).rows[0];

    expect(row?.portfolioImpact).toBe(stockImpact);
    expect(row?.portfolioImpact).not.toBe(cashImpact);
  });

  it("does not substitute an impact from another expression kind", () => {
    const data = {
      watchlistUnwatched: { rows: [{ symbol: "AMD", watch_state: "candidate" }], count: 1 },
      watchlistUnwatchedTickerDecisions: { rows: [{
        ticker: "AMD",
        selected_expression: { kind: "STOCK" },
        portfolio_impacts: { CASH: { impact_id: "impact-cash" } },
      }], count: 1 },
    } as unknown as PanelData;

    expect(buildWatchlistViewModel(data, filters, {}).rows[0]?.portfolioImpact).toBeUndefined();
  });
});
