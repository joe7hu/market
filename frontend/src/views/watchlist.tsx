import { ArrowDown, ArrowUp, Minus, Plus, Search, Star } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { deleteWatchlistSymbol, saveWatchlistSymbol } from "@/api";
import { DataTableFrame, EmptyState } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { PanelData } from "@/types";
import { buildWatchlistViewModel, type WatchState, type WatchlistFilters, type WatchlistRow, type WatchlistSort } from "@/viewModels/watchlist";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

const storageKey = "market.watchlist.localStates.v1";

const columnHelp = {
  watch: {
    label: "Watch",
    detail: "Manual watchlist state. Starred rows stay in the watched set; unstar to demote a name when the thesis or setup no longer deserves attention.",
  },
  ticker: {
    label: "Ticker",
    detail: "Canonical symbol that opens the ticker dossier. Use the dossier before a buy, hold, or sell decision when a row-level signal needs evidence.",
  },
  company: {
    label: "Company",
    detail: "Company or fund name from configured instruments, TradingView, or yfinance. A ticker-only name usually means reference data is still thin.",
  },
  price: {
    label: "Price",
    detail: "Latest stored daily or intraday price. Treat a missing or stale price as a coverage gap before acting.",
  },
  marketCap: {
    label: "Mkt Cap",
    detail: "Latest market capitalization from screener or yfinance data. Use it to size opportunity/risk and avoid comparing small caps directly with mega caps.",
  },
  ps: {
    label: "P/S",
    detail: "Price-to-sales multiple from the latest screener or yfinance fundamentals. Lower can support buys, but only with acceptable growth and margins.",
  },
  pe: {
    label: "P/E",
    detail: "Trailing price-to-earnings multiple. High values require stronger durability/growth; negative or missing earnings show as blank.",
  },
  forwardPe: {
    label: "Fwd P/E",
    detail: "Forward price-to-earnings from estimates or yfinance. Prefer it over trailing P/E for changing earnings cycles, but verify estimate quality.",
  },
  revenueGrowth: {
    label: "Rev YoY",
    detail: "Year-over-year revenue growth from SEC facts or yfinance revenue growth. Rising growth supports buys/holds; slowing growth can trigger review.",
  },
  fcfYield: {
    label: "FCF Yield",
    detail: "Free cash flow divided by market cap. Higher yield can mark cheaper self-funding businesses; weak or negative yield raises valuation risk.",
  },
  fcfMargin: {
    label: "FCF Margin",
    detail: "Free cash flow divided by revenue. Higher margins support quality/hold conviction; deterioration can weaken the thesis even if revenue grows.",
  },
  roic: {
    label: "ROIC",
    detail: "Return on invested capital from fundamentals or screeners. Sustained high ROIC supports quality buys/holds; low ROIC needs a turnaround case.",
  },
  returnYtd: {
    label: "% YTD",
    detail: "Price return from the first available trading day of the current year. Use it as context for crowdedness and tax-year momentum.",
  },
  chart1y: {
    label: "Chart 1Y",
    detail: "One-year stored close-price sparkline. A rising line supports trend alignment; a falling line needs valuation or thesis evidence to offset it.",
  },
  return1y: {
    label: "% 1Y",
    detail: "Trailing one-year price return. Compare it with RS 3M to see whether momentum is persistent or only a short-term bounce.",
  },
  drawdown: {
    label: "Delta 52W Highs",
    detail: "Percent below the stored 52-week high. Small gaps show strength; deep gaps may be value setups or sell-risk depending on fundamentals.",
  },
  rs3m: {
    label: "RS 3M",
    detail: "Percentile rank among tickers with technical coverage by trailing 3-month return; bars show the 3-month rank path. Favor high/rising ranks for buys and holds.",
  },
  relVol1m: {
    label: "RelVol 1M",
    detail: "Recent 1-month average volume divided by the prior-volume baseline. 0.95x means about 5% below normal; >1.2x can confirm institutional interest.",
  },
  atrPct1m: {
    label: "ATR % 1M",
    detail: "Average true range over roughly 1 month divided by price. +3.6% means a normal daily range near 3.6%; use it for position sizing and stops.",
  },
  valuationPercentile: {
    label: "Val %ile",
    detail: "Current valuation percentile versus the ticker's own stored history. Low percentile is cheaper than usual; high percentile needs exceptional growth or quality.",
  },
  sma20: {
    label: "20SMA",
    detail: "Whether price is above the 20-day simple moving average. Good for short-term timing; a break below can flag a failed entry.",
  },
  sma50: {
    label: "50SMA",
    detail: "Whether price is above the 50-day simple moving average. Helps decide whether a pullback is orderly or momentum is weakening.",
  },
  sma200: {
    label: "200SMA",
    detail: "Whether price is above the 200-day simple moving average. Below it, require stronger valuation/thesis evidence before buying.",
  },
  next: {
    label: "Next",
    detail: "Backend-generated next action from decision models and source coverage. Use it as a prompt for research, hold review, or sell-risk checks.",
  },
} as const;

