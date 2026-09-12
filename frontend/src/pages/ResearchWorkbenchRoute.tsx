import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { loadLearningOverview, type LearningOverviewPayload } from "@/api/paper";
import {
  loadResearchEvents,
  loadResearchExperiments,
  loadResearchPredictions,
  loadResearchPrompts,
  loadResearchStrategies,
  type ResearchClaimPage,
  type ResearchExperimentPage,
  type ResearchPromptPage,
  type ResearchStrategyPage,
} from "@/api/research";
import { PageHeader, MetricTile, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const date = (value: unknown) => value ? new Date(String(value)).toLocaleString() : "—";
const text = (value: unknown, fallback = "—") => value == null || value === "" ? fallback : String(value);

export function ResearchWorkbenchRoute() {
  const [searchParams] = useSearchParams();
  const section = searchParams.get("section") || "overview";
  const [overview, setOverview] = useState<LearningOverviewPayload | null>(null);
  const [strategies, setStrategies] = useState<ResearchStrategyPage | null>(null);
  const [predictions, setPredictions] = useState<ResearchClaimPage | null>(null);
  const [prompts, setPrompts] = useState<ResearchPromptPage | null>(null);
  const [experiments, setExperiments] = useState<ResearchExperimentPage | null>(null);
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [loadingMorePredictions, setLoadingMorePredictions] = useState(false);
  const [loadingMoreExperiments, setLoadingMoreExperiments] = useState(false);
  const [loadingMoreStrategies, setLoadingMoreStrategies] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    const request = section === "strategies"
      ? loadResearchStrategies(controller.signal).then(setStrategies)
      : section === "predictions"
        ? Promise.all([loadResearchPredictions(controller.signal), loadResearchPrompts(controller.signal)]).then(([claims, versions]) => { setPredictions(claims); setPrompts(versions); })
        : section === "experiments"
          ? loadResearchExperiments(controller.signal).then(setExperiments)
      : Promise.all([loadLearningOverview(controller.signal), loadResearchEvents(controller.signal)]).then(([state, changes]) => { setOverview(state); setEvents(changes.rows); });
    void request.catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Research workspace unavailable."); });
    return () => controller.abort();
  }, [section]);

  const strategy = overview?.strategy_lane ?? {};
  const prediction = overview?.prediction_lane ?? {};
  const paper = overview?.paper;

  return <div className="space-y-5">
    <PageHeader
      eyebrow="Research"
      title="Trading & learning workbench"
      subtitle="Inspect what was predicted, what was evaluated, and what remains eligible without confusing research observations with paper accounting."
      actions={<div className="flex gap-2"><Link className="rounded-md border border-border px-3 py-2 text-sm underline-offset-4 hover:underline" to="/sources">Evidence library</Link><Link className="rounded-md border border-border px-3 py-2 text-sm underline-offset-4 hover:underline" to="/agent">Advisor controls</Link></div>}
    />
    <nav aria-label="Research sections" className="flex flex-wrap gap-2 border-b border-border pb-3 text-sm">
      {[["overview", "Overview"], ["strategies", "Strategies"], ["predictions", "Predictions"], ["experiments", "Experiments"]].map(([key, label]) => <Link key={key} className={`rounded-md px-3 py-2 ${section === key ? "bg-primary text-primary-foreground" : "border border-border hover:bg-accent"}`} to={`/research?section=${key}`}>{label}</Link>)}
    </nav>
    {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
    {section === "strategies" ? <StrategiesSection page={strategies} loadingMore={loadingMoreStrategies} onLoadMore={(cursor) => {
      const controller = new AbortController();
      setLoadingMoreStrategies(true);
      void loadResearchStrategies(controller.signal, cursor).then((next) => setStrategies((current) => current ? { ...current, rows: [...current.rows, ...next.rows], next_cursor: next.next_cursor, count: next.count } : next)).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Older strategy revisions unavailable."); }).finally(() => setLoadingMoreStrategies(false));
    }} /> : null}
    {section === "predictions" ? <PredictionsSection page={predictions} prompts={prompts} loadingMore={loadingMorePredictions} onLoadMore={(cursor) => {
      const controller = new AbortController();
      setLoadingMorePredictions(true);
      void loadResearchPredictions(controller.signal, cursor).then((next) => setPredictions((current) => current ? { ...current, rows: [...current.rows, ...next.rows], next_cursor: next.next_cursor, count: next.count } : next)).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Older predictions unavailable."); }).finally(() => setLoadingMorePredictions(false));
    }} /> : null}
    {section === "experiments" ? <ExperimentsSection page={experiments} loadingMore={loadingMoreExperiments} onLoadMore={(cursor) => {
      const controller = new AbortController();
      setLoadingMoreExperiments(true);
      void loadResearchExperiments(controller.signal, cursor).then((next) => setExperiments((current) => current ? { ...current, rows: [...current.rows, ...next.rows], next_cursor: next.next_cursor, count: next.count } : next)).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Older experiments unavailable."); }).finally(() => setLoadingMoreExperiments(false));
    }} /> : null}
    {section === "overview" ? <>
      {!overview ? <p className="text-sm text-muted-foreground">Loading research state…</p> : <>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><MetricTile label="Paper orders" value={String(paper?.counts.total_orders ?? 0)} caption="Canonical order scope." /><MetricTile label="Filled orders" value={String(paper?.counts.filled_orders ?? 0)} caption="Fill journal evidence." /><MetricTile label="Strategy lane" value={text(strategy.status, "unknown")} caption={text(strategy.permitted_automatic_action)} tone="warn" /><MetricTile label="Prediction lane" value={text(prediction.status, "unknown")} caption="Advisory only; no execution authority." tone="info" /></div>
        <div className="grid gap-5 lg:grid-cols-2"><LaneCard title="Strategy learning" lane={strategy} link="/research?section=strategies" /><LaneCard title="Prediction / prompt learning" lane={prediction} link="/research?section=predictions" /></div>
        <Card><CardHeader><CardTitle>What changed?</CardTitle></CardHeader><CardContent>{events.length ? <div className="space-y-2">{events.map((event, index) => <div key={`${event.event_type}-${event.new_version}-${index}`} className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2 text-sm last:border-0"><span><strong>{text(event.event_type)}</strong> · {text(event.reason)}</span><span className="text-muted-foreground">{date(event.event_at)}</span></div>)}</div> : <p className="text-sm text-muted-foreground">No proposals, evaluations, activations, rollbacks, or repaired evidence are available in this scope.</p>}</CardContent></Card>
      </>}
    </> : null}
  </div>;
}

function LaneCard({ title, lane, link }: { title: string; lane: Record<string, any>; link: string }) {
  const blockers = Array.isArray(lane.blockers) ? lane.blockers : [];
  return <Card><CardHeader className="flex flex-row items-center justify-between"><CardTitle>{title}</CardTitle><StatusBadge tone={lane.status === "disabled" ? "muted" : "info"}>{text(lane.status, "unknown")}</StatusBadge></CardHeader><CardContent className="space-y-3 text-sm"><p>Deployed: <code>{text(lane.deployed_version, "none")}</code> · Challenger: <code>{text(lane.challenger, "none")}</code></p><p className="text-muted-foreground">{blockers.length ? `Blockers: ${blockers.join(", ")}` : "No current blocker recorded."}</p><Link className="font-medium underline-offset-4 hover:underline" to={link}>Open canonical detail →</Link></CardContent></Card>;
}

function StrategiesSection({ page, loadingMore, onLoadMore }: { page: ResearchStrategyPage | null; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  if (!page) return <p className="text-sm text-muted-foreground">Loading strategy revisions…</p>;
  return <Card><CardHeader><CardTitle>Strategy revisions</CardTitle></CardHeader><CardContent className="overflow-x-auto p-0"><table className="w-full text-left text-sm"><thead><tr className="border-b border-border text-xs uppercase text-muted-foreground"><th className="p-3">Revision</th><th className="p-3">Status</th><th className="p-3">Hypothesis</th><th className="p-3">Evidence</th><th className="p-3">Last evaluation</th></tr></thead><tbody>{page.rows.length ? page.rows.map((row) => <tr key={String(row.strategy_revision_id)} className="border-b border-border last:border-0"><td className="p-3"><Link className="font-medium underline-offset-4 hover:underline" to={`/research/strategies/${row.strategy_revision_id}`}>{text(row.name, row.strategy_key as string)} · r{text(row.revision)}</Link><div className="text-xs text-muted-foreground">{text(row.strategy_family)}</div></td><td className="p-3"><StatusBadge tone={row.status === "active" ? "good" : row.status === "candidate" ? "info" : "warn"}>{text(row.status)}</StatusBadge></td><td className="max-w-sm p-3 text-muted-foreground">{text(row.hypothesis, "No registered hypothesis")}</td><td className="p-3">{text(row.evaluation_count, "0")} evals · {text(row.paper_order_count, "0")} paper orders</td><td className="p-3 text-muted-foreground">{date(row.last_evaluation_at)}</td></tr>) : <tr><td className="p-4 text-muted-foreground" colSpan={5}>No strategy revisions are available. This is an empty research registry, not zero investment quality.</td></tr>}</tbody></table>{page.next_cursor ? <div className="border-t border-border p-3"><button type="button" className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50" disabled={loadingMore} onClick={() => onLoadMore(page.next_cursor!)}>{loadingMore ? "Loading older revisions…" : "Load older revisions"}</button></div> : null}</CardContent></Card>;
}

function PredictionsSection({ page, prompts, loadingMore, onLoadMore }: { page: ResearchClaimPage | null; prompts: ResearchPromptPage | null; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  if (!page) return <p className="text-sm text-muted-foreground">Loading prediction ledger…</p>;
  return <div className="space-y-5"><Card><CardHeader><CardTitle>Claim-level prediction ledger</CardTitle></CardHeader><CardContent className="overflow-x-auto p-0"><table className="w-full text-left text-sm"><thead><tr className="border-b border-border text-xs uppercase text-muted-foreground"><th className="p-3">Issued</th><th className="p-3">Symbol / target</th><th className="p-3">Probability</th><th className="p-3">Horizon</th><th className="p-3">State</th><th className="p-3">Score</th></tr></thead><tbody>{page.rows.length ? page.rows.map((row) => <tr key={String(row.claim_id)} className="border-b border-border last:border-0"><td className="p-3"><Link className="underline-offset-4 hover:underline" to={`/research/predictions/${row.claim_id}`}>{date(row.issued_at)}</Link><div className="text-xs text-muted-foreground">cutoff {date(row.information_cutoff)}</div></td><td className="max-w-md p-3"><strong>{text(row.symbol)}</strong><div className="text-xs text-muted-foreground">{text(row.claim_kind)} · {text(row.statement)}</div></td><td className="p-3">{row.probability == null ? "—" : `${(Number(row.probability) * 100).toFixed(0)}%`}</td><td className="p-3">{text(row.horizon)}</td><td className="p-3"><StatusBadge tone={row.maturity_state === "resolved" ? "good" : row.maturity_state === "pending" ? "info" : "warn"}>{text(row.maturity_state, "pending")}</StatusBadge></td><td className="p-3">{row.calibration_error == null ? "—" : Number(row.calibration_error).toFixed(3)}</td></tr>) : <tr><td className="p-4 text-muted-foreground" colSpan={6}>No claims have been issued. Pending or unavailable data is not treated as prediction failure.</td></tr>}</tbody></table>{page.next_cursor ? <div className="border-t border-border p-3"><button type="button" className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50" disabled={loadingMore} onClick={() => onLoadMore(page.next_cursor!)}>{loadingMore ? "Loading older claims…" : "Load older claims"}</button></div> : null}</CardContent></Card><Card><CardHeader><CardTitle>Prompt versions</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">{prompts?.rows.length ? prompts.rows.map((row) => <div key={String(row.version)} className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2 last:border-0"><Link className="font-medium underline-offset-4 hover:underline" to={`/research/prompts/${encodeURIComponent(String(row.version))}`}>{String(row.version)}</Link><span className="text-muted-foreground">{text(row.semantic_effective_hash, "no hash").slice(0, 12)} · {text(row.claim_count, "0")} claims</span></div>) : <p className="text-muted-foreground">No prompt versions are persisted yet.</p>}</CardContent></Card></div>;
}

function ExperimentsSection({ page, loadingMore, onLoadMore }: { page: ResearchExperimentPage | null; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  if (!page) return <p className="text-sm text-muted-foreground">Loading experiment history…</p>;
  return <Card><CardHeader><CardTitle>Experiments and comparisons</CardTitle></CardHeader><CardContent className="overflow-x-auto p-0"><table className="w-full text-left text-sm"><thead><tr className="border-b border-border text-xs uppercase text-muted-foreground"><th className="p-3">Experiment</th><th className="p-3">Baseline</th><th className="p-3">Challenger</th><th className="p-3">Evidence</th><th className="p-3">Status</th></tr></thead><tbody>{page.rows.length ? page.rows.map((row) => <tr key={String(row.experiment_id)} className="border-b border-border last:border-0"><td className="p-3"><Link className="font-medium underline-offset-4 hover:underline" to={`/research/experiments/${row.experiment_id}`}>{text(row.experiment_kind)}</Link><div className="text-xs text-muted-foreground">{date(row.input_cutoff)}</div></td><td className="p-3">{text(row.champion_strategy_key, row.active_prompt_version as string || "—")}</td><td className="p-3">{text(row.challenger_strategy_key, row.candidate_prompt_version as string || "—")}</td><td className="p-3">{text(row.matched_outcomes, "—")} matched units</td><td className="p-3"><StatusBadge tone={row.distinctness === "matched" || row.distinctness === "distinct" ? "good" : "warn"}>{text(row.distinctness)}</StatusBadge></td></tr>) : <tr><td className="p-4 text-muted-foreground" colSpan={5}>No experiments have been recorded. No winner-only catalog is being fabricated.</td></tr>}</tbody></table>{page.next_cursor ? <div className="border-t border-border p-3"><button type="button" className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50" disabled={loadingMore} onClick={() => onLoadMore(page.next_cursor!)}>{loadingMore ? "Loading older experiments…" : "Load older experiments"}</button></div> : null}</CardContent></Card>;
}
