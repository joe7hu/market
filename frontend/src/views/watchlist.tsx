import { ArrowDown, ArrowUp, Minus, Plus, Search, Star } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { deleteWatchlistSymbol, saveWatchlistSymbol } from "@/api";
import { DataTableFrame, EmptyState, MetricTile } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { PanelData } from "@/types";
import { buildWatchlistViewModel, type WatchState, type WatchlistFilters, type WatchlistRow, type WatchlistSort } from "@/viewModels/watchlist";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

const storageKey = "market.watchlist.localStates.v1";

export function WatchlistPage({ data, onOpenTicker, onRefresh, onLoadUnwatchedPage }: { data: PanelData; onOpenTicker: OpenTicker; onRefresh: () => Promise<void>; onLoadUnwatchedPage: (offset: number) => Promise<void> }) {
  const [pendingSymbol, setPendingSymbol] = useState<string | null>(null);
  const [loadingUnwatched, setLoadingUnwatched] = useState(false);
  const [newSymbol, setNewSymbol] = useState("");
  const [filters, setFilters] = useState<WatchlistFilters>({
    query: "",
    minRating: 0,
    maxForwardPe: null,
    minRoic: null,
    sort: "rank",
  });
  const viewModel = useMemo(() => buildWatchlistViewModel(data, filters, {}), [data, filters]);
  const loadedUnwatchedCount = data.watchlistUnwatched.rows?.length ?? 0;
  const totalUnwatchedCount = data.watchlistUnwatched.count ?? loadedUnwatchedCount;
  const canLoadMoreUnwatched = loadedUnwatchedCount < totalUnwatchedCount;

  const updateFilter = <K extends keyof WatchlistFilters>(key: K, value: WatchlistFilters[K]) => setFilters((current) => ({ ...current, [key]: value }));
  const loadMoreUnwatched = useCallback(async () => {
    if (loadingUnwatched || !canLoadMoreUnwatched) return;
    setLoadingUnwatched(true);
    try {
      await onLoadUnwatchedPage(loadedUnwatchedCount);
    } finally {
      setLoadingUnwatched(false);
    }
  }, [canLoadMoreUnwatched, loadedUnwatchedCount, loadingUnwatched, onLoadUnwatchedPage]);

  useEffect(() => {
    const legacyStates = readLocalStates();
    const entries = Object.entries(legacyStates).filter((entry): entry is [string, WatchState] => Boolean(entry[0] && entry[1]));
    if (!entries.length) return;
    let cancelled = false;
    void Promise.all(
      entries.map(([symbol, state]) => (state === "watched" ? saveWatchlistSymbol(symbol) : deleteWatchlistSymbol(symbol))),
    ).then(async () => {
      if (cancelled) return;
      window.localStorage.removeItem(storageKey);
      await onRefresh();
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [onRefresh]);

  const persistWatchState = async (symbol: string, currentState: WatchState) => {
    setPendingSymbol(symbol);
    try {
      if (currentState === "candidate") {
        await saveWatchlistSymbol(symbol);
      } else {
        await deleteWatchlistSymbol(symbol);
      }
      await onRefresh();
    } finally {
      setPendingSymbol(null);
    }
  };
  const addSymbol = async () => {
    const symbol = newSymbol.trim().toUpperCase();
    if (!symbol) return;
    setPendingSymbol(symbol);
    try {
      await saveWatchlistSymbol(symbol);
      setNewSymbol("");
      await onRefresh();
    } finally {
      setPendingSymbol(null);
    }
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

      <WatchlistControls
        filters={filters}
        counts={viewModel.counts}
        totalRows={viewModel.watchedRows.length + totalUnwatchedCount}
        visibleRows={viewModel.visibleRows.length}
        newSymbol={newSymbol}
        pending={Boolean(pendingSymbol)}
        onNewSymbolChange={setNewSymbol}
        onAddSymbol={addSymbol}
        onChange={updateFilter}
      />
      <WatchlistSection
        title="Watched"
        detail={`${viewModel.watchedRows.length.toLocaleString()} shown, including owned positions`}
        rows={viewModel.watchedRows}
        pendingSymbol={pendingSymbol}
        onOpenTicker={onOpenTicker}
        onSetWatchState={persistWatchState}
      />
      <WatchlistSection
        title="Not watched"
        detail={`${viewModel.unwatchedRows.length.toLocaleString()} shown from ${loadedUnwatchedCount.toLocaleString()} loaded / ${totalUnwatchedCount.toLocaleString()} candidates`}
        rows={viewModel.unwatchedRows}
        canLoadMore={canLoadMoreUnwatched}
        loadingMore={loadingUnwatched}
        onLoadMore={loadMoreUnwatched}
        pendingSymbol={pendingSymbol}
        onOpenTicker={onOpenTicker}
        onSetWatchState={persistWatchState}
      />
    </WorkspacePage>
  );
}

function WatchlistControls({ filters, counts, totalRows, visibleRows, newSymbol, pending, onNewSymbolChange, onAddSymbol, onChange }: { filters: WatchlistFilters; counts: { watched: number; owned: number; unwatched: number; momentum: number; quality: number; value: number }; totalRows: number; visibleRows: number; newSymbol: string; pending: boolean; onNewSymbolChange: (value: string) => void; onAddSymbol: () => void; onChange: <K extends keyof WatchlistFilters>(key: K, value: WatchlistFilters[K]) => void }) {
  return (
    <div className="space-y-3 border-b border-border pb-4">
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input value={newSymbol} onChange={(event) => onNewSymbolChange(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void onAddSymbol(); }} placeholder="Add ticker" aria-label="Add ticker to watchlist" className="max-w-xs uppercase" />
        <Button type="button" className="gap-2 sm:w-auto" disabled={pending || !newSymbol.trim()} onClick={() => void onAddSymbol()}>
          <Plus />
          Add
        </Button>
      </div>
      <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
        <CountPill label="Watched" value={counts.watched + counts.owned} />
        <CountPill label="Owned" value={counts.owned} />
        <CountPill label="Not watched" value={counts.unwatched} />
        <CountPill label="Momentum" value={counts.momentum} />
        <CountPill label="Quality" value={counts.quality} />
        <CountPill label="Value" value={counts.value} />
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
            <SelectItem value="state">Watch state</SelectItem>
            <SelectItem value="momentum">Momentum</SelectItem>
            <SelectItem value="quality">Quality</SelectItem>
            <SelectItem value="value">Value upside</SelectItem>
            <SelectItem value="returnYtd">% YTD</SelectItem>
            <SelectItem value="return1y">% 1Y</SelectItem>
            <SelectItem value="rsRank1m">RS Rank 1M</SelectItem>
            <SelectItem value="rating">Rating</SelectItem>
            <SelectItem value="roic">ROIC</SelectItem>
            <SelectItem value="ps">P/S</SelectItem>
            <SelectItem value="pe">P/E</SelectItem>
            <SelectItem value="forwardPe">Fwd P/E</SelectItem>
            <SelectItem value="price">Price</SelectItem>
            <SelectItem value="marketCap">Market cap</SelectItem>
            <SelectItem value="drawdown">Delta 52W high</SelectItem>
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
          <SelectTrigger aria-label="Maximum forward PE"><SelectValue placeholder="Fwd P/E" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="any">Any fwd P/E</SelectItem>
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

      <div className="text-xs text-muted-foreground">{visibleRows.toLocaleString()} shown from {totalRows.toLocaleString()} available symbols</div>
    </div>
  );
}

function CountPill({ label, value }: { label: string; value: number }) {
  return (
    <span className="rounded-sm border border-border bg-card px-2 py-1">
      {label} <span className="ml-1 font-semibold tabular-nums text-foreground">{value.toLocaleString()}</span>
    </span>
  );
}

function WatchlistSection({ title, detail, rows, canLoadMore = false, loadingMore = false, onLoadMore, pendingSymbol, onOpenTicker, onSetWatchState }: { title: string; detail: string; rows: WatchlistRow[]; canLoadMore?: boolean; loadingMore?: boolean; onLoadMore?: () => Promise<void>; pendingSymbol: string | null; onOpenTicker: OpenTicker; onSetWatchState: (symbol: string, currentState: WatchState) => Promise<void> }) {
  if (!rows.length && !canLoadMore) return <EmptyState title={`No ${title.toLowerCase()} matches`} detail="Adjust the filters to widen the ticker set." />;
  return (
    <div className="space-y-2">
      <div className="flex items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-xs text-muted-foreground">{detail}</p>
        </div>
      </div>
      {rows.length ? <WatchlistTable rows={rows} pendingSymbol={pendingSymbol} onOpenTicker={onOpenTicker} onSetWatchState={onSetWatchState} /> : null}
      {canLoadMore && onLoadMore ? <LoadMoreSentinel loading={loadingMore} onLoadMore={onLoadMore} /> : null}
    </div>
  );
}

function LoadMoreSentinel({ loading, onLoadMore }: { loading: boolean; onLoadMore: () => Promise<void> }) {
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

function WatchlistTable({ rows, pendingSymbol, onOpenTicker, onSetWatchState }: { rows: WatchlistRow[]; pendingSymbol: string | null; onOpenTicker: OpenTicker; onSetWatchState: (symbol: string, currentState: WatchState) => Promise<void> }) {
  return (
    <DataTableFrame title="">
      <table className="w-full min-w-[1600px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="w-12 px-2 py-2 text-center"><Star className="mx-auto size-3.5" aria-label="Watch" /></th>
            <th className="px-2 py-2">Ticker</th>
            <th className="px-2 py-2">Company</th>
            <th className="px-2 py-2 text-right">Price</th>
            <th className="px-2 py-2 text-right">Mkt Cap</th>
            <th className="px-2 py-2 text-right">P/S</th>
            <th className="px-2 py-2 text-right">P/E</th>
            <th className="px-2 py-2 text-right">Fwd P/E</th>
            <th className="px-2 py-2 text-right">% YTD</th>
            <th className="px-2 py-2">Chart 1Y</th>
            <th className="px-2 py-2 text-right">% 1Y</th>
            <th className="px-2 py-2">Delta 52W Highs</th>
            <th className="px-2 py-2">RS Rank 1M</th>
            <th className="px-2 py-2 text-center">20SMA</th>
            <th className="px-2 py-2 text-center">50SMA</th>
            <th className="px-2 py-2 text-center">200SMA</th>
            <th className="px-2 py-2">Next</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol} className="border-b border-border align-middle transition-colors hover:bg-accent/35">
              <td className="px-2 py-2 text-center"><WatchStar row={row} pending={pendingSymbol === row.symbol} onSetWatchState={onSetWatchState} /></td>
              <td className="px-2 py-2"><Button type="button" variant="link" className="h-auto min-w-11 justify-start p-0 font-semibold" onClick={() => onOpenTicker(row.symbol)}>{row.symbol}</Button></td>
              <td className="max-w-60 px-2 py-2"><div className="truncate" title={row.name}>{row.name}</div></td>
              <td className="px-2 py-2 text-right tabular-nums">{formatPrice(row.price)}</td>
              <td className="px-2 py-2 text-right tabular-nums">{formatMarketCap(row.marketCap)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", multipleTone(row.psRatio, 8, 20))}>{formatMultiple(row.psRatio)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", multipleTone(row.peRatio, 25, 50))}>{formatMultiple(row.peRatio)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", multipleTone(row.forwardPe, 25, 50))}>{formatMultiple(row.forwardPe)}</td>
              <td className={cn("px-2 py-2 text-right tabular-nums", percentCellTone(row.returnYtd))}>{formatPercent(row.returnYtd, true)}</td>
              <td className="px-2 py-2"><Sparkline points={row.trend} /></td>
              <td className={cn("px-2 py-2 text-right tabular-nums", percentCellTone(row.return1y))}>{formatPercent(row.return1y, true)}</td>
              <td className="px-2 py-2"><DrawdownBar value={row.drawdownFromHigh} /></td>
              <td className="px-2 py-2"><RsRankMiniChart rank={row.rsRank1m} bars={row.rsRankBars} /></td>
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

function WatchStar({ row, pending, onSetWatchState }: { row: WatchlistRow; pending: boolean; onSetWatchState: (symbol: string, currentState: WatchState) => Promise<void> }) {
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

function Sparkline({ points }: { points: number[] }) {
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

function RsRankMiniChart({ rank, bars }: { rank: number; bars: number[] }) {
  const finiteBars = bars.filter((bar) => Number.isFinite(bar));
  const rising = finiteBars.length >= 2 ? finiteBars.at(-1)! >= finiteBars[0]! : true;
  const peakIndex = finiteBars.reduce((bestIndex, bar, index) => (bar >= finiteBars[bestIndex] ? index : bestIndex), 0);
  const peakLeftPct = finiteBars.length > 1 ? (peakIndex / (finiteBars.length - 1)) * 100 : 100;
  return (
    <div className="min-w-36">
      <div className="mb-1 text-right text-xs tabular-nums">{Number.isFinite(rank) ? rank.toFixed(0) : "-"}</div>
      <div className="relative flex h-8 items-end gap-px pr-px" aria-label="1 month relative strength bars">
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

function MaFlag({ state }: { state: boolean | null }) {
  if (state === null) return <Minus className="mx-auto size-4 text-muted-foreground" aria-label="missing moving average" />;
  const Icon = state ? ArrowUp : ArrowDown;
  return <Icon className={cn("mx-auto size-4 stroke-[2.5]", state ? "text-green-600" : "text-red-600")} aria-label={state ? "above moving average" : "below moving average"} />;
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
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

function formatPercent(value: number, ratio: boolean): string {
  if (!Number.isFinite(value)) return "-";
  const pct = ratio ? value * 100 : value;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

function percentCellTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.5) return "bg-green-100 text-green-900";
  if (value >= 0) return "bg-green-50 text-green-800";
  if (value <= -0.2) return "bg-red-100 text-red-900";
  return "bg-red-50 text-red-800";
}

function multipleTone(value: number, goodMax: number, warnMax: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value <= goodMax) return "bg-green-50 text-green-800";
  if (value <= warnMax) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}
