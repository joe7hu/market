import { Badge } from "@/components/ui/badge";
import type { RowRecord } from "@/types";
import { numberField } from "@/views/rowFormat";

import {
  formatMaybePct,
  postureBadge,
  returnBarStyle,
  returnTextClass,
  returnToneClass,
  groupPillClass,
} from "./format";

export function GroupPill({ value }: { value: string }) {
  return <span className={`inline-flex min-w-24 justify-center rounded-md border px-2 py-1 text-[11px] font-semibold uppercase ${groupPillClass(value)}`}>{value}</span>;
}

export function ReturnCell({ value }: { value: number }) {
  const tone = returnToneClass(value);
  return (
    <td className="px-3 py-3 text-right">
      <span className={`inline-flex min-w-16 justify-end rounded-md px-2 py-1 font-semibold tabular-nums ${tone}`}>
        {formatMaybePct(value)}
      </span>
    </td>
  );
}

export function RangeCell({ value }: { value: number }) {
  if (!Number.isFinite(value)) return <span className="block text-right text-muted-foreground">-</span>;
  const distance = Math.max(0, Math.min(100, Math.abs(value)));
  const width = `${100 - distance}%`;
  return (
    <div className="min-w-28">
      <div className="mb-1 text-right text-xs font-medium tabular-nums text-muted-foreground">{formatMaybePct(-distance)}</div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary/70" style={{ width }} />
      </div>
    </div>
  );
}

export function ReturnProfile({ row }: { row: RowRecord }) {
  const horizons = [
    { label: "1W", value: numberField(row, ["return_1w"], Number.NaN) },
    { label: "1M", value: numberField(row, ["return_1m"], Number.NaN) },
    { label: "YTD", value: numberField(row, ["return_ytd"], Number.NaN) },
    { label: "1Y", value: numberField(row, ["return_1y"], Number.NaN) },
  ];
  const valid = horizons.filter((horizon) => Number.isFinite(horizon.value));
  if (!valid.length) return <span className="text-muted-foreground">-</span>;
  const maxAbs = Math.max(1, ...valid.map((horizon) => Math.abs(horizon.value)));
  return (
    <div className="min-w-56 space-y-1.5" title="1W one-week return; 1M one-month return; YTD year-to-date return; 1Y one-year return">
      {horizons.map((horizon) => (
        <div key={horizon.label} className="grid grid-cols-[30px_1fr_54px] items-center gap-2">
          <span className="text-[11px] font-medium text-muted-foreground">{horizon.label}</span>
          <div className="relative h-2 overflow-hidden rounded-full bg-muted">
            <span className="absolute left-1/2 top-0 h-full w-px bg-border" />
            {Number.isFinite(horizon.value) ? (
              <span
                className={`absolute top-0 h-full rounded-full ${horizon.value >= 0 ? "bg-green-600" : "bg-red-600"}`}
                style={returnBarStyle(horizon.value, maxAbs)}
              />
            ) : null}
          </div>
          <span className={`text-right text-[11px] font-semibold tabular-nums ${returnTextClass(horizon.value)}`}>
            {formatMaybePct(horizon.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

export function TrendMark({ value }: { value: RowRecord[string] }) {
  if (typeof value !== "boolean") return <span className="text-muted-foreground">-</span>;
  return <span className={value ? "rounded-md bg-green-50 px-2 py-1 font-semibold text-green-800" : "rounded-md bg-red-50 px-2 py-1 font-semibold text-red-800"}>{value ? "▲" : "▼"}</span>;
}

export function MiniMetric({ label, value, dark = false }: { label: string; value: string; dark?: boolean }) {
  return (
    <div className={dark ? "min-w-0 rounded-md border border-white/12 bg-white/[0.08] px-3 py-2" : "min-w-0 rounded-md border border-border bg-background px-3 py-2"}>
      <p className={dark ? "truncate text-[11px] font-medium uppercase text-white/56" : "truncate text-[11px] font-medium uppercase text-muted-foreground"}>{label}</p>
      <p className={dark ? "mt-1 truncate text-sm font-semibold text-white" : "mt-1 truncate text-sm font-semibold"}>{value}</p>
    </div>
  );
}

export function ScorePill({ value, posture }: { value: number; posture: string }) {
  return <Badge className="w-fit justify-self-end" variant={postureBadge(posture)}>{Number.isFinite(value) ? Math.round(value) : "--"}</Badge>;
}

export function EmptyChart({ label, dark = false }: { label: string; dark?: boolean }) {
  return (
    <div className={dark ? "flex h-full min-h-32 items-center justify-center rounded-md border border-dashed border-white/18 bg-white/[0.05] px-4 text-center text-xs text-white/62" : "flex h-full min-h-32 items-center justify-center rounded-md border border-dashed border-border bg-muted/30 px-4 text-center text-xs text-muted-foreground"}>
      {label}
    </div>
  );
}
