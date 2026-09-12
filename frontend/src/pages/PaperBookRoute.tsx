import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { loadPaperPerformance, loadPaperTrades, type PaperFilters, type PaperPerformance, type PaperTrade } from "@/api/paper";
import { PageHeader, MetricTile, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PaperPerformanceChart } from "@/components/market/PaperPerformanceChart";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const money = (value: number | null | undefined) => value == null ? "—" : `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "—";

export function PaperBookRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const filters: PaperFilters = {
    symbol: searchParams.get("symbol") || undefined,
    strategy_revision: searchParams.get("strategy_revision") || undefined,
    lifecycle: searchParams.get("lifecycle") || undefined,
  };
  const [symbol, setSymbol] = useState(filters.symbol ?? "");
  const [performance, setPerformance] = useState<PaperPerformance | null>(null);
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paging, setPaging] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    generation.current += 1;
    setPaging(false);
    setSymbol(filters.symbol ?? "");
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadPaperPerformance(filters, controller.signal)
      .then((nextPerformance) => {
        if (controller.signal.aborted) return;
        setPerformance(nextPerformance);
        setTrades(nextPerformance.trades ?? []);
        setNextCursor(nextPerformance.next_cursor ?? null);
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Paper book unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [filters.symbol, filters.strategy_revision, filters.lifecycle]);

  const updateFilter = (name: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value); else next.delete(name);
    setSearchParams(next);
  };

  const loadOlder = () => {
    if (!nextCursor || paging || loading) return;
    const requestedGeneration = generation.current;
    setPaging(true);
    void loadPaperTrades(filters, 100, nextCursor)
      .then((page) => { if (requestedGeneration !== generation.current) return; setTrades((current) => [...current, ...page.rows]); setNextCursor(page.next_cursor); })
      .catch((reason) => { if (requestedGeneration === generation.current) setError(reason instanceof Error ? reason.message : "Older paper trades unavailable."); })
      .finally(() => { if (requestedGeneration === generation.current) setPaging(false); });
  };

  const points = performance?.series?.points ?? [];
  const drawdownPoints = performance?.series?.drawdown_points ?? [];
  const chart = searchParams.get("chart") === "drawdown" ? "drawdown" : "cumulative_net_pnl";
  const counts = performance?.counts ?? {};

  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Portfolio · Paper" title="Paper book" subtitle="Fill-backed paper history with explicit accounting coverage. Staged limits, shadow observations, and unverified marks stay separate." />
        <form className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3 sm:flex-row" onSubmit={(event) => { event.preventDefault(); updateFilter("symbol", symbol.trim().toUpperCase()); }}>
        <Input aria-label="Filter symbol" placeholder="All symbols" value={symbol} onChange={(event) => setSymbol(event.target.value)} className="sm:max-w-xs" />
        <Select value={filters.lifecycle ?? "all"} onValueChange={(value) => updateFilter("lifecycle", value === "all" ? "" : value)}>
          <SelectTrigger className="sm:w-44"><SelectValue placeholder="All lifecycles" /></SelectTrigger>
          <SelectContent><SelectItem value="all">All lifecycles</SelectItem><SelectItem value="staged">Staged</SelectItem><SelectItem value="open">Open</SelectItem><SelectItem value="closed">Closed</SelectItem></SelectContent>
        </Select>
        </form>
      {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
      {loading && !performance ? <p className="text-sm text-muted-foreground">Loading paper evidence…</p> : null}
      {performance && (loading || error) ? <p role="status" className="text-sm text-amber-700">Previous snapshot from {date(performance.as_of)}. {loading ? "Updating the selected scope…" : "Refresh failed; values below are stale and may belong to the previous scope."}</p> : null}
      {performance ? <>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricTile label="Verified realized P&L" value={money(performance.realized_pnl)} caption="Journal fills and explicit fees only." tone={performance.realized_pnl_status === "complete" ? "good" : "warn"} />
          <MetricTile label="Verified unrealized P&L" value={money(performance.unrealized_pnl)} caption="Open exposure only; stale or missing marks stay unknown." tone={performance.unrealized_pnl == null ? "warn" : "info"} />
          <MetricTile label="Filled / closed" value={`${counts.filled_orders ?? 0} / ${counts.closed_trades ?? 0}`} caption={`${counts.total_orders ?? 0} paper orders in scope.`} />
          <MetricTile label="Current NAV" value={money(performance.nav)} caption={performance.nav == null ? "Opening capital and flow history are unavailable." : "Verified paper-book NAV."} tone={performance.nav == null ? "warn" : "good"} />
        </div>
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-100">
          <strong>Accounting coverage:</strong> {performance.missing_evidence_reasons?.length ? performance.missing_evidence_reasons.join(", ") : "verified realized fills"}. This is not a broker account balance.
        </div>
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1.9fr)]">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-3"><CardTitle>{chart === "drawdown" ? "Drawdown curve" : "Verified realized curve"}</CardTitle><Select value={chart} onValueChange={(value) => updateFilter("chart", value)}><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="cumulative_net_pnl">Cumulative net P&L</SelectItem><SelectItem value="drawdown">Drawdown</SelectItem></SelectContent></Select></CardHeader>
            <CardContent>
              {chart === "drawdown" && !drawdownPoints.length ? <p className="text-sm text-muted-foreground">Drawdown is unavailable until the realized series is complete. No zero line is being invented.</p> : null}
              {chart === "cumulative_net_pnl" && !points.length ? <p className="text-sm text-muted-foreground">No verified closed fills in this scope. No zero line is being invented.</p> : null}
              {(chart === "drawdown" ? drawdownPoints : points).length ? <PaperPerformanceChart label={chart === "drawdown" ? "Drawdown of verified realized P&L" : "Cumulative verified realized P&L"} points={chart === "drawdown" ? drawdownPoints.map((point) => ({ ...point, value: point.drawdown })) : points.map((point) => ({ ...point, value: point.cumulative_net_pnl }))} onSelect={(tradeId) => navigate(`/portfolio/paper/trades/${tradeId}${location.search}`)} /> : null}
              <p className="text-xs text-muted-foreground">Realized fill events only. Open-position valuations are excluded from this series. Select a point to inspect its trade.</p>
              <details className="mt-3"><summary className="cursor-pointer text-sm">Accessible event table</summary><div className="max-h-72 overflow-auto space-y-2">{chart === "drawdown" ? drawdownPoints.map((point) => <Link key={`${point.at}-${point.trade_id}`} to={`/portfolio/paper/trades/${point.trade_id}${location.search}`} className="flex items-center justify-between gap-3 border-b border-border py-2 text-sm last:border-0 hover:bg-accent"><span className="text-muted-foreground">{date(point.at)}</span><span className="font-medium">{money(point.drawdown)}</span></Link>) : points.map((point) => <Link key={`${point.at}-${point.trade_id}`} to={`/portfolio/paper/trades/${point.trade_id}${location.search}`} className="flex items-center justify-between gap-3 border-b border-border py-2 text-sm last:border-0 hover:bg-accent"><span className="text-muted-foreground">{date(point.at)}</span><span className="font-medium">{money(point.cumulative_net_pnl)}</span></Link>)}</div></details>
              {performance.series?.gaps?.length ? <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">Some open positions have stale, missing, or unreconciled marks, so the current book total is incomplete.</p> : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-3"><CardTitle>Trade blotter</CardTitle><StatusBadge tone={performance.quality_status === "complete" ? "good" : "warn"}>{performance.quality_status}</StatusBadge></CardHeader>
            <CardContent className="p-0">
              {trades.length ? <Table><TableHeader><TableRow><TableHead>Instrument</TableHead><TableHead>Strategy</TableHead><TableHead>Lifecycle</TableHead><TableHead>Entry / mark</TableHead><TableHead>Realized P&L</TableHead><TableHead>Net P&L</TableHead><TableHead>Evidence</TableHead></TableRow></TableHeader><TableBody>{trades.map((trade) => <TableRow key={trade.paper_order_id}><TableCell><Link className="font-medium underline-offset-4 hover:underline" to={{ pathname: `/portfolio/paper/trades/${trade.paper_order_id}`, search: location.search }}>{trade.symbol}</Link><div className="text-xs text-muted-foreground">{date(trade.decision_at ?? trade.staged_at)}</div></TableCell><TableCell>{trade.strategy?.name ?? trade.strategy?.model_revision ?? "Legacy / unattributed"}</TableCell><TableCell><StatusBadge tone={trade.lifecycle === "closed" ? "good" : trade.lifecycle === "staged" ? "muted" : "info"}>{trade.lifecycle}</StatusBadge></TableCell><TableCell>{money(trade.entry_price)}<div className="text-xs text-muted-foreground">Mark {money(trade.mark_price)} · {trade.mark_status ?? "unavailable"}</div></TableCell><TableCell>{money(trade.realized_pnl)}</TableCell><TableCell>{money(trade.net_pnl)}</TableCell><TableCell><span className="text-xs">{trade.reconciliation_status}</span></TableCell></TableRow>)}</TableBody></Table> : <p className="p-4 text-sm text-muted-foreground">No paper orders match this scope.</p>}
              {nextCursor ? <div className="border-t border-border p-3"><button type="button" className="text-sm font-medium underline" disabled={paging || loading} onClick={loadOlder}>{paging ? "Loading…" : "Load older trades"}</button></div> : null}
            </CardContent>
          </Card>
        </div>
      </> : null}
    </div>
  );
}
