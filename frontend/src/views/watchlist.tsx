import { Search, Star, StarOff } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { DataTableFrame, EmptyState, MetricTile, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type { PanelData } from "@/types";
import { buildWatchlistViewModel, type WatchState, type WatchlistFilters, type WatchlistRow, type WatchlistSort, type WatchlistTab } from "@/viewModels/watchlist";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

const storageKey = "market.watchlist.localStates.v1";
const tabs: Array<{ key: WatchlistTab; label: string }> = [
  { key: "active", label: "Active" },
  { key: "watched", label: "Watched" },
  { key: "owned", label: "Owned" },
  { key: "candidates", label: "Candidates" },
  { key: "momentum", label: "Momentum" },
  { key: "quality", label: "Quality" },
  { key: "value", label: "Value" },
];

export function WatchlistPage({ data, onOpenTicker }: { data: PanelData; onOpenTicker: OpenTicker }) {
  const [localStates, setLocalStates] = useState<Record<string, WatchState | undefined>>(() => readLocalStates());
  const [filters, setFilters] = useState<WatchlistFilters>({
    tab: "active",
    query: "",
    minRating: 0,
    maxForwardPe: null,
    minRoic: null,
    sort: "rank",
  });
  const viewModel = useMemo(() => buildWatchlistViewModel(data, filters, localStates), [data, filters, localStates]);

  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify(localStates));
  }, [localStates]);

  const updateFilter = <K extends keyof WatchlistFilters>(key: K, value: WatchlistFilters[K]) => setFilters((current) => ({ ...current, [key]: value }));
  const setWatchState = (symbol: string, currentState: WatchState) => {
    setLocalStates((current) => {
      const next = { ...current };
      next[symbol] = currentState === "candidate" ? "watched" : "candidate";
      return next;
    });
  };

  return (
    <WorkspacePage
      eyebrow="Market data"
      title="Watchlist"
      subtitle="Dynamic ticker selection, valuation quality, and momentum context for deciding what deserves attention."
    >
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile label="Active Watchlist" value={viewModel.metrics.active} caption="owned and watched names" tone={viewModel.metrics.active ? "info" : "muted"} />
        <MetricTile label="Candidate Pool" value={viewModel.metrics.candidates} caption="unwatched screened names" tone={viewModel.metrics.candidates ? "info" : "muted"} />
        <MetricTile label="Momentum Leaders" value={viewModel.metrics.momentumLeaders} caption="RS score above 70" tone={viewModel.metrics.momentumLeaders ? "good" : "muted"} />
        <MetricTile label="Deep Drawdowns" value={viewModel.metrics.deepDrawdowns} caption="20%+ below high" tone={viewModel.metrics.deepDrawdowns ? "warn" : "good"} />
      </div>

      <WatchlistControls filters={filters} counts={viewModel.counts} totalRows={viewModel.rows.length} visibleRows={viewModel.visibleRows.length} onChange={updateFilter} />
      <WatchlistTable rows={viewModel.visibleRows} onOpenTicker={onOpenTicker} onSetWatchState={setWatchState} />
    </WorkspacePage>
  );
}

