import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";

export type Holding = {
  ticker: string;
  quantity: number;
  price: number;
  marketValue: number;
  hasMarketValue: boolean;
  weight: number;
  unrealizedPnl: number;
  nextStep: string;
};

export type AppModel = {
  holdings: Holding[];
  thesisMonitorRows: RowRecord[];
  portfolioValue: number;
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
  const holdings = buildHoldings(rows(data.portfolio), quoteRows);
  const portfolioValue = holdings.reduce((total, holding) => total + holding.marketValue, 0);
  const weightedHoldings = holdings.map((holding) => ({
    ...holding,
    weight: portfolioValue ? (holding.marketValue / portfolioValue) * 100 : 0,
  }));
  const healthRows = [
    ...rows(data.sourceFreshness),
    ...rows(data.sourceHealth),
    ...rows(data.providerRuns),
    ...rows(data.brokerStatus),
  ];

  return {
    holdings: weightedHoldings,
    thesisMonitorRows: rows(data.thesisMonitor),
    portfolioValue,
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

function buildHoldings(portfolioRows: RowRecord[], quoteRows: RowRecord[]): Holding[] {
  const prices = new Map<string, number>();
  for (const row of quoteRows) {
    const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
    const price = numberField(row, ["price", "close", "regular_market_price", "last"]);
    if (symbol && Number.isFinite(price) && price > 0) prices.set(symbol, price);
  }

  return portfolioRows.map((row) => {
    const ticker = textField(row, ["symbol", "ticker", "security"], "UNKNOWN").toUpperCase();
    const quantity = numberField(row, ["quantity", "shares", "position", "units"], 0);
    const explicitPrice = numberField(row, ["price", "latest_price", "market_price"], Number.NaN);
    const price = Number.isFinite(explicitPrice) && explicitPrice > 0 ? explicitPrice : prices.get(ticker) ?? 0;
    const explicitValue = numberField(row, ["market_value", "value"], Number.NaN);
    const marketValue = Number.isFinite(explicitValue) && explicitValue > 0 ? explicitValue : quantity * price;
    const costBasis = numberField(row, ["cost_basis", "average_cost", "avg_cost"], Number.NaN);
    const unrealizedPnl = Number.isFinite(costBasis) && quantity ? (price - costBasis) * quantity : numberField(row, ["unrealized_pnl", "pnl"], 0);
    const nextStep = textField(row, ["next_step", "review_reason", "status"], "Review sizing, thesis, and latest evidence.");
    return {
      ticker,
      quantity,
      price,
      marketValue,
      hasMarketValue: marketValue > 0,
      weight: 0,
      unrealizedPnl,
      nextStep,
    };
  });
}

function newestDateLabel(values: string[]): string {
  const latest = values
    .map((value) => new Date(value))
    .filter((value) => !Number.isNaN(value.getTime()))
    .sort((a, b) => b.getTime() - a.getTime())[0];
  return latest ? latest.toLocaleString() : "Not loaded";
}
