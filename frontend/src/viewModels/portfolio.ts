import type { AppModel } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";

export type PerformanceRange = "1D" | "1W" | "1M" | "YTD" | "1Y" | "ALL";

export type PortfolioSummary = {
  portfolioValue: number;
  dayPnl: number | null;
  dayPnlPct: number | null;
  dayPnlAsOf: string;
  totalPnl: number;
  totalPnlPct: number | null;
  realizedPnl: number;
  income: number;
  fees: number;
  asOf: string;
  performanceMethod: string;
  costBasisFallbackCount: number;
};

export type CorrelationEdge = {
  id: string;
  symbol: string;
  peerSymbol: string;
  lookbackDays: number;
  observations: number;
  correlation: number | null;
  combinedWeight: number;
  riskLevel: string;
  asOf: string;
  dataStatus: string;
  interpretation: string;
};

export type PortfolioViewModel = {
  summary: PortfolioSummary;
  performanceRows: RowRecord[];
  transactionRows: RowRecord[];
  correlationRows: CorrelationEdge[];
  riskRows: RowRecord[];
  reviewRows: RowRecord[];
  exposureClusterRows: RowRecord[];
  topHolding: AppModel["holdings"][number] | undefined;
};

export function buildPortfolioViewModel(data: PanelData, model: AppModel, correlationWindow = 60): PortfolioViewModel {
  const summaryRow = rows(data.portfolioSummary)[0] ?? {};
  return {
    summary: {
      portfolioValue: numberField(summaryRow, ["portfolio_value"], model.portfolioValue),
      dayPnl: nullableNumberField(summaryRow, "day_pnl"),
      dayPnlPct: nullableNumberField(summaryRow, "day_pnl_pct"),
      dayPnlAsOf: textField(summaryRow, ["day_pnl_as_of"]),
      totalPnl: numberField(summaryRow, ["total_pnl"]),
      totalPnlPct: nullableNumberField(summaryRow, "total_pnl_pct"),
      realizedPnl: numberField(summaryRow, ["realized_pnl"]),
      income: numberField(summaryRow, ["income"]),
      fees: numberField(summaryRow, ["fees"]),
      asOf: textField(summaryRow, ["as_of"]),
      performanceMethod: textField(summaryRow, ["performance_method"], "daily-close external-flow adjusted"),
      costBasisFallbackCount: numberField(summaryRow, ["cost_basis_fallback_count"]),
    },
    performanceRows: rows(data.portfolioPerformance),
    transactionRows: rows(data.portfolioTransactions),
    correlationRows: rows(data.correlationEdges)
      .map((row) => ({
        id: textField(row, ["edge_id"]),
        symbol: textField(row, ["symbol"]),
        peerSymbol: textField(row, ["peer_symbol"]),
        lookbackDays: numberField(row, ["lookback_days"]),
        observations: numberField(row, ["observations"]),
        correlation: row.correlation === null || row.correlation === undefined ? null : numberField(row, ["correlation"]),
        combinedWeight: numberField(row, ["combined_weight"]),
        riskLevel: textField(row, ["risk_level"], "context"),
        asOf: textField(row, ["as_of"]),
        dataStatus: textField(row, ["data_status"], "insufficient_history"),
        interpretation: textField(row, ["interpretation"]),
      }))
      .filter((row) => row.lookbackDays === correlationWindow),
    riskRows: rows(data.portfolioRiskCards),
    reviewRows: rows(data.reviewActions),
    exposureClusterRows: rows(data.exposureClusters),
    topHolding: model.holdings.slice().sort((a, b) => b.weight - a.weight)[0],
  };
}

export function performanceRangeRows(input: RowRecord[], range: PerformanceRange): RowRecord[] {
  if (range === "ALL" || input.length < 2) return input;
  const dated = input
    .map((row) => ({ row, date: new Date(textField(row, ["date"])) }))
    .filter((item) => !Number.isNaN(item.date.getTime()))
    .sort((left, right) => left.date.getTime() - right.date.getTime());
  const latest = dated.at(-1)?.date;
  if (!latest) return input;
  if (range === "1D") return dated.slice(-2).map((item) => item.row);
  const threshold = new Date(latest);
  if (range === "YTD") {
    threshold.setUTCMonth(0, 1);
  } else {
    threshold.setUTCDate(threshold.getUTCDate() - ({ "1W": 7, "1M": 30, "1Y": 365 }[range] ?? 0));
  }
  threshold.setUTCHours(0, 0, 0, 0);
  return dated.filter((item) => item.date >= threshold).map((item) => item.row);
}

function nullableNumberField(row: RowRecord, key: string): number | null {
  return row[key] === null || row[key] === undefined ? null : numberField(row, [key]);
}
