import type { Coverage, JsonValue, RowRecord, TickerDossier, TickerPayload } from "@/types";
import { displayField, formatPct, textField, toneFromText } from "@/views/rowFormat";
import type { Tone } from "@/ui/tone";
import type { MetricSpec } from "@/views/workspacePage";

export type MetricCell = [label: string, value: string, detail: string];

export function presentMetricCells(rows: MetricCell[]): MetricCell[] {
  return rows.filter(([, value]) => value !== "-");
}

export function dossier(ticker: TickerPayload | null): TickerDossier | undefined {
  return ticker?.dossier;
}

export function coverageStatus(coverage: Coverage | undefined): string {
  return coverage?.status ?? "missing";
}

export function isLive(coverage: Coverage | undefined): boolean {
  return coverageStatus(coverage) === "live";
}

export function coverageTone(coverage: Coverage | undefined): Tone {
  const status = coverageStatus(coverage);
  if (status === "live") return "good";
  if (status === "expired") return "bad";
  return "warn";
}

export function tickerHeaderMetrics(ticker: TickerPayload | null): MetricSpec[] {
  const d = ticker?.dossier;
  const quote = d?.quote;
  const verdict = objectField(d?.decision, "verdict");
  const sec = d?.fundamentals?.sec;
  const sources = d?.sources?.signal_count ?? 0;
  return [
    ["Price", moneyMetric(quote, "price"), displayField(quote, ["observed_at"], "No quote timestamp"), toneFromText(textField(quote, ["freshness", "type"], "loaded"))],
    ["Action", displayField(verdict, ["action"], "Not loaded"), displayField(verdict, ["freshness"], "No decision freshness"), toneFromText(textField(verdict, ["action"], "info"))],
    ["Revenue YoY", ratioMetric(sec, "revenue_growth"), "SEC company facts", toneFromText(ratioTone(numberFrom(sec?.revenue_growth)))],
    ["Sources", sources ? String(sources) : "0", "consensus and ticker signals", sources ? "good" : "warn"],
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

export function rowList(value: RowRecord[] | undefined): RowRecord[] {
  return Array.isArray(value) ? value : [];
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

export function scoreMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : value.toFixed(0);
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

export function moneyOrNumber(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  if (value === null) return "-";
  return value >= 100 ? value.toFixed(0) : value.toFixed(2);
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

export function formatCompactMoney(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}T`;
  if (abs >= 1_000_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}B`;
  if (abs >= 1_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}M`;
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: abs > 1000 ? 0 : 2 });
}
