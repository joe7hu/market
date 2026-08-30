import { AlertTriangle, ArrowDownRight, ArrowUpRight, Plus, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import { previewPortfolioTransaction, recordPortfolioTransaction, reversePortfolioTransaction, type PortfolioTransactionInput, type PortfolioTransactionPreview } from "@/api/portfolio";
import { DecisionCard, EmptyState, StatusBadge } from "@/components/market/workstation";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import type { AppModel } from "@/model";
import type { PanelData, RowRecord, ScopeSnapshotStatus } from "@/types";
import { buildPortfolioViewModel, performanceRangeRows, type PerformanceRange } from "@/viewModels/portfolio";
import { booleanField, displayField, formatMoney, formatPct, listField, numberField, textField, titleLabel, toneFromText } from "./rowFormat";
import { PortfolioPerformanceChart } from "./portfolio/performanceChart";
import { PortfolioImpactCard } from "./TradePlanCard";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

type Props = { data: PanelData; model: AppModel; loading: boolean; scopeStatus?: ScopeSnapshotStatus; onOpenTicker: OpenTicker; onRefresh: (force?: boolean) => Promise<void> };
type TradeForm = { side: "buy" | "sell"; symbol: string; quantity: string; price: string; fees: string; executedAt: string; notes: string; idempotencyKey: string };

const PERFORMANCE_RANGES: PerformanceRange[] = ["1D", "1W", "1M", "YTD", "1Y", "ALL"];

export function PortfolioPage({ data, model, loading, scopeStatus, onOpenTicker, onRefresh }: Props) {
  const [tradeOpen, setTradeOpen] = useState(false);
  const [range, setRange] = useState<PerformanceRange>("1Y");
  const [correlationWindow, setCorrelationWindow] = useState(60);
  const [announcement, setAnnouncement] = useState("");
  const viewModel = buildPortfolioViewModel(data, model, correlationWindow);
  const performanceRows = useMemo(() => performanceRangeRows(viewModel.performanceRows, range), [range, viewModel.performanceRows]);
  const { summary } = viewModel;
  const asOf = formatDateTime(summary.asOf);

  return (
    <WorkspacePage
      eyebrow="Portfolio intelligence"
      title="Portfolio"
      subtitle="A transaction-backed view of performance, concentration, and shared risk—built for the next portfolio decision."
      actions={<>
        <Button type="button" onClick={() => setTradeOpen(true)}><Plus /> Add trade</Button>
        <Button type="button" variant="outline" disabled={loading} onClick={() => void onRefresh()}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>
      </>}
      metrics={[
        ["Portfolio value", formatMoney(summary.portfolioValue), summary.costBasisFallbackCount ? `${summary.costBasisFallbackCount} holding value estimated at cost` : `${model.holdings.length} holdings · ${asOf}`, summary.costBasisFallbackCount ? "warn" : summary.portfolioValue ? "info" : "muted"],
        ["Session P&L", summary.dayPnl === null ? "-" : formatSignedMoney(summary.dayPnl), summary.dayPnlPct === null ? `Needs adjacent sessions · ${formatDate(summary.dayPnlAsOf)}` : `${formatPct(summary.dayPnlPct)} · ${formatDate(summary.dayPnlAsOf)}`, summary.dayPnl === null ? "muted" : summary.dayPnl >= 0 ? "good" : "bad"],
        ["Total P&L", formatSignedMoney(summary.totalPnl), summary.totalPnlPct === null ? "-" : formatPct(summary.totalPnlPct), summary.totalPnl >= 0 ? "good" : "bad"],
        ["Realized P&L", formatSignedMoney(summary.realizedPnl), `${formatMoney(summary.income)} income · ${formatMoney(summary.fees)} fees`, summary.realizedPnl >= 0 ? "good" : "bad"],
      ]}
    >
      <ScopeStatusNotice status={scopeStatus} onRetry={() => void onRefresh(true)} />
      {announcement ? <div role="status" className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{announcement}</div> : null}
      {!model.holdings.length ? <EmptyPortfolio onAddTrade={() => setTradeOpen(true)} /> : null}

      <PortfolioDecisionPanels data={data} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
        <PerformancePanel rows={performanceRows} range={range} onRangeChange={setRange} method={summary.performanceMethod} />
        <AttentionPanel rows={viewModel.riskRows} onOpenTicker={onOpenTicker} />
      </div>

      <HoldingsPanel holdings={model.holdings} onOpenTicker={onOpenTicker} onAddTrade={() => setTradeOpen(true)} />

      {viewModel.proposedImpacts.length ? (
        <section className="space-y-2">
          <div>
            <h2 className="text-base font-semibold">Proposed portfolio impact</h2>
            <p className="text-xs text-muted-foreground">Stored before/after impact for each current selected expression.</p>
          </div>
          <div className="grid gap-3 xl:grid-cols-2">
            {viewModel.proposedImpacts.map((impact) => <PortfolioImpactCard key={impact.impact_id} impact={impact} />)}
          </div>
        </section>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-2">
        <CorrelationPanel rows={viewModel.correlationRows} window={correlationWindow} onWindowChange={setCorrelationWindow} onOpenTicker={onOpenTicker} />
        <ExposurePanel rows={viewModel.exposureClusterRows} />
      </div>

      <ActivityPanel rows={viewModel.transactionRows} onReversed={async (transactionId) => { await onRefresh(true); setAnnouncement(`Transaction ${transactionId.slice(0, 8)} reversed and portfolio history replayed.`); }} />
      <AddTradeSheet open={tradeOpen} onOpenChange={setTradeOpen} holdings={model.holdings} onRecorded={async (symbol) => { try { await onRefresh(true); setAnnouncement(`${symbol} trade recorded. Portfolio, P&L, and risk are reconciled.`); } catch { setAnnouncement(`${symbol} trade recorded, but the displayed portfolio could not refresh. Refresh before recording another trade.`); } }} />
    </WorkspacePage>
  );
}

function PortfolioDecisionPanels({ data }: { data: PanelData }) {
  const risks = data.portfolioRiskCards?.rows ?? [];
  const reviews = data.reviewActions?.rows ?? [];
  const plans = data.tradePlan?.rows ?? data.tickerDecisions?.rows ?? [];
  return <section className="grid gap-4 xl:grid-cols-2" aria-label="Portfolio decision context">
    <Card><CardHeader><CardTitle>Exposures and risk budgets</CardTitle></CardHeader><CardContent className="space-y-2">{[...(data.exposureClusters?.rows ?? []).slice(0, 5), ...risks.slice(0, 3)].length ? [...(data.exposureClusters?.rows ?? []).slice(0, 5), ...risks.slice(0, 3)].map((row, index) => <div key={`${textField(row, ["cluster_id", "card_id", "title"], "risk")}:${index}`} className="flex items-center justify-between gap-3 rounded-md border border-border p-3 text-sm"><span>{textField(row, ["cluster_name", "title", "risk_type"], "Portfolio risk")}</span><span className="text-right text-muted-foreground">{displayField(row, ["portfolio_weight", "budget", "utilization", "summary"], "Unavailable")}</span></div>) : <p className="text-sm text-muted-foreground">No exposure or risk-budget rows are available.</p>}</CardContent></Card>
    <Card><CardHeader><CardTitle>Scenario matrix</CardTitle></CardHeader><CardContent><div className="overflow-x-auto"><table className="w-full min-w-[420px] text-sm"><thead className="border-b border-border text-left text-xs text-muted-foreground"><tr><th className="py-2 pr-3">Scenario</th><th className="py-2 pr-3">Portfolio impact</th><th className="py-2">Evidence</th></tr></thead><tbody>{risks.slice(0, 6).map((row, index) => <tr key={index} className="border-b border-border"><td className="py-2 pr-3 font-medium">{textField(row, ["scenario", "scenario_name", "title"], "Current risk case")}</td><td className="py-2 pr-3">{displayField(row, ["scenario_impact", "impact", "portfolio_impact"], "Unavailable")}</td><td className="py-2 text-muted-foreground">{displayField(row, ["probability", "data_status", "as_of"], "Unavailable")}</td></tr>)}</tbody></table>{!risks.length ? <p className="pt-3 text-sm text-muted-foreground">No scenario matrix is currently published.</p> : null}</div></CardContent></Card>
    <Card><CardHeader><CardTitle>Active plans and proposed before / after impacts</CardTitle></CardHeader><CardContent className="space-y-2">{plans.slice(0, 6).map((row, index) => <div key={`${textField(row, ["trade_plan_id", "ticker", "symbol"], "plan")}:${index}`} className="rounded-md border border-border p-3 text-sm"><div className="flex justify-between gap-3 font-medium"><span>{textField(row, ["ticker", "symbol"], "Active plan")}</span><StatusBadge tone={toneFromText(textField(row, ["eligibility", "status", "action"], "review"))}>{titleLabel(textField(row, ["eligibility", "status", "action"], "review"))}</StatusBadge></div><p className="mt-1 text-muted-foreground">Before: {displayField(row, ["portfolio_before", "before"])} · After: {displayField(row, ["portfolio_after", "after"])}</p></div>)}{!plans.length ? <p className="text-sm text-muted-foreground">No active plan is published.</p> : null}</CardContent></Card>
    <Card><CardHeader><CardTitle>Replacement and funding recommendations</CardTitle></CardHeader><CardContent className="space-y-2">{reviews.slice(0, 6).map((row, index) => <div key={`${textField(row, ["id", "title"], "recommendation")}:${index}`} className="rounded-md border border-border p-3 text-sm"><p className="font-medium">{textField(row, ["title", "action"], "Review recommendation")}</p><p className="mt-1 text-muted-foreground">{displayField(row, ["next_step", "recommendation", "summary"], "No next step recorded.")}</p></div>)}{!reviews.length ? <p className="text-sm text-muted-foreground">No replacement or funding recommendation is published.</p> : null}</CardContent></Card>
  </section>;
}

function PerformancePanel({ rows, range, onRangeChange, method }: { rows: RowRecord[]; range: PerformanceRange; onRangeChange: (value: PerformanceRange) => void; method: string }) {
  const latest = rows.at(-1);
  const latestReturnPct = nullableRowNumber(latest, "total_return_pct");
  return (
    <Card className="min-w-0 overflow-hidden">
      <CardHeader className="gap-3 border-b border-border sm:flex-row sm:items-start sm:justify-between">
        <div><CardTitle className="text-base">Profit & loss</CardTitle><CardDescription className="mt-1">Transaction-derived, external-flow-adjusted performance.</CardDescription></div>
        <div className="flex flex-wrap gap-1" aria-label="Performance range">
          {PERFORMANCE_RANGES.map((value) => <Button key={value} type="button" size="sm" variant={range === value ? "default" : "ghost"} onClick={() => onRangeChange(value)}>{value === "ALL" ? "All" : value}</Button>)}
        </div>
      </CardHeader>
      <CardContent className="p-3 sm:p-4">
        {rows.length ? <>
          <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 px-1"><span className={cn("text-2xl font-semibold", numberField(latest, ["total_pnl"]) < 0 ? "text-red-700" : "text-emerald-700")}>{formatSignedMoney(numberField(latest, ["total_pnl"]))}</span><span className="text-sm text-muted-foreground">{latestReturnPct === null ? "-" : formatPct(latestReturnPct)} on invested capital</span></div>
          <PortfolioPerformanceChart rows={rows} />
          <p className="mt-2 text-xs text-muted-foreground">Method: {method}. Daily closes; benchmark data appears when SPY history overlaps.</p>
        </> : <EmptyState title="Performance begins with a trade" detail="Add a trade, then load daily prices to build the P&L history." />}
      </CardContent>
    </Card>
  );
}

function AttentionPanel({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  return (
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2 text-base"><AlertTriangle className="size-4 text-amber-600" /> Needs attention</CardTitle><CardDescription>Risks with a concrete sizing or data action.</CardDescription></CardHeader>
      <CardContent className="space-y-3">
        {rows.length ? rows.slice(0, 4).map((row) => {
          const symbols = listField(row, ["symbols"]);
          const tone = toneFromText(textField(row, ["severity", "risk_level"]));
          return <DecisionCard key={textField(row, ["card_id", "title"])} title={textField(row, ["title"], "Portfolio review")} status={<StatusBadge tone={tone}>{titleLabel(textField(row, ["severity"], "review"))}</StatusBadge>} reason={textField(row, ["summary"])} evidence={textField(row, ["impact"])} nextAction={textField(row, ["next_step"])} symbols={symbols} tone={tone} />;
        }) : <EmptyState title="No material risks flagged" detail="The current concentration, correlation, drawdown, and quote checks are clear." />}
        {rows.length && listField(rows[0], ["symbols"])[0] ? <Button type="button" variant="outline" className="w-full" onClick={() => onOpenTicker(listField(rows[0], ["symbols"])[0])}>Open top risk ticker</Button> : null}
      </CardContent>
    </Card>
  );
}

function HoldingsPanel({ holdings, onOpenTicker, onAddTrade }: { holdings: AppModel["holdings"]; onOpenTicker: OpenTicker; onAddTrade: () => void }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row items-start justify-between border-b border-border"><div><CardTitle className="text-base">Holdings</CardTitle><CardDescription className="mt-1">Current projection from the transaction ledger and latest owned quotes.</CardDescription></div><Button type="button" variant="outline" size="sm" onClick={onAddTrade}><Plus /> Trade</Button></CardHeader>
      {!holdings.length ? <CardContent className="pt-4"><EmptyState title="No holdings" detail="Add an opening buy to start the portfolio." /></CardContent> : <>
        <div className="grid gap-3 p-3 md:hidden">{holdings.map((holding) => <HoldingCard key={holding.ticker} holding={holding} onOpenTicker={onOpenTicker} />)}</div>
        <div className="hidden overflow-x-auto md:block">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground"><tr><th className="px-4 py-3">Symbol</th><th className="px-3 py-3 text-right">Quantity</th><th className="px-3 py-3 text-right">Avg cost</th><th className="px-3 py-3 text-right">Price</th><th className="px-3 py-3 text-right">Market value</th><th className="px-3 py-3 text-right">Weight</th><th className="px-4 py-3 text-right">Total P&L</th></tr></thead>
            <tbody>{holdings.map((holding) => <tr key={holding.ticker} className="border-b border-border last:border-0"><td className="px-4 py-3"><Button type="button" variant="link" className="h-auto p-0 font-semibold" onClick={() => onOpenTicker(holding.ticker)}>{holding.ticker}</Button></td><td className="px-3 py-3 text-right tabular-nums">{formatNumber(holding.quantity)}</td><td className="px-3 py-3 text-right tabular-nums">{formatMoney(holding.averageCost)}</td><td className="px-3 py-3 text-right tabular-nums">{formatMoney(holding.price)}</td><td className="px-3 py-3 text-right tabular-nums">{formatMoney(holding.marketValue)}</td><td className="px-3 py-3 text-right tabular-nums">{holding.weight.toFixed(1)}%</td><td className={cn("px-4 py-3 text-right tabular-nums", holding.unrealizedPnl < 0 ? "text-red-700" : "text-emerald-700")}>{formatSignedMoney(holding.unrealizedPnl)} <span className="ml-1 text-xs">{formatPct(holding.unrealizedPnlPct)}</span></td></tr>)}</tbody>
          </table>
        </div>
      </>}
    </Card>
  );
}

function HoldingCard({ holding, onOpenTicker }: { holding: AppModel["holdings"][number]; onOpenTicker: OpenTicker }) {
  return <button type="button" onClick={() => onOpenTicker(holding.ticker)} className="rounded-lg border border-border p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><div className="flex items-start justify-between"><div><div className="font-semibold">{holding.ticker}</div><div className="text-xs text-muted-foreground">{formatNumber(holding.quantity)} shares · {holding.weight.toFixed(1)}%</div></div><StatusBadge tone={holding.unrealizedPnl >= 0 ? "good" : "bad"}>{formatPct(holding.unrealizedPnlPct)}</StatusBadge></div><div className="mt-4 grid grid-cols-2 gap-3 text-sm"><div><div className="text-xs text-muted-foreground">Market value</div><div className="font-medium">{formatMoney(holding.marketValue)}</div></div><div><div className="text-xs text-muted-foreground">Total P&L</div><div className={holding.unrealizedPnl < 0 ? "text-red-700" : "text-emerald-700"}>{formatSignedMoney(holding.unrealizedPnl)}</div></div></div></button>;
}

function CorrelationPanel({ rows, window, onWindowChange, onOpenTicker }: { rows: ReturnType<typeof buildPortfolioViewModel>["correlationRows"]; window: number; onWindowChange: (value: number) => void; onOpenTicker: OpenTicker }) {
  return <Card><CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between"><div><CardTitle className="text-base">Shared risk</CardTitle><CardDescription className="mt-1">Daily-return correlation among current holdings. High correlation can turn separate positions into one bet.</CardDescription></div><Select value={String(window)} onValueChange={(value) => onWindowChange(Number(value))}><SelectTrigger className="w-32"><SelectValue /></SelectTrigger><SelectContent>{[20, 60, 120].map((value) => <SelectItem key={value} value={String(value)}>{value} days</SelectItem>)}</SelectContent></Select></CardHeader><CardContent className="space-y-3">{rows.length ? rows.slice(0, 6).map((row) => <button type="button" key={row.id} className="w-full rounded-lg border border-border p-4 text-left hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onOpenTicker(row.symbol)}><div className="flex items-start justify-between gap-3"><div><div className="font-semibold">{row.symbol} / {row.peerSymbol}</div><div className="mt-1 text-xs text-muted-foreground">{row.observations} observations · {row.combinedWeight.toFixed(1)}% combined weight</div></div><StatusBadge tone={toneFromText(row.riskLevel)}>{row.correlation === null ? "Not ready" : row.correlation.toFixed(2)}</StatusBadge></div><p className="mt-3 text-sm leading-6 text-muted-foreground">{row.interpretation}</p></button>) : <EmptyState title="No pairwise correlation yet" detail="At least two holdings and ten overlapping daily returns are required." />}</CardContent></Card>;
}

