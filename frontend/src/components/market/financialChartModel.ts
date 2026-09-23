/** Presentation-only chart coordinates. Missing evidence is never a zero return. */
export type FinancialPoint = { at: string; value: number | null; trade_id: string; gapBefore?: boolean; segment?: number };
export type PaperEvent = { at: string; kind: string; trade_id?: string; symbol?: string; strategy?: string | null; label?: string; value?: number; cumulative_net_pnl?: number | null; drawdown?: number | null; pnl?: number | null; price?: number | null; quantity?: number | null; status?: string };
export type VisibleWindow = { start: number; end: number; from: string; to: string };

export const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
export const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
export const eventName = (event: PaperEvent): string => event.label || event.kind.replaceAll("_", " ");
export const chartMoney = (value: unknown, compact = false): string => finite(value)
  ? new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: compact ? "compact" : "standard", maximumFractionDigits: compact ? 1 : 2 }).format(value)
  : "Not verified";

export function normalizeFinancialPoints(points: readonly FinancialPoint[]): FinancialPoint[] {
  const normalized: FinancialPoint[] = [];
  let interrupted = false;
  for (const point of points) {
    if (!Number.isFinite(Date.parse(point.at))) { interrupted = true; continue; }
    normalized.push({ ...point, value: finite(point.value) ? point.value : null, gapBefore: point.gapBefore || interrupted });
    interrupted = false;
  }
  return normalized.sort((left, right) => Date.parse(left.at) - Date.parse(right.at));
}

export function financialLineData(points: readonly FinancialPoint[]) {
  return points.flatMap((point, index) => {
    const value: [number, number | null] = [Date.parse(point.at), point.value];
    const item = { value, point };
    // An explicit discontinuity is a null coordinate, never a made-up price.
    return index > 0 && (point.gapBefore || point.segment !== points[index - 1].segment)
      ? [{ value: [value[0], null] as [number, null], point: undefined }, item]
      : [item];
  });
}

export function timeExtent(points: readonly { at: string }[]): [number, number] | null {
  let min = Infinity, max = -Infinity;
  for (const point of points) {
    const value = Date.parse(point.at);
    if (Number.isFinite(value)) { min = Math.min(min, value); max = Math.max(max, value); }
  }
  return Number.isFinite(min) ? [min, max] : null;
}

function timestamp(value: unknown): number | null {
  if (finite(value)) return value;
  if (typeof value !== "string" || !value.trim()) return null;
  const parsed = /^-?\d+(\.\d+)?$/.test(value) ? Number(value) : Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function visibleTimeWindow(payload: unknown, extent: [number, number] | null): VisibleWindow | null {
  if (!extent) return null;
  const outer = record(payload);
  const range = record(Array.isArray(outer.batch) ? outer.batch[0] : outer);
  const percentage = (value: unknown, fallback: number) => finite(value) ? Math.max(0, Math.min(100, value)) : fallback;
  const clamp = (value: number) => Math.max(extent[0], Math.min(extent[1], value));
  // ECharts percentages refer to elapsed time, not indices in an irregular tape.
  const start = clamp(timestamp(range.startValue) ?? extent[0] + (extent[1] - extent[0]) * percentage(range.start, 0) / 100);
  const end = clamp(timestamp(range.endValue) ?? extent[0] + (extent[1] - extent[0]) * percentage(range.end, 100) / 100);
  return start <= end ? { start, end, from: new Date(start).toISOString().slice(0, 10), to: new Date(end).toISOString().slice(0, 10) } : null;
}

export function eventCurveValue(event: PaperEvent, mode: "cumulative" | "drawdown"): number | null {
  const value = mode === "drawdown" ? event.drawdown : event.cumulative_net_pnl;
  return finite(value) ? value : null;
}

export function paperEventTooltip(event: PaperEvent): string {
  const amount = (value: unknown) => finite(value) ? `$${value.toFixed(2)}` : "not verified";
  return [eventName(event), new Date(event.at).toLocaleString(),
    `${event.symbol ?? ""}${event.strategy ? ` · ${event.strategy}` : ""}`,
    `Price: ${amount(event.price)} · Quantity: ${event.quantity ?? "not recorded"}`,
    `P&L: ${amount(event.pnl)}`].join("\n");
}
