import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { loadPaperPerformance, loadPaperTrades, paperTradesExportUrl, type PaperFilters, type PaperPerformance, type PaperTrade } from "@/api/paper";
import { PaperPerformanceChart } from "@/components/market/PaperPerformanceChart";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { evidenceReason } from "@/presentation/evidence";
import { humanize, lifecycleLabel, money, percent, shortDate, statusLabel, statusTone } from "@/presentation/labels";
import { learningProgress } from "@/presentation/lifecycle";

const PERIODS = { "1m": 30, "3m": 90, "1y": 365 } as const;

function periodStart(period: string): string | undefined {
  const days = PERIODS[period as keyof typeof PERIODS];
  if (!days) return undefined;
  const value = new Date();
  value.setDate(value.getDate() - days);
  return value.toISOString().slice(0, 10);
}

function scopeFrom(searchParams: URLSearchParams): PaperFilters {
  const period = searchParams.get("period") || "3m";
  return {
    book: searchParams.get("book") || "paper",
    sleeve: searchParams.get("sleeve") || undefined,
    symbol: searchParams.get("symbol") || undefined,
    instrument_kind: searchParams.get("instrument_kind") || undefined,
    strategy_revision: searchParams.get("strategy_revision") || undefined,
    lifecycle: searchParams.get("lifecycle") || undefined,
    date_from: searchParams.get("date_from") || periodStart(period),
    date_to: searchParams.get("date_to") || undefined,
    lane: searchParams.get("lane") || undefined,
    structure: searchParams.get("structure") || undefined,
    evidence_class: searchParams.get("evidence_class") || undefined,
    reconciliation_status: searchParams.get("reconciliation_status") || undefined,
  };
}

