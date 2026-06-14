import type { JsonValue, RowRecord, TickerPayload } from "@/types";
import { displayField, formatPct, textField, toneFromText } from "@/views/rowFormat";
import type { MetricSpec } from "@/views/workspacePage";

export type MetricCell = [label: string, value: string, detail: string];

export function presentMetricCells(rows: MetricCell[]): MetricCell[] {
  return rows.filter(([, value]) => value !== "-");
}

export function compactRows(sectionRows: RowRecord[] | undefined): RowRecord[] {
  return (sectionRows ?? [])
    .map((row) => Object.fromEntries(Object.entries(row).filter(([, value]) => !isEmptyCell(value))) as RowRecord)
    .filter((row) => Object.keys(row).length > 0);
}

export function isEmptyCell(value: RowRecord[string]): boolean {
  if (value === undefined || value === null || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === "object" && Object.keys(value).length === 0;
}

export function tickerHeaderMetrics(ticker: TickerPayload | null): MetricSpec[] {
  const tables = ticker?.tables ?? {};
  const quote = latestRow(compactRows(tables.quotes), ["observed_at", "as_of", "date"]);
  const decision = compactRows(tables.decision_queue)[0] ?? compactRows(tables.symbol_decision_snapshot)[0];
  const fundamentals = compactRows(tables.fundamentals)
    .filter((row) => textField(row, ["source"]) === "sec_companyfacts")
    .sort((a, b) => dateSortValue(b, ["filing_date", "period_end"]) - dateSortValue(a, ["filing_date", "period_end"]))[0];
  const metrics = objectField(fundamentals, "metrics");
  const sourceCount = compactRows(tables.source_consensus).length + compactRows(tables.ticker_source_signals).length;
  return [
    ["Price", moneyMetric(quote, "price"), displayField(quote, ["observed_at", "as_of"], "No quote timestamp"), toneFromText(displayField(quote, ["freshness_status"], "loaded"))],
    ["Action", displayField(decision, ["action_grade", "decision_bucket", "decision"], "Not loaded"), displayField(decision, ["freshness_status", "overall_decision_freshness"], "No decision freshness"), toneFromText(displayField(decision, ["action_grade", "freshness_status"], "info"))],
    ["Revenue YoY", ratioMetric(metrics, "revenue_growth"), "SEC company facts", toneFromText(ratioTone(numberFrom(metrics.revenue_growth)))],
    ["Sources", sourceCount ? String(sourceCount) : "0", "consensus and ticker signals", sourceCount ? "good" : "warn"],
  ];
}

export function objectField(row: RowRecord | undefined, key: string): RowRecord {
  const value = row?.[key];
  if (value && typeof value === "object" && !Array.isArray(value)) return value as RowRecord;
  if (typeof value === "string" && value.trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(value) as JsonValue;
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as RowRecord : {};
    } catch {
      return {};
    }
  }
  return {};
}

export function arrayField(row: RowRecord | undefined, key: string): RowRecord[] {
  const value = row?.[key];
  if (Array.isArray(value)) return value.filter((item) => Boolean(item) && typeof item === "object" && !Array.isArray(item)) as RowRecord[];
  return [];
}

export function latestRow(rows: RowRecord[], keys: string[]): RowRecord | undefined {
  return rows.length ? [...rows].sort((a, b) => dateSortValue(b, keys) - dateSortValue(a, keys))[0] : undefined;
}

export function dateSortValue(row: RowRecord | undefined, keys: string[]): number {
  if (!row) return 0;
  for (const key of keys) {
    const value = row[key];
    if (!value) continue;
    const date = new Date(String(value));
    if (!Number.isNaN(date.getTime())) return date.getTime();
  }
  return 0;
}

export function estimateForPeriod(rows: RowRecord[], period: string): RowRecord {
  return rows.find((row) => textField(row, ["period"]) === period) ?? {};
}

export function moneyMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : formatCompactMoney(value);
}

export function ratioMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : formatPct(value * 100);
}

export function percentMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : formatPct(value);
}

export function multipleMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}x`;
}

export function numberMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function targetRange(row: RowRecord | undefined): string {
  const low = moneyMetric(row, "low");
  const high = moneyMetric(row, "high");
  return low === "-" && high === "-" ? "-" : `${low} / ${high}`;
}

export function optionMove(row: RowRecord | undefined): string {
  const move = numberFrom(row?.expected_move);
  const pct = numberFrom(row?.expected_move_pct);
  if (move === null && pct === null) return "-";
  const parts = [];
  if (move !== null) parts.push(formatCompactMoney(move));
  if (pct !== null) parts.push(formatPct(pct * 100));
  return parts.join(" / ");
}

export function skewDetail(row: RowRecord | undefined): string {
  const skew = numberFrom(row?.put_call_iv_skew);
  return skew === null ? "25-delta skew unavailable" : `${formatPct(skew * 100)} put-call IV`;
}

export function liquidityDetail(row: RowRecord | undefined): string {
  const score = numberFrom(row?.liquidity_score);
  return score === null ? "spread quality from bid/ask" : `${score.toFixed(0)} spread-derived score`;
}

export function moneyOrNumber(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  if (value === null) return "-";
  return value >= 100 ? value.toFixed(0) : value.toFixed(2);
}

export function unavailableSignals(row: RowRecord | undefined): RowRecord[] {
  const value = row?.unavailable_signals;
  if (Array.isArray(value)) return value.filter((item) => item && typeof item === "object" && !Array.isArray(item)) as RowRecord[];
  return [];
}

export function numberFrom(value: RowRecord[string]): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function ratioTone(value: number | null): string {
  if (value === null) return "warn";
  return value > 0 ? "good" : value < 0 ? "bad" : "info";
}

export function usefulEvidence(row: Record<string, string>): boolean {
  const source = row.source.toLowerCase();
  const title = row.title.toLowerCase();
  const genericTitle = ["technical setups", "liquidity", "earnings setups", "options payoff", "trader filings", "evidence item"].includes(title);
  const genericSource = ["technical", "liquidity", "earnings_setup", "options_payoff", "filings"].includes(source);
  const hasSignal = row.signal !== "-";
  const hasDate = row.date !== "-";
  return !genericTitle && (!genericSource || hasSignal || hasDate);
}

export function formatCompactMoney(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}T`;
  if (abs >= 1_000_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}B`;
  if (abs >= 1_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}M`;
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: abs > 1000 ? 0 : 2 });
}
