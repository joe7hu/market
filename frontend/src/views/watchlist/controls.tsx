import { Plus, RefreshCw, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type { WatchlistFilters, WatchlistSort } from "@/viewModels/watchlist";

import { CountPill } from "./cells";
import type { WatchlistRefreshStatus } from "./columns";
import { refreshStatusText } from "./format";

export function WatchlistRefreshAction({ status, finishedAt, error, onRefresh }: { status: WatchlistRefreshStatus; finishedAt: Date | null; error: string | null; onRefresh: () => Promise<void> }) {
  const running = status === "starting" || status === "running";
  const buttonLabel = running ? "Refreshing" : status === "failed" ? "Retry refresh" : "Refresh all";
  const statusText = refreshStatusText(status, finishedAt, error);
  return (
    <div className="flex flex-col items-start gap-1 sm:items-end">
      <Button type="button" variant="outline" className="gap-2" disabled={running} onClick={() => void onRefresh()}>
        <RefreshCw className={cn("size-4", running && "animate-spin")} />
        {buttonLabel}
      </Button>
      <div className={cn("text-xs tabular-nums", status === "failed" ? "text-red-700" : "text-muted-foreground")} aria-live="polite">
        {statusText}
      </div>
    </div>
  );
}

export function WatchlistControls({ filters, counts, totalRows, visibleRows, newSymbol, pending, onNewSymbolChange, onAddSymbol, onChange }: { filters: WatchlistFilters; counts: { watched: number; owned: number; unwatched: number; momentum: number; quality: number; value: number }; totalRows: number; visibleRows: number; newSymbol: string; pending: boolean; onNewSymbolChange: (value: string) => void; onAddSymbol: () => void; onChange: <K extends keyof WatchlistFilters>(key: K, value: WatchlistFilters[K]) => void }) {
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
