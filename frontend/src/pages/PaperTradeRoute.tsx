import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { loadPaperTrade, type PaperTrade } from "@/api/paper";
import { StoredEvidence, TechnicalDetails } from "@/components/market/StoredEvidence";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { eventLabel } from "@/presentation/actions";
import { safeRecord } from "@/presentation/evidence";
import { humanize, lifecycleLabel, money, percent, dateTime, statusLabel, statusTone } from "@/presentation/labels";

type TimelineEvent = { label: string; at: string | null; kind: string; quantity?: number | null; price?: number | null; pnl?: number | null };

export function PaperTradeRoute() {
  const { tradeId } = useParams();
  const location = useLocation();
  const [trade, setTrade] = useState<PaperTrade | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!tradeId) return;
    setTrade(null);
    setError(null);
    const controller = new AbortController();
    void loadPaperTrade(tradeId, controller.signal).then((value) => { if (!controller.signal.aborted) setTrade(value); }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Trade unavailable."); });
    return () => controller.abort();
  }, [tradeId]);

  const timeline = useMemo(() => trade ? buildTimeline(trade) : [], [trade]);
  const decision = safeRecord(trade?.decision);
  const thesis = safeRecord(decision.thesis);
  const forecast = safeRecord(decision.forecast);
  const execution = safeRecord(trade?.execution);
  const outcome = safeRecord(trade?.outcome);
  const evidence = Array.isArray(decision.evidence) ? decision.evidence : [];
  const title = trade ? `${trade.symbol ?? "Paper position"}${trade.strike ? ` ${trade.strike}${trade.option_type ? String(trade.option_type).slice(0, 1).toUpperCase() : ""}` : ""}${trade.expiration ? ` · ${new Date(String(trade.expiration)).toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : ""}` : "Paper position";

  return <div className="space-y-5">
    <Link className="text-sm text-muted-foreground underline-offset-4 hover:underline" to={{ pathname: "/portfolio/paper", search: location.search }}>← Back to paper portfolio</Link>
    {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
    {!trade && !error ? <p className="text-sm text-muted-foreground">Loading decision…</p> : null}
    {trade ? <>
      <PageHeader eyebrow={`Paper position · ${humanize(trade.symbol, "Unattributed")}`} title={title} subtitle="A decision-led view of entry, holding path, exit, and what the evidence can teach us." actions={<StatusBadge tone={statusTone(trade.reconciliation_status)}>{statusLabel(trade.reconciliation_status, "Evidence unavailable")}</StatusBadge>} />
      <TradeTimeline events={timeline} />
      <MiniTradeCharts trade={trade} timeline={timeline} />
      <div className="grid gap-4 lg:grid-cols-2"><HumanPanel title="Why we traded"><p>{String(thesis.core_thesis ?? thesis.thesis ?? "No stored thesis is available.")}</p><div className="mt-4 grid gap-3 sm:grid-cols-3"><Metric label="Probability" value={forecast.probability_profit == null ? "—" : percent(forecast.probability_profit)} /><Metric label="Expected value" value={money(forecast.expected_value, true)} /><Metric label="Risk allocation" value={money(trade.initial_risk)} /></div>{Array.isArray(decision.reasons) && decision.reasons.length ? <div className="mt-4 rounded-lg bg-muted/60 p-3 text-sm"><p className="font-medium">Selection rationale</p><ul className="mt-2 list-disc space-y-1 pl-5">{decision.reasons.map((reason: unknown, index: number) => <li key={index}>{humanize(reason)}</li>)}</ul></div> : null}</HumanPanel><HumanPanel title="What could invalidate it"><p>{String(thesis.invalidation ?? thesis.falsification ?? "No specific invalidation condition was stored.")}</p>{Array.isArray(decision.blockers) && decision.blockers.length ? <p className="mt-3 text-sm text-muted-foreground">At decision time: {decision.blockers.map((blocker: unknown) => humanize(blocker)).join("; ")}</p> : null}</HumanPanel></div>
      <div className="grid gap-4 lg:grid-cols-2"><HumanPanel title="What happened"><div className="grid gap-3 sm:grid-cols-2"><Metric label="Lifecycle" value={lifecycleLabel(trade.lifecycle)} /><Metric label="Holding period" value={holdingPeriod(timeline)} /><Metric label="Realized P&L" value={money(trade.realized_pnl, true)} /><Metric label="Net P&L" value={money(trade.net_pnl, true)} /><Metric label="MFE / MAE" value={`${money((outcome as any).mfe, true)} / ${percent(outcome.mae)}`} /><Metric label="Exit reason" value={humanize(outcome.exit_reason ?? outcome.state, "Not recorded")} /></div><p className="mt-4 text-sm text-muted-foreground">{trade.mark_status === "verified" ? "The current mark is backed by an authoritative quote." : "Current open P&L is not claimed because the mark is not fully verified."}</p></HumanPanel><HumanPanel title="What the system learned"><p>{String(outcome.learning_note ?? outcome.postmortem ?? (trade.realized_pnl != null ? "The outcome is recorded; a human-readable postmortem has not been attached yet." : "Close the position and resolve its outcome before learning can be attributed."))}</p><p className="mt-3 text-sm text-muted-foreground">Research outcome records provide context. The paper ledger remains the source for paper P&amp;L.</p></HumanPanel></div>
      <TechnicalDetails><StoredEvidence title="Decision evidence" value={{ decision, evidence }} /><StoredEvidence title="Paper ticket, fills and policy" value={{ ticket: execution.ticket_snapshot, fills: execution.fills, legs: execution.legs, policy: execution.policy_result }} /><StoredEvidence title="Outcome and related research" value={{ outcome, related_research: trade.related_research }} /><Card><CardHeader><CardTitle>Stored identifiers and lineage</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><p>Decision: <code>{String(trade.decision_id ?? "unavailable")}</code></p><p>Paper order: <code>{String(trade.paper_order_id ?? "unavailable")}</code></p><p className="text-xs text-muted-foreground">Stored source references: {evidence.length ? evidence.map((item: any) => String(item.reference_key ?? item.evidence_kind ?? "record")).join(", ") : "none"}.</p></CardContent></Card></TechnicalDetails>
    </> : null}
  </div>;
}

function buildTimeline(trade: PaperTrade): TimelineEvent[] {
  const execution = safeRecord(trade.execution);
  const fills = Array.isArray(execution.fills) ? execution.fills : [];
  const events: TimelineEvent[] = [];
  if (trade.decision_at) events.push({ label: "Decision", at: trade.decision_at, kind: "decision" });
  if (trade.staged_at) events.push({ label: "Paper order staged", at: trade.staged_at, kind: "staged" });
  fills.forEach((fill: any) => events.push({ label: eventLabel(fill.action), at: typeof fill.created_at === "string" ? fill.created_at : null, kind: String(fill.action ?? "fill"), quantity: typeof fill.quantity === "number" ? fill.quantity : Number(fill.quantity) || null, price: typeof fill.price === "number" ? fill.price : Number(fill.price) || null }));
  if (trade.mark_observed_at && trade.remaining_quantity) events.push({ label: "Latest verified mark", at: trade.mark_observed_at, kind: "mark", price: trade.mark_price });
  const outcome = safeRecord(trade.outcome);
  if (outcome.observed_through) events.push({ label: "Outcome observed", at: String(outcome.observed_through), kind: "outcome" });
  return events.sort((left, right) => new Date(left.at ?? 0).getTime() - new Date(right.at ?? 0).getTime());
}

function TradeTimeline({ events }: { events: TimelineEvent[] }) {
  return <Card><CardHeader><CardTitle>Decision → entry → holding → exit</CardTitle><p className="text-sm text-muted-foreground">The position’s story in the order it happened.</p></CardHeader><CardContent>{events.length ? <ol className="relative grid gap-4 sm:grid-cols-4">{events.map((event, index) => <li key={`${event.kind}-${event.at}-${index}`} className="relative flex gap-3 sm:block sm:text-center"><span className="relative z-10 mt-1 block size-4 shrink-0 rounded-full border-4 border-background bg-primary ring-1 ring-primary/30 sm:mx-auto" />{index < events.length - 1 ? <span className="absolute left-2 top-5 h-[calc(100%+1rem)] w-px bg-border sm:left-1/2 sm:top-2 sm:h-px sm:w-full" /> : null}<div className="min-w-0"><p className="font-medium">{event.label}</p><p className="mt-1 text-xs text-muted-foreground">{dateTime(event.at)}</p>{event.quantity != null || event.price != null ? <p className="mt-1 text-xs text-muted-foreground">{event.quantity != null ? `${event.quantity} units` : ""}{event.quantity != null && event.price != null ? " · " : ""}{event.price != null ? money(event.price) : ""}</p> : null}</div></li>)}</ol> : <p className="text-sm text-muted-foreground">No timeline events are recorded.</p>}</CardContent></Card>;
}

function MiniTradeCharts({ trade, timeline }: { trade: PaperTrade; timeline: TimelineEvent[] }) {
  const priceEvents = timeline.filter((event) => event.price != null);
  const values = priceEvents.map((event) => Number(event.price));
  if (!values.length) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const points = values.map((value, index) => `${20 + index * (560 / Math.max(1, values.length - 1))},${190 - ((value - min) / Math.max(0.01, max - min)) * 150}`).join(" ");
  return <Card><CardHeader><CardTitle>Holding path</CardTitle><p className="text-sm text-muted-foreground">Observed fill and mark prices, with no synthetic path between observations.</p></CardHeader><CardContent><div className="grid gap-4 md:grid-cols-2"><div><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Observed price</p><svg viewBox="0 0 600 220" role="img" aria-label="Observed trade price path" className="h-48 w-full rounded-lg bg-muted/30"><polyline points={points} fill="none" stroke="#2563eb" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />{values.map((value, index) => <circle key={`${value}-${index}`} cx={20 + index * (560 / Math.max(1, values.length - 1))} cy={190 - ((value - min) / Math.max(0.01, max - min)) * 150} r="6" fill="#2563eb"><title>{money(value)}</title></circle>)}</svg></div><div className="flex items-center justify-center rounded-lg border border-dashed border-border p-5 text-center"><div><p className="font-medium">P&L attribution</p><p className="mt-1 text-sm text-muted-foreground">{trade.realized_pnl == null ? "Unavailable until a verified exit is recorded." : `Verified realized result: ${money(trade.realized_pnl, true)}`}</p></div></div></div></CardContent></Card>;
}

function HumanPanel({ title, children }: { title: string; children: ReactNode }) {
  return <Card><CardHeader><CardTitle>{title}</CardTitle></CardHeader><CardContent className="text-sm leading-6">{children}</CardContent></Card>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p><p className="mt-1 font-semibold">{value}</p></div>;
}

function holdingPeriod(events: TimelineEvent[]): string {
  const entry = events.find((event) => event.kind === "paper_entry");
  const exits = events.filter((event) => event.kind.startsWith("paper_exit"));
  const exit = exits[exits.length - 1];
  if (!entry?.at || !exit?.at) return "Open / unavailable";
  const hours = (new Date(exit.at).getTime() - new Date(entry.at).getTime()) / 3_600_000;
  if (!Number.isFinite(hours) || hours < 0) return "Unavailable";
  return hours < 24 ? `${Math.round(hours)}h` : `${(hours / 24).toFixed(1)}d`;
}
