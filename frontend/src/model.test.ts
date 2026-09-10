import { describe, expect, it } from "vitest";

import type { components } from "@/generated/apiSchema";
import { emptyPanelData } from "@/apiPanelData";
import { buildModel } from "@/model";
import type { PanelData } from "@/types";

type PortfolioHolding = components["schemas"]["PortfolioHoldingDTO"];

function holding(overrides: Partial<PortfolioHolding> = {}): PortfolioHolding {
  return {
    symbol: "AAA",
    quantity: 10,
    currency: "USD",
    next_step: "Review sizing, thesis, and latest evidence.",
    valuation_available: true,
    valuation_status: "market_quote",
    ...overrides,
  };
}

describe("canonical portfolio model", () => {
  it("uses backend valuation fields instead of conflicting quote rows", () => {
    const data = { ...emptyPanelData(), quotes: { rows: [{ symbol: "AAA", price: 999 }] }, portfolioHoldings: [holding({ price: 12, market_value: 120, portfolio_weight: 100 })], portfolioSummaryDto: { portfolio_value: 120, availability: "complete", valuation_coverage: 1, valuation_blockers: [], cost_basis_fallback_count: 0, day_pnl_status: "ready", holdings_count: 1, missing_valuation_count: 0, performance_method: "test", valuation_status: "market_quotes", valued_position_count: 1 } } as unknown as PanelData;
    const model = buildModel(data);

    expect(model.holdings[0]).toMatchObject({ ticker: "AAA", price: 12, marketValue: 120, weight: 100 });
    expect(model.portfolioValue).toBe(120);
  });

  it("does not reconstruct a complete value from partial holdings", () => {
    const data = { ...emptyPanelData(), portfolioHoldings: [holding({ market_value: 120 }), holding({ symbol: "BBB", market_value: null, valuation_available: false })] } as PanelData;

    expect(buildModel(data).portfolioValue).toBeNull();
  });

  it("preserves zero and signed values and does not fabricate unavailable values", () => {
    const data = {
      ...emptyPanelData(),
      portfolioHoldings: [
        holding({ symbol: "ZERO", quantity: 0, price: 0, market_value: 0, portfolio_weight: 0 }),
        holding({ symbol: "SHORT", quantity: -1, price: 50, market_value: -50, portfolio_weight: -50 }),
        holding({ symbol: "STALE", price: null, valuation_price: null, market_value: null, portfolio_weight: null, valuation_available: false, valuation_status: "stale_quote" }),
      ],
    } as PanelData;
    const model = buildModel(data);

    expect(model.holdings.map((row) => row.marketValue)).toEqual([0, -50, null]);
    expect(model.holdings.map((row) => row.hasMarketValue)).toEqual([true, true, false]);
    expect(model.holdings[2]).toMatchObject({ price: null, weight: null, unrealizedPnl: null });
  });
});
