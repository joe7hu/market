import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { loadPaperPerformance, loadPaperTrades, type PaperFilters, type PaperPerformance, type PaperTrade } from "@/api/paper";
import { PageHeader, MetricTile, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const money = (value: number | null | undefined) => value == null ? "—" : `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "—";

export function PaperBookRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
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

  useEffect(() => {
    setSymbol(filters.symbol ?? "");
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([loadPaperPerformance(filters, controller.signal), loadPaperTrades(filters, 100, null, controller.signal)])
      .then(([nextPerformance, page]) => {
        if (controller.signal.aborted) return;
        setPerformance(nextPerformance);
        setTrades(page.rows);
        setNextCursor(page.next_cursor);
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
    if (!nextCursor) return;
    void loadPaperTrades(filters, 100, nextCursor)
      .then((page) => { setTrades((current) => [...current, ...page.rows]); setNextCursor(page.next_cursor); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Older paper trades unavailable."));
  };

  const points = performance?.series?.points ?? [];
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
      {performance ? <>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricTile label="Verified realized P&L" value={money(performance.realized_pnl)} caption="Journal fills and explicit fees only." tone={performance.realized_pnl == null ? "warn" : "good"} />
          <MetricTile label="Filled / closed" value={`${counts.filled_orders ?? 0} / ${counts.closed_trades ?? 0}`} caption={`${counts.total_orders ?? 0} paper orders in scope.`} />
          <MetricTile label="Reconciled fills" value={String(performance.evidence_coverage?.reconciled_orders ?? 0)} caption="Unknown multipliers or fees remain unresolved." tone="info" />
          <MetricTile label="Current NAV" value="Unavailable" caption="Opening capital and verified current marks are not present in this view." tone="warn" />
        </div>
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-100">
          <strong>Accounting coverage:</strong> {performance.missing_evidence_reasons?.length ? performance.missing_evidence_reasons.join(", ") : "verified realized fills"}. This is not a broker account balance.
        </div>
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1.9fr)]">
          <Card>
            <CardHeader><CardTitle>Verified realized curve</CardTitle></CardHeader>
            <CardContent>
              {points.length ? <div className="space-y-2">{points.map((point) => <div key={`${point.at}-${point.trade_id}`} className="flex items-center justify-between gap-3 border-b border-border py-2 text-sm last:border-0"><span className="text-muted-foreground">{date(point.at)}</span><span className="font-medium">{money(point.cumulative_net_pnl)}</span></div>)}</div> : <p className="text-sm text-muted-foreground">No verified closed fills in this scope. No zero line is being invented.</p>}
              {performance.series?.gaps?.length ? <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">Open positions have no verified current marks, so the curve has a visible coverage gap.</p> : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-3"><CardTitle>Trade blotter</CardTitle><StatusBadge tone={performance.quality_status === "complete" ? "good" : "warn"}>{performance.quality_status}</StatusBadge></CardHeader>
            <CardContent className="p-0">
              {trades.length ? <Table><TableHeader><TableRow><TableHead>Instrument</TableHead><TableHead>Strategy</TableHead><TableHead>Lifecycle</TableHead><TableHead>Entry / mark</TableHead><TableHead>Net P&L</TableHead><TableHead>Evidence</TableHead></TableRow></TableHeader><TableBody>{trades.map((trade) => <TableRow key={trade.paper_order_id}><TableCell><Link className="font-medium underline-offset-4 hover:underline" to={{ pathname: `/portfolio/paper/trades/${trade.paper_order_id}`, search: location.search }}>{trade.symbol}</Link><div className="text-xs text-muted-foreground">{date(trade.decision_at ?? trade.staged_at)}</div></TableCell><TableCell>{trade.strategy?.name ?? trade.strategy?.model_revision ?? "Legacy / unattributed"}</TableCell><TableCell><StatusBadge tone={trade.lifecycle === "closed" ? "good" : trade.lifecycle === "staged" ? "muted" : "info"}>{trade.lifecycle}</StatusBadge></TableCell><TableCell>{money(trade.entry_price)}<div className="text-xs text-muted-foreground">Limit {money(trade.staged_limit_price)}</div></TableCell><TableCell>{money(trade.net_pnl ?? trade.realized_pnl)}</TableCell><TableCell><span className="text-xs">{trade.reconciliation_status}</span></TableCell></TableRow>)}</TableBody></Table> : <p className="p-4 text-sm text-muted-foreground">No paper orders match this scope.</p>}
              {nextCursor ? <div className="border-t border-border p-3"><button type="button" className="text-sm font-medium underline" onClick={loadOlder}>Load older trades</button></div> : null}
            </CardContent>
          </Card>
        </div>
      </> : null}
    </div>
  );
}