function WatchlistControls({ filters, counts, totalRows, visibleRows, onChange }: { filters: WatchlistFilters; counts: Record<WatchlistTab, number>; totalRows: number; visibleRows: number; onChange: <K extends keyof WatchlistFilters>(key: K, value: WatchlistFilters[K]) => void }) {
  return (
    <div className="space-y-3 border-b border-border pb-4">
      <div className="flex flex-wrap gap-2">
        {tabs.map((tab) => (
          <Button key={tab.key} type="button" variant={filters.tab === tab.key ? "default" : "outline"} size="sm" className={cn("gap-2", filters.tab !== tab.key && "bg-card")} onClick={() => onChange("tab", tab.key)}>
            {tab.label}
            <span className={cn("rounded-sm px-1.5 py-0.5 text-[11px] leading-none", filters.tab === tab.key ? "bg-primary-foreground/20" : "bg-muted text-muted-foreground")}>{counts[tab.key]}</span>
          </Button>
        ))}
      </div>

      <div className="grid gap-2 lg:grid-cols-[minmax(220px,1fr)_repeat(4,minmax(140px,180px))]">
        <div className="relative min-w-0">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input className="pl-9" value={filters.query} onChange={(event) => onChange("query", event.target.value)} placeholder="Filter ticker, company, action" aria-label="Filter watchlist" />
        </div>
        <Select value={filters.sort} onValueChange={(value) => onChange("sort", value as WatchlistSort)}>
          <SelectTrigger aria-label="Sort watchlist"><SelectValue placeholder="Sort" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="rank">Decision rank</SelectItem>
            <SelectItem value="momentum">Momentum</SelectItem>
            <SelectItem value="quality">Quality</SelectItem>
            <SelectItem value="value">Value upside</SelectItem>
            <SelectItem value="marketCap">Market cap</SelectItem>
            <SelectItem value="drawdown">Deep drawdown</SelectItem>
            <SelectItem value="symbol">Ticker</SelectItem>
          </SelectContent>
        </Select>
        <Select value={String(filters.minRating)} onValueChange={(value) => onChange("minRating", Number(value))}>
          <SelectTrigger aria-label="Minimum rating"><SelectValue placeholder="Rating" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="0">Any rating</SelectItem>
            <SelectItem value="3">3+ stars</SelectItem>
            <SelectItem value="4">4+ stars</SelectItem>
            <SelectItem value="5">5 stars</SelectItem>
          </SelectContent>
        </Select>
        <Select value={filters.maxForwardPe === null ? "any" : String(filters.maxForwardPe)} onValueChange={(value) => onChange("maxForwardPe", value === "any" ? null : Number(value))}>
          <SelectTrigger aria-label="Maximum forward PE"><SelectValue placeholder="Forward P/E" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="any">Any Fwd P/E</SelectItem>
            <SelectItem value="15">Under 15x</SelectItem>
            <SelectItem value="20">Under 20x</SelectItem>
            <SelectItem value="25">Under 25x</SelectItem>
            <SelectItem value="40">Under 40x</SelectItem>
          </SelectContent>
        </Select>
        <Select value={filters.minRoic === null ? "any" : String(filters.minRoic)} onValueChange={(value) => onChange("minRoic", value === "any" ? null : Number(value))}>
          <SelectTrigger aria-label="Minimum ROIC"><SelectValue placeholder="ROIC" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="any">Any ROIC</SelectItem>
            <SelectItem value="10">10%+ ROIC</SelectItem>
            <SelectItem value="15">15%+ ROIC</SelectItem>
            <SelectItem value="20">20%+ ROIC</SelectItem>
            <SelectItem value="30">30%+ ROIC</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="text-xs text-muted-foreground">{visibleRows.toLocaleString()} shown from {totalRows.toLocaleString()} loaded symbols</div>
    </div>
  );
}

