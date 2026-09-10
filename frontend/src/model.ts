import type { components } from "@/generated/apiSchema";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { textField } from "@/shared/rowFormat";

type PortfolioHolding = components["schemas"]["PortfolioHoldingDTO"];
type PortfolioSummaryDTO = components["schemas"]["PortfolioSummaryDTO"];

export type Holding = {
  ticker: string;
  quantity: number;
  price: number | null;
  averageCost: number | null;
  marketValue: number | null;
  hasMarketValue: boolean;
  weight: number | null;
  unrealizedPnl: number | null;
  unrealizedPnlPct: number | null;
  dayChange: number | null;
  dayChangePct: number | null;
  quoteObservedAt: string;
  quoteAvailableAt: string;
  availableAt: string;
  quoteSource: string;
  quoteSourceKind: string;
  quoteTradingDate: string;
  currency: string;
  valuationAvailable: boolean;
  valuationStatus: string;
  nextStep: string;
};

export type AppModel = {
  holdings: Holding[];
  thesisMonitorRows: RowRecord[];
  portfolioValue: number | null;
  portfolioSummary: PortfolioSummaryDTO | null;
  latestHealthCheck: string;
  sources: {
    watchlist: "live" | "empty";
    opportunities: "live" | "empty";
    holdings: "live" | "empty";
    filings: "live" | "empty";
    calendar: "live" | "empty";
    health: "live" | "empty";
  };
};

export function buildModel(data: PanelData): AppModel {
  const quoteRows = [...rows(data.quotes), ...rows(data.watchlistWatchedQuotes), ...rows(data.watchlistUnwatchedQuotes)];
  const holdings = (data.portfolioHoldings ?? []).map(toHolding);
  const portfolioSummary = data.portfolioSummaryDto ?? null;
  const summaryValue = portfolioSummary?.portfolio_value;
  const portfolioValue = typeof summaryValue === "number" && Number.isFinite(summaryValue)
    ? summaryValue
    : null;
  const healthRows = [
    ...rows(data.sourceFreshness),
    ...rows(data.sourceHealth),
    ...rows(data.providerRuns),
    ...rows(data.brokerStatus),
  ];

  return {
    holdings,
    thesisMonitorRows: rows(data.thesisMonitor),
    portfolioValue,
    portfolioSummary,
    latestHealthCheck: newestDateLabel(healthRows.map((row) => textField(row, ["checked_at", "last_run_at", "as_of", "updated_at", "timestamp"]))),
    sources: {
      watchlist: quoteRows.length || rows(data.watchlistWatched).length || rows(data.watchlistUnwatched).length ? "live" : "empty",
      opportunities: rows(data.decisionQueue).length || rows(data.opportunitiesRanked).length ? "live" : "empty",
      holdings: holdings.length ? "live" : "empty",
      filings: rows(data.disclosures).length ? "live" : "empty",
      calendar: rows(data.catalysts).length || rows(data.earnings).length ? "live" : "empty",
      health: healthRows.length ? "live" : "empty",
    },
  };
}

function toHolding(row: PortfolioHolding): Holding {
  const ticker = row.symbol.trim().toUpperCase();
  return {
    ticker,
    quantity: row.quantity,
    price: row.price ?? null,
    averageCost: row.average_cost ?? null,
    marketValue: row.market_value ?? null,
    hasMarketValue: row.valuation_available && row.market_value != null,
    weight: row.portfolio_weight ?? null,
    unrealizedPnl: row.unrealized_pnl ?? null,
    unrealizedPnlPct: row.unrealized_pnl_pct ?? null,
    dayChange: row.day_change ?? null,
    dayChangePct: row.day_change_pct ?? null,
    quoteObservedAt: row.quote_observed_at ?? "",
    quoteAvailableAt: row.quote_available_at ?? "",
    availableAt: row.available_at ?? "",
    quoteSource: row.quote_source ?? "",
    quoteSourceKind: row.quote_source_kind ?? "",
    quoteTradingDate: row.quote_trading_date ?? "",
    currency: row.currency,
    valuationAvailable: row.valuation_available,
    valuationStatus: row.valuation_status,
    nextStep: row.next_step,
  };
}

function newestDateLabel(values: string[]): string {
  const latest = values
    .map((value) => new Date(value))
    .filter((value) => !Number.isNaN(value.getTime()))
    .sort((a, b) => b.getTime() - a.getTime())[0];
  return latest ? latest.toLocaleString() : "Not loaded";
}
