import type { AppModel } from "@/model";
import type { components } from "@/generated/apiSchema";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { listField, numberField, textField } from "@/shared/rowFormat";

export type PerformanceRange = "1D" | "1W" | "1M" | "YTD" | "1Y" | "ALL";
type PortfolioImpact = components["schemas"]["TickerPortfolioImpactSummaryResponse"];

type ProposedPortfolioImpact = PortfolioImpact & { ticker: string };

export type PortfolioSummary = {
  portfolioValue: number | null;
  availability: "complete" | "partial" | "unavailable";
  knownValueSubtotal: number | null;
  valuationCoverage: number;
  valuationBlockers: string[];
  currency: string;
  dayPnl: number | null;
  dayPnlPct: number | null;
  dayPnlAsOf: string;
  totalPnl: number | null;
  totalPnlPct: number | null;
  realizedPnl: number | null;
  income: number | null;
  fees: number | null;
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
  proposedImpacts: ProposedPortfolioImpact[];
  topHolding: AppModel["holdings"][number] | undefined;
};

export function buildPortfolioViewModel(data: PanelData, model: AppModel, correlationWindow = 60): PortfolioViewModel {
  const summaryDto = data.portfolioSummaryDto;
  const decisions = new Map(rows(data.tickerDecisions).map((row) => [textField(row, ["ticker", "symbol"]).toUpperCase(), row]));
  return {
    summary: {
      portfolioValue: summaryDto?.portfolio_value ?? null,
      availability: summaryDto?.availability ?? "unavailable",
      knownValueSubtotal: summaryDto?.known_value_subtotal ?? null,
      valuationCoverage: summaryDto?.valuation_coverage ?? 0,
      valuationBlockers: summaryDto?.valuation_blockers ?? ["portfolio_summary_unavailable"],
      currency: summaryDto?.currency ?? "Unknown",
      dayPnl: summaryDto?.day_pnl ?? null,
      dayPnlPct: summaryDto?.day_pnl_pct ?? null,
      dayPnlAsOf: summaryDto?.day_pnl_as_of ?? "",
      totalPnl: summaryDto?.total_pnl ?? null,
      totalPnlPct: summaryDto?.total_pnl_pct ?? null,
      realizedPnl: summaryDto?.realized_pnl ?? null,
      income: summaryDto?.income ?? null,
      fees: summaryDto?.fees ?? null,
      asOf: summaryDto?.as_of ?? "",
      performanceMethod: summaryDto?.performance_method ?? "Unavailable",
      costBasisFallbackCount: summaryDto?.cost_basis_fallback_count ?? 0,
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
    proposedImpacts: model.holdings.flatMap((holding) => {
      const impact = selectedPortfolioImpact(decisions.get(holding.ticker.toUpperCase()));
      return impact ? [{ ticker: holding.ticker, ...impact }] : [];
    }),
    topHolding: model.holdings.slice().sort((a, b) => (b.weight ?? -Infinity) - (a.weight ?? -Infinity))[0],
  };
}

function selectedPortfolioImpact(decision: RowRecord | undefined): PortfolioImpact | undefined {
  const selected = decision?.selected_expression;
  const impacts = decision?.portfolio_impacts;
  if (!isRecord(selected) || !isRecord(impacts) || typeof selected.kind !== "string" || selected.kind.toUpperCase() === "CASH") return undefined;
  const impact = impacts[selected.kind];
  if (!isRecord(impact)) return undefined;
  return {
    expression_kind: String(impact.expression_kind || selected.kind) as PortfolioImpact["expression_kind"],
    availability: typeof impact.availability === "string" ? impact.availability : "unavailable",
    marginal_risk: typeof impact.marginal_risk === "number" ? impact.marginal_risk : null,
    risk_budget_consumed: typeof impact.risk_budget_consumed === "number" ? impact.risk_budget_consumed : null,
    blockers: Array.isArray(impact.blockers) ? impact.blockers.map(String) : [],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
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