type ColumnHelpKey = keyof typeof columnHelp;

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
            <SelectItem value="rsRank3m">RS Rank 3M</SelectItem>
            <SelectItem value="revenueGrowth">Rev YoY</SelectItem>
            <SelectItem value="fcfYield">FCF Yield</SelectItem>
            <SelectItem value="fcfMargin">FCF Margin</SelectItem>
            <SelectItem value="relVol1m">RelVol 1M</SelectItem>
            <SelectItem value="atrPct1m">ATR % 1M</SelectItem>
            <SelectItem value="valuationPercentile">Valuation percentile</SelectItem>
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
    <TooltipProvider delayDuration={150}>
      <DataTableFrame title="">
        <table className="watchlist-table w-full min-w-[2200px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <ColumnHeader id="watch" className="w-12 text-center"><Star className="mx-auto size-3.5" aria-label="Watch" /></ColumnHeader>
              <ColumnHeader id="ticker" />
              <ColumnHeader id="company" />
              <ColumnHeader id="price" className="text-right" />
              <ColumnHeader id="marketCap" className="text-right" />
              <ColumnHeader id="ps" className="text-right" />
              <ColumnHeader id="pe" className="text-right" />
              <ColumnHeader id="forwardPe" className="text-right" />
              <ColumnHeader id="revenueGrowth" className="text-right" />
              <ColumnHeader id="fcfYield" className="text-right" />
              <ColumnHeader id="fcfMargin" className="text-right" />
              <ColumnHeader id="roic" className="text-right" />
              <ColumnHeader id="returnYtd" className="text-right" />
              <ColumnHeader id="chart1y" />
              <ColumnHeader id="return1y" className="text-right" />
              <ColumnHeader id="drawdown" />
              <ColumnHeader id="rs3m" className="text-right" />
              <ColumnHeader id="relVol1m" />
              <ColumnHeader id="atrPct1m" />
              <ColumnHeader id="valuationPercentile" />
              <ColumnHeader id="sma20" className="text-center" />
              <ColumnHeader id="sma50" className="text-center" />
              <ColumnHeader id="sma200" className="text-center" />
              <ColumnHeader id="next" />
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
                <td className={cn("px-2 py-2 text-right tabular-nums", growthTone(row.revenueGrowthYoy))}>{formatPercent(row.revenueGrowthYoy, true)}</td>
                <td className={cn("px-2 py-2 text-right tabular-nums", fcfYieldTone(row.fcfYield))}>{formatPercent(row.fcfYield, true)}</td>
                <td className={cn("px-2 py-2 text-right tabular-nums", fcfMarginTone(row.fcfMargin))}>{formatPercent(row.fcfMargin, true)}</td>
                <td className={cn("px-2 py-2 text-right tabular-nums", roicTone(row.roic))}>{formatPercent(row.roic, false)}</td>
                <td className={cn("px-2 py-2 text-right tabular-nums", returnTone(row.returnYtd))}>{formatPercent(row.returnYtd, true)}</td>
                <td className="px-2 py-2"><Sparkline points={row.trend} /></td>
                <td className={cn("px-2 py-2 text-right tabular-nums", returnTone(row.return1y))}>{formatPercent(row.return1y, true)}</td>
                <td className="px-2 py-2"><DrawdownBar value={row.drawdownFromHigh} /></td>
                <td className="px-2 py-2"><RsRankMiniChart rank={row.rsRank3m} bars={row.rsRank3mBars} /></td>
                <td className="px-2 py-2"><VolumeBars value={row.relVol1m} bars={row.relVolBars} /></td>
                <td className="px-2 py-2"><AtrMiniLine value={row.atrPct1m} points={row.atrTrend} /></td>
                <td className="px-2 py-2"><ValuationPercentile value={row.valuationPercentile} /></td>
                <td className="px-2 py-2 text-center"><MaFlag state={row.ma20Up} /></td>
                <td className="px-2 py-2 text-center"><MaFlag state={row.ma50Up} /></td>
                <td className="px-2 py-2 text-center"><MaFlag state={row.ma200Up} /></td>
                <td className="max-w-72 px-2 py-2 text-xs leading-5 text-muted-foreground"><div className="line-clamp-2">{row.nextAction}</div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </DataTableFrame>
    </TooltipProvider>
  );
}

function ColumnHeader({ id, className, children }: { id: ColumnHelpKey; className?: string; children?: ReactNode }) {
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

function VolumeBars({ value, bars }: { value: number; bars: number[] }) {
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

function AtrMiniLine({ value, points }: { value: number; points: number[] }) {
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

function ValuationPercentile({ value }: { value: number }) {
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

function returnTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.5) return "bg-green-100 text-green-900";
  if (value >= 0) return "bg-green-50 text-green-800";
  if (value <= -0.2) return "bg-red-100 text-red-900";
  return "bg-red-50 text-red-800";
}

function growthTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.15) return "bg-green-100 text-green-900";
  if (value >= 0.03) return "bg-green-50 text-green-800";
  if (value >= 0) return "bg-amber-50 text-amber-900";
  if (value <= -0.1) return "bg-red-100 text-red-900";
  return "bg-red-50 text-red-800";
}

function fcfYieldTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.05) return "bg-green-100 text-green-900";
  if (value >= 0.02) return "bg-green-50 text-green-800";
  if (value >= 0) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}

function fcfMarginTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.2) return "bg-green-100 text-green-900";
  if (value >= 0.08) return "bg-green-50 text-green-800";
  if (value >= 0) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}

function roicTone(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value >= 25) return "bg-green-100 text-green-900";
  if (value >= 15) return "bg-green-50 text-green-800";
  if (value < 5) return "bg-red-50 text-red-800";
  return "bg-amber-50 text-amber-900";
}

function multipleTone(value: number, goodMax: number, warnMax: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value <= goodMax) return "bg-green-50 text-green-800";
  if (value <= warnMax) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}