function ExposurePanel({ rows }: { rows: RowRecord[] }) {
  return <Card><CardHeader><CardTitle className="text-base">Exposure structure</CardTitle><CardDescription>Where the portfolio clusters by asset class, sector, and industry.</CardDescription></CardHeader><CardContent className="space-y-3">{rows.length ? rows.slice(0, 8).map((row) => { const weight = numberField(row, ["portfolio_weight"]); return <div key={textField(row, ["cluster_id"])} className="rounded-lg border border-border p-3"><div className="flex items-center justify-between gap-3"><div><div className="font-medium">{textField(row, ["cluster_name"], "Unclassified")}</div><div className="text-xs text-muted-foreground">{titleLabel(textField(row, ["cluster_type"]))} · {displayField(row, ["symbol_count"])} holdings</div></div><StatusBadge tone={toneFromText(textField(row, ["risk_level"]))}>{weight.toFixed(1)}%</StatusBadge></div><div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted"><div className={cn("h-full rounded-full", weight >= 65 ? "bg-red-600" : weight >= 35 ? "bg-amber-500" : "bg-blue-600")} style={{ width: `${Math.min(100, weight)}%` }} /></div><p className="mt-2 text-xs text-muted-foreground">{textField(row, ["interpretation"])}</p></div>; }) : <EmptyState title="No exposure clusters" detail="Sector and industry metadata will appear as owned instruments are classified." />}</CardContent></Card>;
}

function ActivityPanel({ rows, onReversed }: { rows: RowRecord[]; onReversed: (transactionId: string) => Promise<void> }) {
  const [confirming, setConfirming] = useState("");
  const [reversalKey, setReversalKey] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const reverse = async (transactionId: string) => {
    setBusy(transactionId);
    setError("");
    try {
      await reversePortfolioTransaction(transactionId, reversalKey);
      await onReversed(transactionId);
      setConfirming("");
      setReversalKey("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not reverse this transaction.");
    } finally {
      setBusy("");
    }
  };
  const beginReversal = (transactionId: string) => {
    setConfirming(transactionId);
    setReversalKey(typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `reversal-${Date.now()}`);
  };
  const cancelReversal = () => { setConfirming(""); setReversalKey(""); setError(""); };
  return <Card className="overflow-hidden"><CardHeader><CardTitle className="text-base">Activity</CardTitle><CardDescription>Append-only history. Reversals void an entry and replay every derived position and P&L value.</CardDescription></CardHeader><CardContent>{error ? <div role="alert" className="mb-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div> : null}{rows.length ? <div className="space-y-2">{rows.slice(0, 12).map((row) => { const id = textField(row, ["id"]); const side = textField(row, ["transaction_type"]); const isReversal = booleanField(row, ["is_reversal"]); const isReversed = booleanField(row, ["is_reversed"]); const buyLike = ["buy", "opening_balance", "transfer_in"].includes(side); return <div key={textField(row, ["id", "idempotency_key"])} className={cn("flex flex-wrap items-center gap-3 rounded-lg border border-border p-3", (isReversal || isReversed) && "opacity-60")}><div className={cn("flex size-9 shrink-0 items-center justify-center rounded-full", buyLike ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700")}>{buyLike ? <ArrowDownRight className="size-4" /> : <ArrowUpRight className="size-4" />}</div><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2 font-medium">{titleLabel(side)} {textField(row, ["symbol"], "cash")}{isReversal ? <StatusBadge tone="muted">Reversal</StatusBadge> : isReversed ? <StatusBadge tone="muted">Reversed</StatusBadge> : null}</div><div className="truncate text-xs text-muted-foreground">{formatDateTime(textField(row, ["executed_at"]))}{textField(row, ["notes"]) ? ` · ${textField(row, ["notes"])}` : ""}</div></div><div className="text-right text-sm tabular-nums"><div>{formatNumber(numberField(row, ["quantity"]))} @ {formatMoney(numberField(row, ["price"]))}</div><div className="text-xs text-muted-foreground">{formatMoney(numberField(row, ["amount"]))}</div></div>{!isReversal && !isReversed ? confirming === id ? <div className="flex w-full justify-end gap-2"><Button type="button" size="sm" variant="ghost" disabled={busy === id} onClick={cancelReversal}>Cancel</Button><Button type="button" size="sm" variant="destructive" disabled={busy === id} onClick={() => void reverse(id)}>{busy === id ? "Replaying…" : "Confirm reversal"}</Button></div> : <Button type="button" size="sm" variant="ghost" onClick={() => beginReversal(id)}>Reverse</Button> : null}</div>; })}</div> : <EmptyState title="No activity yet" detail="Confirmed trades appear here and remain auditable." />}</CardContent></Card>;
}

function EmptyPortfolio({ onAddTrade }: { onAddTrade: () => void }) {
  return <Card className="border-dashed"><CardContent className="flex flex-col items-center px-6 py-10 text-center"><div className="mb-4 flex size-12 items-center justify-center rounded-full bg-primary/10"><Plus className="size-5 text-primary" /></div><h2 className="text-lg font-semibold">Start with the first trade</h2><p className="mt-2 max-w-lg text-sm leading-6 text-muted-foreground">Record a buy to create the position ledger. P&L, concentration, and correlation will build from that shared source of truth.</p><Button type="button" className="mt-5" onClick={onAddTrade}><Plus /> Add first trade</Button></CardContent></Card>;
}

function AddTradeSheet({ open, onOpenChange, holdings, onRecorded }: { open: boolean; onOpenChange: (open: boolean) => void; holdings: AppModel["holdings"]; onRecorded: (symbol: string) => Promise<void> }) {
  const [form, setForm] = useState<TradeForm>(() => initialTradeForm());
  const [preview, setPreview] = useState<PortfolioTransactionPreview | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    if (open) {
      setForm(initialTradeForm());
      setPreview(null);
      setError("");
    }
  }, [open]);
  const update = (fields: Partial<TradeForm>) => { setForm((current) => ({ ...current, ...fields })); setPreview(null); setError(""); };
  const payload = (): PortfolioTransactionInput => ({ symbol: form.symbol.trim().toUpperCase(), transaction_type: form.side, quantity: Number(form.quantity), price: Number(form.price), fees: Number(form.fees || 0), executed_at: new Date(form.executedAt).toISOString(), notes: form.notes.trim(), idempotency_key: form.idempotencyKey });
  const onPreview = async (event: FormEvent) => { event.preventDefault(); setSubmitting(true); setError(""); try { setPreview(await previewPortfolioTransaction(payload())); } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not preview this trade."); } finally { setSubmitting(false); } };
  const onConfirm = async () => {
    if (!preview) return;
    setSubmitting(true);
    setError("");
    const trade = { ...payload(), expected_position_version: preview.position_version };
    try {
      await recordPortfolioTransaction(trade);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not record this trade.");
      setSubmitting(false);
      return;
    }
    setForm(initialTradeForm());
    setPreview(null);
    onOpenChange(false);
    setSubmitting(false);
    await onRecorded(trade.symbol ?? "");
  };
  const close = (next: boolean) => { if (!next && !submitting) { setPreview(null); setError(""); } onOpenChange(next); };
  return <Sheet open={open} onOpenChange={close}><SheetContent className="flex w-full flex-col overflow-y-auto sm:max-w-lg"><SheetHeader><SheetTitle>Add trade</SheetTitle><SheetDescription>Preview the position and realized P&L impact before this appends to the portfolio ledger.</SheetDescription></SheetHeader><form className="mt-6 flex flex-1 flex-col gap-5" onSubmit={onPreview}>
    <div className="grid grid-cols-2 gap-3"><FieldLabel label="Side"><Select value={form.side} onValueChange={(value) => update({ side: value as TradeForm["side"] })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="buy">Buy</SelectItem><SelectItem value="sell">Sell</SelectItem></SelectContent></Select></FieldLabel><FieldLabel label="Symbol"><Input value={form.symbol} onChange={(event) => update({ symbol: event.target.value.toUpperCase() })} list="owned-symbols" placeholder="NVDA" required autoFocus /><datalist id="owned-symbols">{holdings.map((holding) => <option key={holding.ticker} value={holding.ticker} />)}</datalist></FieldLabel></div>
    <div className="grid grid-cols-2 gap-3"><FieldLabel label="Quantity"><Input type="number" min="0.000001" step="any" inputMode="decimal" value={form.quantity} onChange={(event) => update({ quantity: event.target.value })} required /></FieldLabel><FieldLabel label="Price"><Input type="number" min="0" step="any" inputMode="decimal" value={form.price} onChange={(event) => update({ price: event.target.value })} required /></FieldLabel></div>
    <div className="grid grid-cols-2 gap-3"><FieldLabel label="Fees"><Input type="number" min="0" step="any" inputMode="decimal" value={form.fees} onChange={(event) => update({ fees: event.target.value })} /></FieldLabel><FieldLabel label="Executed"><Input type="datetime-local" value={form.executedAt} onChange={(event) => update({ executedAt: event.target.value })} required /></FieldLabel></div>
    <FieldLabel label="Notes"><textarea value={form.notes} onChange={(event) => update({ notes: event.target.value })} placeholder="Optional thesis or execution note" className="min-h-24 w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" /></FieldLabel>
    {error ? <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div> : null}
    {preview ? <div className="rounded-lg border border-border bg-muted/40 p-4"><div className="mb-3 font-semibold">Confirm trade impact</div><div className="grid grid-cols-2 gap-4 text-sm"><PreviewMetric label="Notional" value={formatMoney(preview.amount ?? 0)} /><PreviewMetric label="Position" value={`${formatNumber(preview.old_quantity ?? 0)} → ${formatNumber(preview.new_quantity ?? 0)}`} /><PreviewMetric label="Average cost" value={formatMoney(preview.new_average_cost ?? 0)} /><PreviewMetric label="Realized P&L" value={formatSignedMoney(preview.realized_pnl ?? 0)} /></div></div> : null}
    <SheetFooter className="mt-auto border-t border-border pt-5"><Button type="button" variant="outline" disabled={submitting} onClick={() => close(false)}>Cancel</Button>{preview ? <Button type="button" disabled={submitting} onClick={() => void onConfirm()}>{submitting ? "Recording…" : "Confirm trade"}</Button> : <Button type="submit" disabled={submitting}>{submitting ? "Checking…" : "Preview trade"}</Button>}</SheetFooter>
  </form></SheetContent></Sheet>;
}

function FieldLabel({ label, children }: { label: string; children: React.ReactNode }) { return <label className="grid gap-1.5 text-sm font-medium"><span>{label}</span>{children}</label>; }
function PreviewMetric({ label, value }: { label: string; value: string }) { return <div><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 font-medium tabular-nums">{value}</div></div>; }
function initialTradeForm(): TradeForm { const now = new Date(); now.setMinutes(now.getMinutes() - now.getTimezoneOffset()); return { side: "buy", symbol: "", quantity: "", price: "", fees: "0", executedAt: now.toISOString().slice(0, 16), notes: "", idempotencyKey: typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `trade-${Date.now()}` }; }
function formatDateTime(value: string): string { if (!value) return "Awaiting current quote"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); }
function formatDate(value: string): string { if (!value) return "No priced session"; const date = new Date(`${value}T12:00:00`); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
function formatSignedMoney(value: number): string { const formatted = formatMoney(Math.abs(value)); return `${value > 0 ? "+" : value < 0 ? "−" : ""}${formatted}`; }
function formatNumber(value: number): string { return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 6 }) : "-"; }
function nullableRowNumber(row: RowRecord | undefined, key: string): number | null { return !row || row[key] === null || row[key] === undefined ? null : numberField(row, [key]); }
