import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { loadLearningOverview, type LearningOverviewPayload } from "@/api/paper";
import { PageHeader, MetricTile, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function ResearchWorkbenchRoute() {
  const [overview, setOverview] = useState<LearningOverviewPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void loadLearningOverview(controller.signal).then(setOverview).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Research workspace unavailable."); });
    return () => controller.abort();
  }, []);
  const strategy = overview?.strategy_lane ?? {};
  const prediction = overview?.prediction_lane ?? {};
  const paper = overview?.paper;
  return <div className="space-y-5">
    <PageHeader eyebrow="Research" title="Trading & learning workbench" subtitle="Inspect prediction quality, strategy evidence, and paper results without confusing research observations with accounting authority." actions={<div className="flex gap-2"><Link className="rounded-md border border-border px-3 py-2 text-sm underline-offset-4 hover:underline" to="/sources">Evidence library</Link><Link className="rounded-md border border-border px-3 py-2 text-sm underline-offset-4 hover:underline" to="/agent">Advisor controls</Link></div>} />
    {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
    {overview ? <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><MetricTile label="Paper orders" value={String(paper?.counts.total_orders ?? 0)} caption="Canonical order scope." /><MetricTile label="Filled orders" value={String(paper?.counts.filled_orders ?? 0)} caption="Fill journal evidence." /><MetricTile label="Strategy lane" value={String(strategy.status ?? "unknown")} caption={strategy.permitted_automatic_action as string} tone={strategy.status === "collecting_outcomes" ? "info" : "warn"} /><MetricTile label="Prediction lane" value={String(prediction.status ?? "unknown")} caption="Advisory only; no execution authority." tone="info" /></div>
      <div className="grid gap-5 lg:grid-cols-2"><Card><CardHeader className="flex flex-row items-center justify-between"><CardTitle>Strategy learning</CardTitle><StatusBadge tone="warn">{String(strategy.status ?? "unknown")}</StatusBadge></CardHeader><CardContent className="space-y-3 text-sm"><p>Strategy evidence is distinct from paper-book P&L. Filled orders: {String(strategy.evidence_counts?.filled_orders ?? 0)}. Reconciled orders: {String(strategy.evidence_counts?.reconciled_orders ?? 0)}.</p><p className="text-muted-foreground">{strategy.blockers?.length ? `Blockers: ${strategy.blockers.join(", ")}` : "No additional strategy blocker recorded."}</p><Link className="font-medium underline-offset-4 hover:underline" to="/portfolio/paper">Open canonical paper evidence →</Link></CardContent></Card><Card><CardHeader className="flex flex-row items-center justify-between"><CardTitle>Prediction / prompt learning</CardTitle><StatusBadge tone={prediction.status === "disabled" ? "muted" : "info"}>{String(prediction.status ?? "unknown")}</StatusBadge></CardHeader><CardContent className="space-y-3 text-sm"><p>Active prompt: <code>{String(prediction.deployed_version ?? "unavailable")}</code></p><p>Challenger: <code>{String(prediction.challenger ?? "none")}</code></p><p className="text-muted-foreground">Automatic action is limited to advisory evaluation. A successful run is not a profitable-strategy claim.</p><Link className="font-medium underline-offset-4 hover:underline" to="/agent">Review advisor operations →</Link></CardContent></Card></div>
      <Card><CardHeader><CardTitle>What changed?</CardTitle></CardHeader><CardContent><p className="text-sm text-muted-foreground">No linked promotion, rollback, repaired-mark, or postmortem events are available in this first read-only slice.</p></CardContent></Card>
    </> : <p className="text-sm text-muted-foreground">Loading research state…</p>}
  </div>;
}