export function PaperBookRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const filters = scopeFrom(searchParams);
  const period = searchParams.get("period") || "3m";
  const [symbol, setSymbol] = useState(filters.symbol ?? "");
  const [sleeve, setSleeve] = useState(filters.sleeve ?? "");
  const [instrumentKind, setInstrumentKind] = useState(filters.instrument_kind ?? "");
  const [dateFrom, setDateFrom] = useState(filters.date_from ?? "");
  const [dateTo, setDateTo] = useState(filters.date_to ?? "");
  const [structure, setStructure] = useState(filters.structure ?? "");
  const [performance, setPerformance] = useState<PaperPerformance | null>(null);
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paging, setPaging] = useState(false);
  const [loadedScopeKey, setLoadedScopeKey] = useState<string | null>(null);
  const generation = useRef(0);
  const rangeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scopeKey = JSON.stringify(filters);

  useEffect(() => {
    generation.current += 1;
    setPerformance(null);
    setTrades([]);
    setNextCursor(null);
    setLoadedScopeKey(null);
    setPaging(false);
    setSymbol(searchParams.get("symbol") ?? "");
    setSleeve(searchParams.get("sleeve") ?? "");
    setInstrumentKind(searchParams.get("instrument_kind") ?? "");
    setDateFrom(filters.date_from ?? "");
    setDateTo(filters.date_to ?? "");
    setStructure(searchParams.get("structure") ?? "");
    setLoading(true);
    setError(null);
    const controller = new AbortController();
    void loadPaperPerformance(filters, controller.signal)
      .then((nextPerformance) => {
        if (controller.signal.aborted) return;
        setPerformance(nextPerformance);
        setTrades((nextPerformance.trades ?? []) as PaperTrade[]);
        setNextCursor(nextPerformance.next_cursor ?? null);
        setLoadedScopeKey(scopeKey);
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Paper book unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [scopeKey]);

  const updateFilters = useCallback((updates: Record<string, string>) => {
    const next = new URLSearchParams(searchParams);
    Object.entries(updates).forEach(([name, value]) => { if (value) next.set(name, value); else next.delete(name); });
    setSearchParams(next);
  }, [searchParams, setSearchParams]);

  const loadOlder = () => {
    if (!nextCursor || paging || loading) return;
    const requestedGeneration = generation.current;
    setPaging(true);
    void loadPaperTrades(filters, 100, nextCursor)
      .then((page) => { if (requestedGeneration !== generation.current) return; setTrades((current) => [...current, ...page.rows]); setNextCursor(page.next_cursor); })
      .catch((reason) => { if (requestedGeneration === generation.current) setError(reason instanceof Error ? reason.message : "Older paper trades unavailable."); })
      .finally(() => { if (requestedGeneration === generation.current) setPaging(false); });
  };

  const onRangeChange = (from: string, to: string) => {
    if (rangeTimer.current) clearTimeout(rangeTimer.current);
    rangeTimer.current = setTimeout(() => updateFilters({ date_from: from, date_to: to, period: "custom" }), 250);
  };
  const chart = searchParams.get("chart") === "drawdown" ? "drawdown" : "cumulative_net_pnl";
  const visiblePerformance = loadedScopeKey === scopeKey ? performance : null;
  const scopeLoading = loading || (performance !== null && visiblePerformance === null);
  const points = visiblePerformance?.series?.points ?? [];
  const drawdownPoints = visiblePerformance?.series?.drawdown_points ?? [];
  const counts = visiblePerformance?.counts ?? {};
  const hasVerifiedPerformance = (counts.realized_pnl_known ?? 0) > 0;
  const learningNotStarted = Boolean(visiblePerformance && (counts.filled_orders ?? 0) === 0);

  return <div className="space-y-5">
    <PageHeader eyebrow="Portfolio · Paper" title="Paper portfolio" subtitle="See verified performance, what drove it, and which evidence still needs to accumulate." actions={<a className="rounded-md border border-border px-3 py-2 text-sm underline-offset-4 hover:underline" href={paperTradesExportUrl(filters)} download>Export CSV</a>} />
    <PaperFilters period={period} filters={filters} symbol={symbol} sleeve={sleeve} instrumentKind={instrumentKind} dateFrom={dateFrom} dateTo={dateTo} structure={structure} setSymbol={setSymbol} setSleeve={setSleeve} setInstrumentKind={setInstrumentKind} setDateFrom={setDateFrom} setDateTo={setDateTo} setStructure={setStructure} updateFilters={updateFilters} />
    {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
    {scopeLoading ? <PortfolioSkeleton /> : null}
    {!scopeLoading && visiblePerformance ? <>
      <section className="rounded-2xl border border-border bg-card p-5 shadow-sm sm:p-7" aria-label="Paper portfolio summary">
        <div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">Paper portfolio · {period === "custom" ? "Selected range" : period.toUpperCase()}</p><h2 className="mt-2 text-3xl font-semibold tracking-tight sm:text-5xl">{hasVerifiedPerformance ? money(visiblePerformance.net_pnl, true) : "Learning has not started"}</h2><p className="mt-2 max-w-2xl text-sm text-muted-foreground">{hasVerifiedPerformance ? "Verified realized P&L from fill-backed exits. Open P&L is shown only when current marks are authoritative." : "The system has not accumulated a verified exit series in this scope yet."}</p></div><StatusBadge tone={statusTone(visiblePerformance.quality_status)}>{statusLabel(visiblePerformance.quality_status)}</StatusBadge></div>
        {learningNotStarted ? <LearningLifecycle performance={visiblePerformance} /> : <div className="mt-7 grid gap-3 sm:grid-cols-2 xl:grid-cols-5"><SummaryMetric label="Realized P&L" value={money(visiblePerformance.realized_pnl, true)} note={visiblePerformance.realized_pnl_status === "complete" ? "Verified fills" : "Some exits need evidence"} /><SummaryMetric label="Open P&L" value={money(visiblePerformance.unrealized_pnl, true)} note={visiblePerformance.open_exposure_status === "complete" ? "Current marks verified" : "Mark coverage incomplete"} /><SummaryMetric label="Drawdown" value={money(visiblePerformance.drawdown, true)} note="From verified realized curve" /><SummaryMetric label="Closed trades" value={String(counts.closed_trades ?? 0)} note={`${counts.filled_orders ?? 0} filled positions`} /><SummaryMetric label="Evidence" value={`${counts.reconciled_orders ?? 0} / ${counts.filled_orders ?? 0}`} note="Fill records reconciled" /></div>}
      </section>
      {!learningNotStarted ? <>
        <PerformanceVisual performance={visiblePerformance} chart={chart} points={points} drawdownPoints={drawdownPoints} onChart={(value) => updateFilters({ chart: value })} onSelect={(tradeId) => navigate(`/portfolio/paper/trades/${encodeURIComponent(tradeId)}${location.search}`)} onRangeChange={onRangeChange} />
        <Attribution performance={visiblePerformance} />
      </> : null}
      <TradeBlotter trades={trades} performance={visiblePerformance} locationSearch={location.search} onLoadOlder={loadOlder} paging={paging} />
    </> : null}
  </div>;
}

function PaperFilters({ period, filters, symbol, sleeve, instrumentKind, dateFrom, dateTo, structure, setSymbol, setSleeve, setInstrumentKind, setDateFrom, setDateTo, setStructure, updateFilters }: { period: string; filters: PaperFilters; symbol: string; sleeve: string; instrumentKind: string; dateFrom: string; dateTo: string; structure: string; setSymbol: (value: string) => void; setSleeve: (value: string) => void; setInstrumentKind: (value: string) => void; setDateFrom: (value: string) => void; setDateTo: (value: string) => void; setStructure: (value: string) => void; updateFilters: (updates: Record<string, string>) => void }) {
  const applyAdvanced = (event: FormEvent) => {
    event.preventDefault();
    updateFilters({ symbol: symbol.trim().toUpperCase(), sleeve: sleeve.trim(), instrument_kind: instrumentKind.trim().toLowerCase(), date_from: dateFrom, date_to: dateTo, structure: structure.trim().toLowerCase(), period: "custom" });
  };
  return <form className="rounded-xl border border-border bg-card p-3" onSubmit={applyAdvanced}>
    <div className="flex flex-wrap items-center gap-2"><span className="mr-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">View</span><Select value={period} onValueChange={(value) => updateFilters({ period: value, date_from: value === "all" ? "" : periodStart(value) ?? "" })}><SelectTrigger className="w-36" aria-label="Performance period"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="1m">1 month</SelectItem><SelectItem value="3m">3 months</SelectItem><SelectItem value="1y">1 year</SelectItem><SelectItem value="all">All history</SelectItem><SelectItem value="custom">Custom range</SelectItem></SelectContent></Select><Select value={filters.lifecycle ?? "all"} onValueChange={(value) => updateFilters({ lifecycle: value === "all" ? "" : value })}><SelectTrigger className="w-36" aria-label="Trade status"><SelectValue placeholder="All status" /></SelectTrigger><SelectContent><SelectItem value="all">All status</SelectItem><SelectItem value="open">Open</SelectItem><SelectItem value="closed">Closed</SelectItem><SelectItem value="staged">Staged</SelectItem></SelectContent></Select><span className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">All strategies</span><details className="ml-auto"><summary className="cursor-pointer rounded-md border border-border px-3 py-2 text-sm hover:bg-accent">More filters</summary><div className="mt-3 grid gap-2 border-t border-border pt-3 sm:grid-cols-2 lg:grid-cols-4"><Input aria-label="Filter symbol" placeholder="Symbol" value={symbol} onChange={(event) => setSymbol(event.target.value)} /><Input aria-label="Filter sleeve" placeholder="Sleeve" value={sleeve} onChange={(event) => setSleeve(event.target.value)} /><Input aria-label="Filter instrument kind" placeholder="Asset type" value={instrumentKind} onChange={(event) => setInstrumentKind(event.target.value)} /><Input aria-label="Filter structure" placeholder="Structure" value={structure} onChange={(event) => setStructure(event.target.value)} /><Input aria-label="Date from" type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /><Input aria-label="Date to" type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /><Input aria-label="Strategy revision" placeholder="Strategy version" value={filters.strategy_revision ?? ""} onChange={(event) => updateFilters({ strategy_revision: event.target.value })} /><Select value={filters.evidence_class ?? "all"} onValueChange={(value) => updateFilters({ evidence_class: value === "all" ? "" : value })}><SelectTrigger aria-label="Evidence coverage"><SelectValue placeholder="All evidence" /></SelectTrigger><SelectContent><SelectItem value="all">All evidence</SelectItem><SelectItem value="verified">Verified</SelectItem><SelectItem value="partial">Some missing</SelectItem><SelectItem value="unavailable">Unavailable</SelectItem></SelectContent></Select><button type="submit" className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground">Apply filters</button><button type="button" className="rounded-md border border-border px-3 py-2 text-sm" onClick={() => updateFilters({ symbol: "", sleeve: "", instrument_kind: "", structure: "", date_from: periodStart("3m") ?? "", date_to: "", strategy_revision: "", evidence_class: "", period: "3m" })}>Reset</button></div></details></div>
  </form>;
}

function SummaryMetric({ label, value, note }: { label: string; value: string; note: string }) {
  return <div className="rounded-xl border border-border bg-background/70 p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p><p className="mt-2 text-2xl font-semibold">{value}</p><p className="mt-1 text-xs text-muted-foreground">{note}</p></div>;
}

function LearningLifecycle({ performance }: { performance: PaperPerformance }) {
  const filled = performance.counts?.filled_orders ?? 0;
  const staged = Math.max(0, (performance.counts?.total_orders ?? 0) - filled);
  const progress = learningProgress(filled, 1, 1);
  const blocker = filled ? performance.missing_evidence_reasons?.[0] : "no_paper_fills";
  return <div className="mt-8 rounded-xl border border-dashed border-primary/30 bg-primary/[0.04] p-5"><div className="flex flex-wrap items-end justify-between gap-3"><div><h3 className="text-lg font-semibold">Paper learning has not started yet</h3><p className="mt-1 text-sm text-muted-foreground">The system currently has no filled paper positions. A paper-ready decision must be staged and filled before performance learning begins.</p></div><span className="text-sm font-medium text-primary">Collection {filled} / 1</span></div><div className="mt-5 h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-all" style={{ width: `${progress.percent}%` }} /></div><div className="mt-4 grid gap-3 sm:grid-cols-3"><div><p className="text-xs uppercase tracking-wide text-muted-foreground">Collection</p><p className="mt-1 font-medium">{filled} paper fill{filled === 1 ? "" : "s"}</p>{staged ? <p className="mt-1 text-xs text-muted-foreground">{staged} staged or pending decision{staged === 1 ? "" : "s"}</p> : null}</div><div><p className="text-xs uppercase tracking-wide text-muted-foreground">Next action</p><p className="mt-1 font-medium">Stage a paper-ready decision</p><Link className="mt-2 inline-block text-xs font-medium text-primary hover:underline" to="/opportunities">Review current opportunities →</Link></div><div><p className="text-xs uppercase tracking-wide text-muted-foreground">Biggest blocker</p><p className="mt-1 font-medium">{evidenceReason(blocker)}</p></div></div></div>;
}

function PerformanceVisual({ performance, chart, points, drawdownPoints, onChart, onSelect, onRangeChange }: { performance: PaperPerformance; chart: string; points: Array<{ at: string; cumulative_net_pnl: number; trade_id: string }>; drawdownPoints: Array<{ at: string; drawdown: number; trade_id: string }>; onChart: (value: string) => void; onSelect: (tradeId: string) => void; onRangeChange: (from: string, to: string) => void }) {
  const drawdown = chart === "drawdown";
  const chartPoints = drawdown ? drawdownPoints.map((point) => ({ ...point, value: point.drawdown })) : points.map((point) => ({ ...point, value: point.cumulative_net_pnl }));
  const events = performance.series?.event_markers ?? [];
  return <Card><CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"><div><CardTitle>Performance and trade events</CardTitle><p className="mt-1 text-sm text-muted-foreground">{drawdown ? "Drawdown from the verified realized curve." : "Cumulative verified realized P&L with entries, exits, open positions and strategy observations."}</p></div><Select value={chart} onValueChange={onChart}><SelectTrigger className="w-48" aria-label="Performance chart mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="cumulative_net_pnl">Cumulative P&L</SelectItem><SelectItem value="drawdown">Drawdown</SelectItem></SelectContent></Select></CardHeader><CardContent>{chartPoints.length ? <PaperPerformanceChart label={drawdown ? "Verified realized drawdown" : "Cumulative verified realized P&L"} mode={drawdown ? "drawdown" : "cumulative"} points={chartPoints} events={events} onSelect={onSelect} onRangeChange={onRangeChange} /> : <div className="flex h-[26rem] items-center justify-center rounded-xl border border-dashed border-border bg-muted/20 p-6 text-center"><div><p className="font-medium">The curve will appear after the first verified exit</p><p className="mt-1 text-sm text-muted-foreground">Entries and open positions can be tracked now; no zero line is being invented for missing P&L.</p></div></div>}<div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground"><span><i className="mr-1 inline-block size-2 rounded-full bg-emerald-600" />Entry</span><span><i className="mr-1 inline-block size-2 rounded-full bg-red-600" />Exit</span><span><i className="mr-1 inline-block size-2 rounded-full bg-amber-500" />Partial exit</span><span><i className="mr-1 inline-block size-2 rounded-full bg-violet-600" />Open position</span></div>{events.length ? <details className="mt-4"><summary className="cursor-pointer text-sm font-medium">Event list</summary><div className="mt-2 grid gap-1 sm:grid-cols-2">{events.slice(0, 40).map((event, index) => <Link key={`${event.at}-${event.kind}-${index}`} to={event.trade_id ? `/portfolio/paper/trades/${encodeURIComponent(event.trade_id)}` : "#"} className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-xs hover:bg-accent"><span><strong>{humanize(event.symbol, "Portfolio")}</strong> · {event.label ?? humanize(event.kind)}</span><span className="text-muted-foreground">{shortDate(event.at)} · {money(event.pnl, true)}</span></Link>)}</div></details> : null}</CardContent></Card>;
}

function Attribution({ performance }: { performance: PaperPerformance }) {
  const attribution = performance.attribution ?? {};
  const dimensions = [["strategy", "By strategy"], ["symbol", "By ticker"], ["structure", "By structure"], ["holding_period", "By holding period"], ["confidence", "By confidence"]] as const;
  return <section><div className="mb-3"><h2 className="text-xl font-semibold">What drove performance?</h2><p className="text-sm text-muted-foreground">Only verified realized exits are included. Open or unsupported marks stay out of the attribution.</p></div><div className="grid gap-4 xl:grid-cols-2">{dimensions.map(([key, label]) => <AttributionCard key={key} title={label} rows={attribution[key] ?? []} />)}</div></section>;
}

function AttributionCard({ title, rows }: { title: string; rows: Array<{ label: string; pnl: number; trades: number; wins: number; win_rate: number | null; average_pnl: number | null }> }) {
  const max = Math.max(1, ...rows.map((row) => Math.abs(row.pnl)));
  return <Card><CardHeader><CardTitle className="text-base">{title}</CardTitle></CardHeader><CardContent>{rows.length ? <div className="space-y-3">{rows.slice(0, 8).map((row) => <div key={row.label}><div className="flex items-center justify-between gap-3 text-sm"><span className="font-medium">{humanize(row.label)}</span><strong className={row.pnl >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-red-700 dark:text-red-300"}>{money(row.pnl, true)}</strong></div><div className="mt-1 flex items-center justify-between text-xs text-muted-foreground"><span>{row.trades} trade{row.trades === 1 ? "" : "s"} · {percent(row.win_rate)} win rate</span><span>{money(row.average_pnl, true)} average</span></div><div className="mt-2 h-1.5 rounded-full bg-muted"><div className={`h-full rounded-full ${row.pnl >= 0 ? "bg-emerald-600" : "bg-red-500"}`} style={{ width: `${Math.min(100, Math.max(8, Math.abs(row.pnl) / max * 100))}%` }} /></div></div>)}</div> : <p className="text-sm text-muted-foreground">This breakdown will appear after the first verified exit.</p>}</CardContent></Card>;
}

function TradeBlotter({ trades, performance, locationSearch, onLoadOlder, paging }: { trades: PaperTrade[]; performance: PaperPerformance; locationSearch: string; onLoadOlder: () => void; paging: boolean }) {
  return <Card><CardHeader className="flex flex-row items-center justify-between gap-3"><div><CardTitle>Trade decisions</CardTitle><p className="mt-1 text-sm text-muted-foreground">Open a position to see the decision, holding path, exit, and learning note.</p></div><StatusBadge tone={statusTone(performance.quality_status)}>{statusLabel(performance.quality_status)}</StatusBadge></CardHeader><CardContent className="p-0">{trades.length ? <div className="divide-y divide-border">{trades.map((trade) => <Link key={String(trade.paper_order_id)} to={`/portfolio/paper/trades/${encodeURIComponent(String(trade.paper_order_id))}${locationSearch}`} className="grid gap-2 px-4 py-4 transition-colors hover:bg-accent sm:grid-cols-[1.4fr_1fr_1fr_auto] sm:items-center"><div><div className="font-semibold">{humanize(trade.symbol, "Unattributed position")}</div><div className="text-xs text-muted-foreground">{humanize(trade.structure, "Instrument")}{trade.strategy?.name ? ` · ${trade.strategy.name}` : ""}</div></div><div><div className="text-xs uppercase tracking-wide text-muted-foreground">Status</div><div className="mt-1 text-sm font-medium">{lifecycleLabel(trade.lifecycle)}</div></div><div><div className="text-xs uppercase tracking-wide text-muted-foreground">P&L</div><div className="mt-1 text-sm font-medium">{money(trade.net_pnl ?? trade.realized_pnl, true)}</div></div><div className="text-sm text-primary">View decision →</div></Link>)}</div> : <div className="p-6 text-sm text-muted-foreground">No paper orders match this scope.</div>}{performance.next_cursor || (trades.length >= 100) ? <div className="border-t border-border p-3"><button type="button" className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50" disabled={paging} onClick={onLoadOlder}>{paging ? "Loading older trades…" : "Load older trades"}</button></div> : null}</CardContent></Card>;
}

function PortfolioSkeleton() {
  return <div className="space-y-4" aria-label="Loading paper portfolio"><div className="h-48 animate-pulse rounded-2xl bg-muted" /><div className="grid gap-4 lg:grid-cols-2"><div className="h-96 animate-pulse rounded-xl bg-muted" /><div className="h-96 animate-pulse rounded-xl bg-muted" /></div></div>;
}
