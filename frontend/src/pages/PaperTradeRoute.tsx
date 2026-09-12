import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { loadPaperTrade, type PaperTrade } from "@/api/paper";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

import { StoredEvidence } from "@/components/market/StoredEvidence";

const money = (value: unknown) => typeof value === "number" ? `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—";
const date = (value: unknown) => typeof value === "string" ? new Date(value).toLocaleString() : "—";

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
  const decision = (trade?.decision ?? {}) as Record<string, any>;
  const thesis = (decision.thesis ?? {}) as Record<string, any>;
  const execution = (trade?.execution ?? {}) as Record<string, any>;
  const outcome = (trade?.outcome ?? {}) as Record<string, any>;
  const fills = Array.isArray(execution.fills) ? execution.fills as Array<Record<string, any>> : [];
  const evidence = Array.isArray(decision.evidence) ? decision.evidence as Array<Record<string, any>> : [];

  return (
    <div className="space-y-5">
      <Link className="text-sm text-muted-foreground underline-offset-4 hover:underline" to={{ pathname: "/portfolio/paper", search: location.search }}>← Back to paper book</Link>
      {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
      {!trade && !error ? <p className="text-sm text-muted-foreground">Loading stored trade evidence…</p> : null}
      {trade ? <>
        <PageHeader eyebrow={`Paper trade · ${trade.symbol}`} title={`${trade.symbol} investigation`} subtitle="Stored decision and execution evidence. Research observations are related context, not paper fills." actions={<StatusBadge tone={trade.reconciliation_status === "verified" ? "good" : "warn"}>{trade.reconciliation_status}</StatusBadge>} />
        <Card><CardHeader><CardTitle>Contemporaneous decision</CardTitle></CardHeader><CardContent className="grid gap-4 md:grid-cols-2"><div><p className="text-xs font-semibold uppercase text-muted-foreground">Thesis</p><p className="mt-1 text-sm">{thesis.core_thesis ?? thesis.thesis ?? "Stored thesis unavailable."}</p></div><div><p className="text-xs font-semibold uppercase text-muted-foreground">Countercase / invalidation</p><p className="mt-1 text-sm">{thesis.invalidation ?? decision.blockers?.join(", ") ?? "No stored invalidation."}</p></div><div><p className="text-xs font-semibold uppercase text-muted-foreground">Why this instrument</p><p className="mt-1 text-sm">{decision.reasons?.join(" · ") || "No stored selection rationale."}</p></div><div><p className="text-xs font-semibold uppercase text-muted-foreground">Risk allocation</p><p className="mt-1 text-sm">{money(trade.initial_risk)}</p></div></CardContent></Card>
        <StoredEvidence title="Decision and fill timeline" value={[
          { event: "Decision", at: trade.decision_at }, { event: "Staged paper order", at: trade.staged_at },
          ...fills.map((fill) => ({ event: fill.action, at: fill.created_at, quantity: fill.quantity, price: fill.price })),
          { event: "Latest mark", at: trade.mark_observed_at, available_at: trade.mark_available_at, basis: trade.mark_basis },
          { event: "Outcome observation", at: outcome.observed_through },
        ]} />
        <StoredEvidence title="Original forecasts and deterministic policy" value={{ forecast: decision.forecast, policy: execution.policy_result }} />
        <div className="grid gap-5 lg:grid-cols-2">
          <Card><CardHeader><CardTitle>Execution</CardTitle></CardHeader><CardContent className="space-y-3 text-sm"><div className="grid grid-cols-2 gap-3"><div><span className="text-muted-foreground">Lifecycle</span><p className="font-medium">{trade.lifecycle}</p></div><div><span className="text-muted-foreground">Paper only</span><p className="font-medium">{execution.paper_only ? "Yes" : "Blocked"}</p></div><div><span className="text-muted-foreground">Filled quantity</span><p className="font-medium">{String(trade.filled_quantity ?? 0)}</p></div><div><span className="text-muted-foreground">Remaining</span><p className="font-medium">{String(trade.remaining_quantity ?? 0)}</p></div><div><span className="text-muted-foreground">Entry</span><p className="font-medium">{money(trade.entry_price)}</p></div><div><span className="text-muted-foreground">Current mark</span><p className="font-medium">{money(trade.mark_price)}</p><p className="text-xs text-muted-foreground">{trade.mark_status ?? "unavailable"} · {trade.mark_basis ?? "no basis"}</p></div><div><span className="text-muted-foreground">Staged limit</span><p className="font-medium">{money(trade.staged_limit_price)}</p></div></div><p className="text-xs text-muted-foreground">A staged limit is not a fill. Multiplier: {execution.multiplier_evidence ?? "unavailable"}; mark source: {trade.mark_source ?? "unavailable"}.</p>{fills.length ? <Table><TableHeader><TableRow><TableHead>At</TableHead><TableHead>Action</TableHead><TableHead>Quantity</TableHead><TableHead>Price</TableHead></TableRow></TableHeader><TableBody>{fills.map((fill) => <TableRow key={String(fill.id)}><TableCell>{date(fill.created_at)}</TableCell><TableCell>{String(fill.action ?? "")}</TableCell><TableCell>{String(fill.quantity ?? "")}</TableCell><TableCell>{money(typeof fill.price === "number" ? fill.price : null)}</TableCell></TableRow>)}</TableBody></Table> : <p className="rounded-md bg-muted p-3 text-xs">No deterministic fill journal. No paper position or P&L is claimed.</p>}</CardContent></Card>
          <Card><CardHeader><CardTitle>Outcome</CardTitle></CardHeader><CardContent className="space-y-3 text-sm"><div className="grid grid-cols-2 gap-3"><div><span className="text-muted-foreground">Realized P&L</span><p className="font-medium">{money(trade.realized_pnl)}</p></div><div><span className="text-muted-foreground">Net P&L</span><p className="font-medium">{money(trade.net_pnl)}</p></div><div><span className="text-muted-foreground">Research outcome</span><p className="font-medium">{String(outcome.state ?? "unavailable")}</p></div><div><span className="text-muted-foreground">Observed through</span><p className="font-medium">{date(outcome.observed_through)}</p></div></div><p className="text-xs text-muted-foreground">{String(outcome.accounting_note ?? "Outcome evidence does not replace the paper ledger.")}</p></CardContent></Card>
        </div>
        <StoredEvidence title="Immutable paper ticket and legs" value={{ ticket: execution.ticket_snapshot, legs: execution.legs, fees: execution.fees, multiplier: execution.multiplier }} />
        <StoredEvidence title="Research outcome and related observation" value={{ outcome, relationship: trade.related_research }} />
        <Card><CardHeader><CardTitle>Artifacts and lineage</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><p>Decision <code>{String(trade.decision_id ?? "unavailable")}</code> · Order <code>{trade.paper_order_id}</code></p>{evidence.length ? <ul className="space-y-1">{evidence.map((item) => <li key={`${item.evidence_kind}-${item.reference_key}`} className="rounded-md border border-border p-2"><span className="font-medium">{String(item.evidence_kind)}</span> · {String(item.reference_key)}</li>)}</ul> : <p className="text-muted-foreground">No linked decision evidence was stored.</p>}<p className="text-xs text-muted-foreground">The view shows stored rationale and output. It does not reconstruct hidden model reasoning.</p></CardContent></Card>
      </> : null}
    </div>
  );
}
