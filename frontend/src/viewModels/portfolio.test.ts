import { describe, expect, it } from "vitest";

import type { AppModel } from "@/model";
import type { PanelData } from "@/types";
import { buildPortfolioViewModel, performanceRangeRows } from "@/viewModels/portfolio";

const model = {
  holdings: [{ ticker: "NVDA", quantity: 2, price: 120, averageCost: 100, marketValue: 240, hasMarketValue: true, weight: 60, unrealizedPnl: 40, unrealizedPnlPct: 20, dayChange: 4, dayChangePct: 1.7, quoteObservedAt: "2026-07-15T20:00:00Z", valuationStatus: "market_quote", nextStep: "Review sizing" }],
  portfolioValue: 240,
  thesisMonitorRows: [],
  latestHealthCheck: "Not loaded",
  sources: { watchlist: "empty", opportunities: "empty", holdings: "live", filings: "empty", calendar: "empty", health: "empty" },
} satisfies AppModel;

describe("portfolio view model", () => {
  it("maps the reconciled summary and keeps one correlation window", () => {
    const data = {
      portfolioSummary: { count: 1, rows: [{ portfolio_value: 240, total_pnl: 40, total_pnl_pct: 20, day_pnl: 4, day_pnl_pct: 1.7, as_of: "2026-07-15T20:00:00Z" }] },
      portfolioPerformance: { count: 2, rows: [{ date: "2026-07-14", total_pnl: 30 }, { date: "2026-07-15", total_pnl: 40 }] },
      portfolioTransactions: { count: 1, rows: [{ id: "trade-1", symbol: "NVDA", transaction_type: "buy" }] },
      correlationEdges: { count: 2, rows: [
        { edge_id: "NVDA:MSFT:20", lookback_days: 20, correlation: 0.5 },
        { edge_id: "NVDA:MSFT:60", lookback_days: 60, correlation: 0.8 },
      ] },
      portfolioRiskCards: { count: 0, rows: [] },
      reviewActions: { count: 0, rows: [] },
      exposureClusters: { count: 0, rows: [] },
    } as unknown as PanelData;

    const viewModel = buildPortfolioViewModel(data, model, 60);
    expect(viewModel.summary.portfolioValue).toBe(240);
    expect(viewModel.summary.totalPnl).toBe(40);
    expect(viewModel.correlationRows).toHaveLength(1);
    expect(viewModel.correlationRows[0]?.correlation).toBe(0.8);
    expect(viewModel.transactionRows[0]?.symbol).toBe("NVDA");
  });

  it("clips performance rows to a selected calendar range", () => {
    const rows = [
      { date: "2025-07-14" },
      { date: "2026-06-15" },
      { date: "2026-07-15" },
    ];
    expect(performanceRangeRows(rows, "1M").map((row) => row.date)).toEqual(["2026-06-15", "2026-07-15"]);
    expect(performanceRangeRows(rows, "ALL")).toHaveLength(3);
  });

  it("keeps January 1 in YTD regardless of local timezone", () => {
    const rows = [{ date: "2026-01-01" }, { date: "2026-07-15" }];
    expect(performanceRangeRows(rows, "YTD").map((row) => row.date)).toEqual(["2026-01-01", "2026-07-15"]);
  });

  it("preserves an undefined total return", () => {
    const data = {
      portfolioSummary: { count: 1, rows: [{ total_pnl: -10, total_pnl_pct: null }] },
    } as unknown as PanelData;

    expect(buildPortfolioViewModel(data, model).summary.totalPnlPct).toBeNull();
  });

  it("keeps a compact held-ticker impact for the selected expression kind", () => {
    const selectedImpact = { impact_id: "impact-stock", expression_kind: "STOCK", availability: "unavailable", marginal_risk: 0.2, blockers: ["stale_quote"], scenario_pnl: { market_down_20: -1200 } };
    const otherImpact = { impact_id: "impact-cash", scenario_pnl: { market_down_20: 0 } };
    const data = {
      tickerDecisions: { count: 1, rows: [{
        ticker: "NVDA",
        selected_expression: { kind: "STOCK" },
        portfolio_impacts: { STOCK: selectedImpact, CASH: otherImpact },
      }] },
    } as unknown as PanelData;

    const impacts = buildPortfolioViewModel(data, model).proposedImpacts;

    expect(impacts).toHaveLength(1);
    expect(impacts[0]).toMatchObject({ ticker: "NVDA", expression_kind: "STOCK", availability: "unavailable", marginal_risk: 0.2, blockers: ["stale_quote"] });
    expect(impacts[0]).not.toHaveProperty("scenario_pnl");
    expect(impacts[0]).not.toBe(otherImpact);
  });
});
