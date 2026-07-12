import { Star } from "lucide-react";

import { DataTableFrame, EmptyState } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { WatchState, WatchlistRow } from "@/viewModels/watchlist";
import type { OpenTicker } from "@/views/workspacePage";

import {
  AtrMiniLine,
  ColumnHeader,
  DrawdownBar,
  LoadMoreSentinel,
  MaFlag,
  OptionsIvRegime,
  OptionsSkew,
  OptionsStatus,
  RsRankMiniChart,
  Sparkline,
  ValuationPercentile,
  VolumeBars,
  WatchStar,
} from "./cells";
import {
  fcfMarginTone,
  fcfYieldTone,
  formatMarketCap,
  formatMultiple,
  formatPercent,
  formatPrice,
  growthTone,
  multipleTone,
  returnTone,
  roicTone,
} from "./format";

export function WatchlistSection({ title, detail, rows, canLoadMore = false, loadingMore = false, onLoadMore, pendingSymbol, onOpenTicker, onSetWatchState }: { title: string; detail: string; rows: WatchlistRow[]; canLoadMore?: boolean; loadingMore?: boolean; onLoadMore?: () => Promise<void>; pendingSymbol: string | null; onOpenTicker: OpenTicker; onSetWatchState: (symbol: string, currentState: WatchState) => Promise<void> }) {
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

function WatchlistTable({ rows, pendingSymbol, onOpenTicker, onSetWatchState }: { rows: WatchlistRow[]; pendingSymbol: string | null; onOpenTicker: OpenTicker; onSetWatchState: (symbol: string, currentState: WatchState) => Promise<void> }) {
  return (
    <TooltipProvider delayDuration={150}>
      <DataTableFrame title="">
        <table className="watchlist-table w-full min-w-[2500px] text-sm">
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
              <ColumnHeader id="optionsStatus" />
              <ColumnHeader id="optionsIv" />
              <ColumnHeader id="optionsMove" className="text-right" />
              <ColumnHeader id="optionsSkew" />
              <ColumnHeader id="research" />
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
                <td className={cn("px-2 py-2 text-right tabular-nums", multipleTone(row.peRatio, 25, 50))}>{formatMultiple(row.peRatio, row.peStatus === "not_meaningful" ? "N/M" : "-")}</td>
                <td className={cn("px-2 py-2 text-right tabular-nums", multipleTone(row.forwardPe, 25, 50))}>{formatMultiple(row.forwardPe, row.forwardPeStatus === "not_meaningful" ? "N/M" : "-")}</td>
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
                <td className="px-2 py-2"><OptionsStatus status={row.optionsStatus} quality={row.optionsSpreadQuality} /></td>
                <td className="px-2 py-2"><OptionsIvRegime regime={row.optionsIvRegime} /></td>
                <td className="px-2 py-2 text-right tabular-nums">{formatPercent(row.optionsExpectedMovePct, true)}</td>
                <td className="px-2 py-2"><OptionsSkew signal={row.optionsSkewSignal} /></td>
                <td className="px-2 py-2"><ResearchSignal row={row} /></td>
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

function ResearchSignal({ row }: { row: WatchlistRow }) {
  const tone =
    row.researchStatus === "review"
      ? "border-amber-300 bg-amber-50 text-amber-800"
      : row.researchStatus === "memo"
        ? "border-green-300 bg-green-50 text-green-800"
        : row.researchStatus === "packet"
          ? "border-blue-300 bg-blue-50 text-blue-800"
          : "border-border bg-muted text-muted-foreground";
  const evidence = row.researchEvidenceCount ? ` · ${row.researchEvidenceCount.toFixed(0)} ev` : "";
  return (
    <div className="max-w-44 text-xs leading-5" title={row.researchDetail}>
      <span className={cn("inline-flex rounded border px-1.5 py-0.5 font-medium", tone)}>{row.researchLabel}</span>
      {evidence ? <span className="ml-1 text-muted-foreground">{evidence}</span> : null}
    </div>
  );
}
