import { ArrowDown, ArrowUp, Minus, Star } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { WatchState, WatchlistRow } from "@/viewModels/watchlist";

import { columnHelp, type ColumnHelpKey } from "./columns";
import { formatPercent } from "./format";

export function CountPill({ label, value }: { label: string; value: number }) {
  return (
    <span className="rounded-sm border border-border bg-card px-2 py-1">
      {label} <span className="ml-1 font-semibold tabular-nums text-foreground">{value.toLocaleString()}</span>
    </span>
  );
}

export function LoadMoreSentinel({ loading, onLoadMore }: { loading: boolean; onLoadMore: () => Promise<void> }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node || loading) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void onLoadMore();
      },
      { rootMargin: "720px 0px 720px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [loading, onLoadMore]);

  return (
    <div ref={ref} className="flex justify-center py-3">
      <Button type="button" variant="outline" disabled={loading} onClick={() => void onLoadMore()}>
        {loading ? "Loading" : "Load more"}
      </Button>
    </div>
  );
}

export function ColumnHeader({ id, className, children }: { id: ColumnHelpKey; className?: string; children?: ReactNode }) {
  const help = columnHelp[id];
  return (
    <th className={cn("px-2 py-2", className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 whitespace-nowrap underline decoration-dotted underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {children ?? help.label}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-80 text-xs leading-5">
          <div className="mb-1 font-semibold">{help.label}</div>
          <div>{help.detail}</div>
        </TooltipContent>
      </Tooltip>
    </th>
  );
}

export function WatchStar({ row, pending, onSetWatchState }: { row: WatchlistRow; pending: boolean; onSetWatchState: (symbol: string, currentState: WatchState) => Promise<void> }) {
  const active = row.watchState === "watched" || row.watchState === "owned";
  const label = row.watchState === "owned" ? "Owned position" : active ? `Remove ${row.symbol} from watchlist` : `Add ${row.symbol} to watchlist`;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className={cn("size-8", active ? "text-amber-500 hover:text-amber-600" : "text-muted-foreground hover:text-amber-500")}
            disabled={pending || row.watchState === "owned"}
            aria-label={label}
            onClick={() => void onSetWatchState(row.symbol, row.watchState)}
          >
            <Star className={cn("size-4", active && "fill-current")} />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function Sparkline({ points }: { points: number[] }) {
  const width = 124;
  const height = 30;
  const safePoints = points.filter((point) => Number.isFinite(point));
  if (safePoints.length < 2) return <span className="text-xs text-muted-foreground">-</span>;
  const min = Math.min(...safePoints);
  const max = Math.max(...safePoints);
  const spread = max - min || 1;
  const path = safePoints.map((point, index) => {
    const x = (index / Math.max(1, safePoints.length - 1)) * width;
    const y = height - ((point - min) / spread) * (height - 4) - 2;
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const up = safePoints.at(-1)! >= safePoints[0]!;
  return (
    <svg width={width} height={height} role="img" aria-label={up ? "rising 1 year trend" : "falling 1 year trend"}>
      <path d={path} fill="none" stroke={up ? "#22c55e" : "#ef4444"} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function DrawdownBar({ value }: { value: number }) {
  const pct = Number.isFinite(value) ? value * 100 : Number.NaN;
  const width = Number.isFinite(pct) ? Math.min(100, Math.max(4, Math.abs(pct) * 2)) : 0;
  const tone = pct > -10 ? "bg-green-600" : pct > -25 ? "bg-amber-500" : "bg-red-600";
  return (
    <div className="min-w-28">
      <div className="mb-1 text-right text-xs tabular-nums">{Number.isFinite(pct) ? `${pct.toFixed(1)}%` : "-"}</div>
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full", tone)} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

export function RsRankMiniChart({ rank, bars }: { rank: number; bars: number[] }) {
  const finiteBars = bars.filter((bar) => Number.isFinite(bar));
  const rising = finiteBars.length >= 2 ? finiteBars.at(-1)! >= finiteBars[0]! : true;
  const peakIndex = finiteBars.reduce((bestIndex, bar, index) => (bar >= finiteBars[bestIndex] ? index : bestIndex), 0);
  const peakLeftPct = finiteBars.length > 1 ? (peakIndex / (finiteBars.length - 1)) * 100 : 100;
  return (
    <div className="min-w-36">
      <div className="mb-1 text-right text-xs tabular-nums">{Number.isFinite(rank) ? rank.toFixed(0) : "-"}</div>
      <div className="relative flex h-8 items-end gap-px pr-px" aria-label="3 month relative strength bars">
        {finiteBars.length ? finiteBars.map((bar, index) => (
          <span
            key={`${index}-${bar.toFixed(1)}`}
            className={cn("w-1 flex-1 rounded-[1px]", rising ? "bg-green-400" : "bg-red-400")}
            style={{ height: `${Math.max(3, Math.min(30, 3 + bar * 0.27))}px` }}
          />
        )) : <span className="text-xs text-muted-foreground">-</span>}
        {finiteBars.length ? (
          <span
            aria-hidden="true"
            className="pointer-events-none absolute bottom-0 top-0 w-0.5 -translate-x-1/2 rounded-full bg-foreground/70"
            style={{ left: `${peakLeftPct}%` }}
          />
        ) : null}
      </div>
    </div>
  );
}

export function VolumeBars({ value, bars }: { value: number; bars: number[] }) {
  const finiteBars = bars.filter((bar) => Number.isFinite(bar));
  const hot = Number.isFinite(value) && value >= 1.2;
  return (
    <div className="min-w-32">
      <div className={cn("mb-1 text-right text-xs tabular-nums", hot ? "text-amber-700" : "text-muted-foreground")}>
        {Number.isFinite(value) ? `${value.toFixed(2)}x` : "-"}
      </div>
      <div className="flex h-8 items-end gap-px pr-px" aria-label="1 month relative volume bars">
        {finiteBars.length ? finiteBars.map((bar, index) => (
          <span
            key={`${index}-${bar.toFixed(1)}`}
            className={cn("w-1 flex-1 rounded-[1px]", hot ? "bg-amber-500" : "bg-sky-400")}
            style={{ height: `${Math.max(3, Math.min(30, 3 + bar * 0.27))}px` }}
          />
        )) : <span className="text-xs text-muted-foreground">-</span>}
      </div>
    </div>
  );
}

export function AtrMiniLine({ value, points }: { value: number; points: number[] }) {
  const width = 96;
  const height = 28;
  const safePoints = points.filter((point) => Number.isFinite(point));
  const label = formatPercent(value, true);
  if (safePoints.length < 2) {
    return <div className="min-w-28 text-right text-xs tabular-nums text-muted-foreground">{label}</div>;
  }
  const min = Math.min(...safePoints);
  const max = Math.max(...safePoints);
  const spread = max - min || 1;
  const path = safePoints.map((point, index) => {
    const x = (index / Math.max(1, safePoints.length - 1)) * width;
    const y = height - ((point - min) / spread) * (height - 4) - 2;
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <div className="min-w-32">
      <div className="mb-1 text-right text-xs tabular-nums">{label}</div>
      <svg width={width} height={height} role="img" aria-label="1 month ATR percent line">
        <path d={path} fill="none" stroke="#64748b" strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}

export function ValuationPercentile({ value }: { value: number }) {
  const pct = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : Number.NaN;
  const tone = pct <= 30 ? "bg-green-600" : pct <= 70 ? "bg-amber-500" : "bg-red-600";
  return (
    <div className="min-w-28">
      <div className="mb-1 text-right text-xs tabular-nums">{Number.isFinite(pct) ? `${pct.toFixed(0)}th` : "-"}</div>
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        {Number.isFinite(pct) ? <div className={cn("h-full rounded-full", tone)} style={{ width: `${Math.max(4, pct)}%` }} /> : null}
      </div>
    </div>
  );
}

export function OptionsStatus({ status, quality }: { status: string; quality: string }) {
  const loaded = status === "loaded";
  const tone = loaded ? quality === "tight" ? "border-green-200 bg-green-50 text-green-800" : quality === "wide" ? "border-amber-200 bg-amber-50 text-amber-900" : "border-sky-200 bg-sky-50 text-sky-900" : "border-border bg-muted text-muted-foreground";
  return <span className={cn("inline-flex min-w-16 justify-center rounded border px-2 py-1 text-xs font-medium", tone)}>{loaded ? quality : status}</span>;
}

export function OptionsIvRegime({ regime }: { regime: string }) {
  const tone = regime === "low" ? "bg-green-50 text-green-800" : regime === "elevated" ? "bg-red-50 text-red-800" : regime === "normal" ? "bg-sky-50 text-sky-900" : "text-muted-foreground";
  return <span className={cn("inline-flex min-w-16 justify-center rounded px-2 py-1 text-xs font-medium", tone)}>{regime}</span>;
}

export function OptionsSkew({ signal }: { signal: string }) {
  const tone = signal === "put premium" ? "bg-amber-50 text-amber-900" : signal === "call premium" ? "bg-sky-50 text-sky-900" : signal === "neutral" ? "bg-muted text-muted-foreground" : "text-muted-foreground";
  return <span className={cn("inline-flex min-w-24 justify-center rounded px-2 py-1 text-xs font-medium", tone)}>{signal}</span>;
}

export function MaFlag({ state }: { state: boolean | null }) {
  if (state === null) return <Minus className="mx-auto size-4 text-muted-foreground" aria-label="missing moving average" />;
  const Icon = state ? ArrowUp : ArrowDown;
  return <Icon className={cn("mx-auto size-4 stroke-[2.5]", state ? "text-green-600" : "text-red-600")} aria-label={state ? "above moving average" : "below moving average"} />;
}