function WatchlistTable({ rows, onOpenTicker, onSetWatchState }: { rows: WatchlistRow[]; onOpenTicker: OpenTicker; onSetWatchState: (symbol: string, currentState: WatchState) => void }) {
  if (!rows.length) return <EmptyState title="No watchlist matches" detail="Adjust the filters or switch tabs to widen the ticker set." />;
  return (
    <DataTableFrame title="Watchlist Matrix">
      <table className="w-full min-w-[1320px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-2 py-2">Watch</th>
            <th className="px-2 py-2">Ticker</th>
            <th className="px-2 py-2">Company</th>
            <th className="px-2 py-2 text-right">Price</th>
            <th className="px-2 py-2 text-right">Mkt Cap</th>
            <th className="px-2 py-2 text-right">Fwd P/E</th>
            <th className="px-2 py-2 text-right">ROIC</th>
            <th className="px-2 py-2">Rating</th>
            <th className="px-2 py-2 text-right">20D</th>
            <th className="px-2 py-2 text-right">60D</th>
            <th className="px-2 py-2">Trend</th>
            <th className="px-2 py-2">52W Gap</th>
            <th className="px-2 py-2">RS</th>
            <th className="px-2 py-2 text-center">20</th>
            <th className="px-2 py-2 text-center">50</th>
            <th className="px-2 py-2 text-center">200</th>
            <th className="px-2 py-2">Next</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol} className="border-b border-border align-middle transition-colors hover:bg-accent/35">
              <td className="px-2 py-2">
                {row.watchState === "owned" ? (
                  <StatusBadge tone="info">Owned</StatusBadge>
                ) : (
                  <Button type="button" variant="ghost" size="sm" className="min-w-24 justify-start" onClick={() => onSetWatchState(row.symbol, row.watchState)}>
                    {row.watchState === "watched" ? <StarOff /> : <Star />}
                    {row.watchState === "watched" ? "Unwatch" : "Watch"}
                  </Button>
                )}
              </td>
              <td className="px-2 py-2"><Button type="button" variant="link" className="h-auto min-w-11 justify-start p-0 font-semibold" onClick={() => onOpenTicker(row.symbol)}>{row.symbol}</Button></td>
              <td className="max-w-60 px-2 py-2"><div className="truncate" title={row.name}>{row.name}</div></td>
              <td className="px-2 py-2 text-right tabular-nums">{formatPrice(row.price)}</td>
              <td className="px-2 py-2 text-right tabular-nums">{formatMarketCap(row.marketCap)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", peTone(row.forwardPe))}>{formatMultiple(row.forwardPe)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", row.roic >= 20 && "text-green-700")}>{formatPercent(row.roic, false)}</td>
              <td className="px-2 py-2"><StarRating rating={row.rating} /></td>
              <td className={cn("px-2 py-2 text-right tabular-nums", percentTone(row.return20d))}>{formatPercent(row.return20d, true)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", percentTone(row.return60d))}>{formatPercent(row.return60d, true)}</td>
              <td className="px-2 py-2"><Sparkline points={row.trend} /></td>
              <td className="px-2 py-2"><DrawdownBar value={row.drawdownFromHigh} /></td>
              <td className="px-2 py-2"><StrengthBar value={row.technicalScore} /></td>
              <td className="px-2 py-2 text-center"><MaFlag state={row.ma20Up} /></td>
              <td className="px-2 py-2 text-center"><MaFlag state={row.ma50Up} /></td>
              <td className="px-2 py-2 text-center"><MaFlag state={row.ma200Up} /></td>
              <td className="max-w-72 px-2 py-2 text-xs leading-5 text-muted-foreground"><div className="line-clamp-2">{row.nextAction}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function StarRating({ rating }: { rating: number }) {
  return (
    <div className="flex items-center gap-0.5" aria-label={`${rating || 0} of 5 rating`}>
      {Array.from({ length: 5 }, (_, index) => (
        <Star key={index} className={cn("size-3.5", index < Math.round(rating) ? "fill-amber-400 text-amber-500" : "text-muted-foreground/35")} />
      ))}
    </div>
  );
}

function Sparkline({ points }: { points: number[] }) {
  const width = 92;
  const height = 26;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const spread = max - min || 1;
  const path = points.map((point, index) => {
    const x = (index / Math.max(1, points.length - 1)) * width;
    const y = height - ((point - min) / spread) * (height - 4) - 2;
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const up = points.at(-1)! >= points[0]!;
  return (
    <svg width={width} height={height} role="img" aria-label={up ? "rising trend" : "falling trend"}>
      <path d={path} fill="none" stroke={up ? "#16a34a" : "#dc2626"} strokeWidth="1.8" />
    </svg>
  );
}

function DrawdownBar({ value }: { value: number }) {
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

function StrengthBar({ value }: { value: number }) {
  const normalized = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
  return (
    <div className="min-w-28">
      <div className="mb-1 flex justify-between text-xs tabular-nums"><span>RS</span><span>{Number.isFinite(value) ? value.toFixed(0) : "-"}</span></div>
      <div className="flex h-8 items-end gap-px border-r border-green-600/50">
        {Array.from({ length: 20 }, (_, index) => {
          const active = (index + 1) * 5 <= normalized;
          return <span key={index} className={cn("w-1 flex-1 bg-green-500/25", active && "bg-green-500")} style={{ height: `${20 + index * 4}%` }} />;
        })}
      </div>
    </div>
  );
}

function MaFlag({ state }: { state: boolean | null }) {
  if (state === null) return <span className="text-muted-foreground">-</span>;
  return <span className={state ? "font-semibold text-green-600" : "font-semibold text-red-600"}>{state ? "Up" : "Dn"}</span>;
}

function readLocalStates(): Record<string, WatchState | undefined> {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey) ?? "{}") as Record<string, WatchState | undefined>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function formatPrice(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value >= 100 ? 0 : 2 });
}

function formatMarketCap(value: number): string {
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 1_000_000_000_000) return `$${(value / 1_000_000_000_000).toFixed(2)}T`;
  if (Math.abs(value) >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(0)}M`;
  return `$${value.toLocaleString()}`;
}

function formatMultiple(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(1)}x` : "-";
}

function formatPercent(value: number, ratio: boolean): string {
  if (!Number.isFinite(value)) return "-";
  const pct = ratio ? value * 100 : value;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

function percentTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  return value >= 0 ? "text-green-700" : "text-red-700";
}

function peTone(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value <= 20) return "bg-green-50 text-green-800";
  if (value <= 40) return "bg-amber-50 text-amber-800";
  return "bg-red-50 text-red-800";
}
