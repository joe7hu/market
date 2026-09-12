import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { loadLearningOverview, type LearningOverviewPayload } from "@/api/paper";
import { loadResearchEvents, loadResearchExperiments, loadResearchPredictions, loadResearchPrompts, loadResearchStrategies, type ResearchClaimPage, type ResearchExperimentPage, type ResearchPromptPage, type ResearchStrategyPage } from "@/api/research";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { eventLabel } from "@/presentation/actions";
import { calibrationPoints, predictionProgress, predictionState, promptMetric } from "@/presentation/prediction";
import { humanize, money, numberValue, percent, shortDate, statusLabel, statusTone } from "@/presentation/labels";
import { lifecycleHeadline, laneBlockers } from "@/presentation/lifecycle";
import { strategyName, strategyStatus, strategySteps, strategyTone } from "@/presentation/strategy";

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
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setOverview(null);
    setStrategies(null);
    setPredictions(null);
    setPrompts(null);
    setExperiments(null);
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

  const loadMore = (kind: "strategies" | "predictions" | "experiments", cursor: string) => {
    const controller = new AbortController();
    setLoadingMore(true);
    const request = kind === "strategies" ? loadResearchStrategies(controller.signal, cursor) : kind === "predictions" ? loadResearchPredictions(controller.signal, cursor) : loadResearchExperiments(controller.signal, cursor);
    void request.then((next) => {
      if (kind === "strategies") setStrategies((current) => current ? { ...current, rows: [...current.rows, ...next.rows], next_cursor: next.next_cursor, count: next.count } : next as ResearchStrategyPage);
      if (kind === "predictions") setPredictions((current) => current ? { ...current, rows: [...current.rows, ...next.rows], next_cursor: next.next_cursor, count: next.count } : next as ResearchClaimPage);
      if (kind === "experiments") setExperiments((current) => current ? { ...current, rows: [...current.rows, ...next.rows], next_cursor: next.next_cursor, count: next.count } : next as ResearchExperimentPage);
    }).catch((reason) => setError(reason instanceof Error ? reason.message : "Older research records unavailable.")).finally(() => setLoadingMore(false));
  };

  const strategy = overview?.strategy_lane ?? {};
  const prediction = overview?.prediction_lane ?? {};
  return <div className="space-y-5">
    <PageHeader eyebrow="Research" title="Learning workbench" subtitle="See what is active, what is being tested, and exactly what evidence remains before a change can be trusted." actions={<div className="flex gap-2"><Link className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent" to="/portfolio/paper">Portfolio</Link><Link className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent" to="/agent">Advisor controls</Link></div>} />
    <nav aria-label="Research sections" className="flex flex-wrap gap-2 border-b border-border pb-3 text-sm">{[["overview", "Overview"], ["strategies", "Strategies"], ["predictions", "Forecast quality"], ["experiments", "Comparisons"]].map(([key, label]) => <Link key={key} className={`rounded-md px-3 py-2 ${section === key ? "bg-primary text-primary-foreground" : "border border-border hover:bg-accent"}`} to={`/research?section=${key}`}>{label}</Link>)}</nav>
    {error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
    {section === "overview" ? <OverviewSection overview={overview} strategy={strategy} prediction={prediction} events={events} /> : null}
    {section === "strategies" ? <StrategiesSection page={strategies} loadingMore={loadingMore} onLoadMore={(cursor) => loadMore("strategies", cursor)} /> : null}
    {section === "predictions" ? <PredictionsSection page={predictions} prompts={prompts} loadingMore={loadingMore} onLoadMore={(cursor) => loadMore("predictions", cursor)} /> : null}
    {section === "experiments" ? <ExperimentsSection page={experiments} loadingMore={loadingMore} onLoadMore={(cursor) => loadMore("experiments", cursor)} /> : null}
  </div>;
}

function OverviewSection({ overview, strategy, prediction, events }: { overview: LearningOverviewPayload | null; strategy: Record<string, any>; prediction: Record<string, any>; events: Array<Record<string, any>> }) {
  if (!overview) return <ResearchSkeleton />;
  const paper = overview.paper;
  const predictionQuality = (prediction.quality ?? prediction.forecast_quality ?? prediction.overview?.quality ?? {}) as Record<string, any>;
  return <div className="space-y-5"><section className="grid gap-4 xl:grid-cols-[1.2fr_1fr]"><StrategyPipeline lane={strategy} /><PromptPipeline lane={prediction} quality={predictionQuality} /></section><section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"><InsightMetric label="Paper fills" value={String(paper?.counts.filled_orders ?? 0)} note={paper?.counts.filled_orders ? "Available for performance learning" : "Waiting for the first paper-ready fill"} /><InsightMetric label="Comparable cases" value={String((overview.diagnostics as any)?.decision_quality?.counts?.comparable_counterfactual_count ?? 0)} note="Matched outcome evidence" /><InsightMetric label="Active strategy" value={humanize(strategy.deployed_version, "Not selected")} note="Deterministic policy remains in control" /><InsightMetric label="Forecast learning" value={predictionQuality.valid_resolved_claims ? String(predictionQuality.valid_resolved_claims) : "Not started"} note="Independent resolved forecasts" /></section><NextStep strategy={strategy} prediction={prediction} /><DecisionDiagnostics diagnostics={(overview.diagnostics ?? {}) as Record<string, any>} /><Card><CardHeader><CardTitle>What changed</CardTitle><p className="text-sm text-muted-foreground">A short history of changes that can affect future decisions.</p></CardHeader><CardContent>{events.length ? <div className="space-y-2">{events.slice(0, 12).map((event, index) => <div key={`${event.event_type}-${index}`} className="flex flex-wrap items-center justify-between gap-3 border-b border-border py-3 text-sm last:border-0"><span><strong>{eventLabel(event.event_type)}</strong><span className="ml-2 text-muted-foreground">{humanize(event.reason, "Evidence update")}</span></span><span className="text-muted-foreground">{date(event.event_at)}</span></div>)}</div> : <p className="text-sm text-muted-foreground">No strategy or prompt changes are recorded yet.</p>}</CardContent></Card></div>;
}

function StrategyPipeline({ lane }: { lane: Record<string, any> }) {
  const steps = strategySteps(lane);
  const blockers = laneBlockers(lane);
  return <Card className="overflow-hidden"><CardHeader className="border-b border-border bg-primary/[0.04]"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">Strategy learning</p><CardTitle className="mt-1">{lifecycleHeadline(lane, "Strategy evidence collection")}</CardTitle></div><StatusBadge tone={strategyTone(lane.status)}>{strategyStatus(lane.status)}</StatusBadge></div></CardHeader><CardContent className="space-y-5 p-5"><div className="grid gap-2 sm:grid-cols-4">{steps.map((step, index) => <div key={step.label} className="relative rounded-xl border border-border bg-background p-3">{index < steps.length - 1 ? <span className="absolute -right-2 top-1/2 hidden h-px w-4 bg-border sm:block" /> : null}<div className="flex items-center gap-2"><span className={`size-2.5 rounded-full ${step.state === "complete" ? "bg-emerald-600" : step.state === "current" ? "bg-amber-500" : "bg-muted-foreground/30"}`} /><span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{step.label}</span></div><p className="mt-3 text-sm font-semibold">{humanize(step.value)}</p><p className="mt-1 text-xs text-muted-foreground">{step.detail ?? "Current policy"}</p></div>)}</div><div className="flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-muted-foreground">{blockers.length ? blockers[0] : "No promotion blocker recorded."}</p><Link className="text-sm font-medium text-primary hover:underline" to="/research?section=strategies">Review strategy history →</Link></div></CardContent></Card>;
}

function PromptPipeline({ lane, quality }: { lane: Record<string, any>; quality: Record<string, any> }) {
  const progress = predictionProgress(quality);
  const active = lane.active_prompt_version ?? lane.deployed_version;
  const challenger = lane.challenger ?? lane.candidate_prompt_version;
  return <Card className="overflow-hidden"><CardHeader className="border-b border-border bg-violet-500/[0.04]"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">Prompt learning</p><CardTitle className="mt-1">{lane.status === "disabled" ? "Forecast learning is paused" : challenger ? "Challenger under observation" : "Collecting forecast outcomes"}</CardTitle></div><StatusBadge tone={lane.status === "disabled" ? "muted" : "info"}>{statusLabel(lane.status, "Advisory only")}</StatusBadge></div></CardHeader><CardContent className="space-y-5 p-5"><div className="grid gap-3 sm:grid-cols-2"><div className="rounded-xl border border-border p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">Active prompt</p><p className="mt-2 text-lg font-semibold">{humanize(active, "Not selected")}</p><p className="mt-1 text-xs text-muted-foreground">The current advisory baseline</p></div><div className="rounded-xl border border-dashed border-violet-500/40 p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">Prompt being tested</p><p className="mt-2 text-lg font-semibold">{humanize(challenger, "No challenger yet")}</p><p className="mt-1 text-xs text-muted-foreground">A challenger cannot change deterministic execution</p></div></div><div><div className="flex items-center justify-between gap-3 text-sm"><span className="font-medium">Forecast history</span><span className="text-muted-foreground">{progress.label}</span></div><div className="mt-2 h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-violet-500" style={{ width: `${progress.percent}%` }} /></div><p className="mt-2 text-xs text-muted-foreground">{progress.required ? progress.detail : "Independent outcome target is not available yet."}</p></div><Link className="text-sm font-medium text-primary hover:underline" to="/research?section=predictions">Open calibration and prompt comparison →</Link></CardContent></Card>;
}

function NextStep({ strategy, prediction }: { strategy: Record<string, any>; prediction: Record<string, any> }) {
  const blockers = [...laneBlockers(strategy), ...laneBlockers(prediction)];
  return <section className="rounded-xl border border-amber-500/30 bg-amber-500/[0.07] p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-amber-800 dark:text-amber-200">Next decision</p><h2 className="mt-1 text-base font-semibold">{blockers.length ? blockers[0] : "Continue collecting matched evidence"}</h2><p className="mt-1 text-sm text-muted-foreground">Learning stays advisory until its statistical, safety, and forward checks are complete.</p></div><Link className="rounded-md border border-amber-500/40 px-3 py-2 text-sm font-medium hover:bg-amber-500/10" to={blockers.length && String(strategy.status).includes("paper") ? "/portfolio/paper" : "/research?section=experiments"}>{blockers.length ? "Inspect blocker" : "Review experiments"}</Link></div></section>;
}

function InsightMetric({ label, value, note }: { label: string; value: string; note: string }) {
  return <div className="rounded-xl border border-border bg-card p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p><p className="mt-2 text-2xl font-semibold">{value}</p><p className="mt-1 text-xs text-muted-foreground">{note}</p></div>;
}

function DecisionDiagnostics({ diagnostics }: { diagnostics: Record<string, any> }) {
  const quality = diagnostics.decision_quality ?? {};
  const counts = quality.counts ?? {};
  const rejected = diagnostics.rejected_opportunities ?? {};
  const components = Array.isArray(diagnostics.edge_waterfall?.components) ? diagnostics.edge_waterfall.components : [];
  return <section><div className="mb-3"><h2 className="text-xl font-semibold">Decision quality</h2><p className="text-sm text-muted-foreground">Outcome evidence is separate from paper P&L and never treated as a trading result by itself.</p></div><div className="grid gap-4 lg:grid-cols-2"><Card><CardHeader><CardTitle className="text-base">What the evidence says</CardTitle></CardHeader><CardContent className="grid gap-4 sm:grid-cols-3"><MiniStat label="Comparable cases" value={String(counts.comparable_counterfactual_count ?? 0)} /><MiniStat label="Selection edge" value={percent(quality.mean_selection_delta)} /><MiniStat label="Rejected setups" value={String(rejected.counts?.excluded_observations ?? 0)} /></CardContent></Card><Card><CardHeader><CardTitle className="text-base">Where evidence is missing</CardTitle></CardHeader><CardContent className="space-y-2">{components.length ? components.slice(0, 4).map((component: Record<string, any>) => <div key={String(component.name)} className="flex items-center justify-between gap-3 border-b border-border py-2 text-sm last:border-0"><span>{humanize(component.name)}</span><span className="text-muted-foreground">{component.value == null ? "Not available" : percent(component.value)}</span></div>) : <p className="text-sm text-muted-foreground">No edge-loss diagnosis is available yet.</p>}</CardContent></Card></div></section>;
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return <div><p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p><p className="mt-1 text-lg font-semibold">{value}</p></div>;
}

function StrategiesSection({ page, loadingMore, onLoadMore }: { page: ResearchStrategyPage | null; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  if (!page) return <ResearchSkeleton />;
  const active = page.rows.find((row) => row.status === "active");
  return <div className="space-y-5"><section className="rounded-xl border border-border bg-card p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Strategy lineage</p><h2 className="mt-1 text-xl font-semibold">How the policy has changed</h2></div><StatusBadge tone={active ? "good" : "muted"}>{active ? "Active policy identified" : "No active policy"}</StatusBadge></div><div className="mt-5 space-y-3">{page.rows.length ? page.rows.map((row, index) => <div key={String(row.strategy_revision_id)} className="flex gap-3"><div className="flex w-7 shrink-0 flex-col items-center"><span className={`mt-1 size-3 rounded-full ${row.status === "active" ? "bg-emerald-600" : row.status === "candidate" ? "bg-amber-500" : "bg-muted-foreground/40"}`} />{index < page.rows.length - 1 ? <span className="mt-1 h-full w-px bg-border" /> : null}</div><Link className="mb-1 min-w-0 flex-1 rounded-xl border border-border p-4 hover:bg-accent" to={`/research/strategies/${row.strategy_revision_id}?section=strategies`}><div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-semibold">{strategyName(row, "Unnamed strategy")}</h3><p className="text-xs text-muted-foreground">Revision {text(row.revision)} · {humanize(row.strategy_family, "General strategy")}</p></div><StatusBadge tone={strategyTone(row.status)}>{strategyStatus(row.status)}</StatusBadge></div><p className="mt-3 text-sm text-muted-foreground">{text(row.hypothesis, "No hypothesis recorded yet.")}</p><div className="mt-3 flex flex-wrap gap-4 text-xs text-muted-foreground"><span>{text(row.evaluation_count, "0")} historical evaluations</span><span>{text(row.paper_order_count, "0")} paper orders</span><span>Last checked {shortDate(row.last_evaluation_at)}</span></div></Link></div>) : <p className="text-sm text-muted-foreground">No strategy revisions are recorded. This is an empty registry, not zero strategy quality.</p>}</div></section>{page.next_cursor ? <LoadMore loading={loadingMore} label="Load older strategy revisions" onClick={() => onLoadMore(page.next_cursor!) } /> : null}</div>;
}

function PredictionsSection({ page, prompts, loadingMore, onLoadMore }: { page: ResearchClaimPage | null; prompts: ResearchPromptPage | null; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  if (!page) return <ResearchSkeleton />;
  const quality = (page.quality ?? {}) as Record<string, any>;
  const progress = predictionProgress(quality);
  return <div className="space-y-5"><section className="rounded-xl border border-border bg-card p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Forecast quality</p><h2 className="mt-1 text-2xl font-semibold">Are the probabilities calibrated?</h2><p className="mt-1 text-sm text-muted-foreground">Only independently resolved and evidence-valid claims contribute to the curves below.</p></div><StatusBadge tone={quality.status === "insufficient_evidence" ? "warn" : "info"}>{statusLabel(quality.status, "Collecting evidence")}</StatusBadge></div><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5"><InsightMetric label="Resolved forecasts" value={String(quality.valid_resolved_claims ?? 0)} note={progress.detail} /><InsightMetric label="Brier score" value={promptMetric(quality.brier_score, "brier")} note="Lower is better" /><InsightMetric label="Directional accuracy" value={promptMetric(quality.directional_accuracy, "percent")} note="Resolved directional claims" /><InsightMetric label="Coverage" value={promptMetric(quality.coverage, "percent")} note="Issued claims with outcomes" /><InsightMetric label="Next gate" value={progress.remaining ? `${progress.remaining} more` : "Ready"} note="Independent outcomes" /></div></section><CalibrationVisual quality={quality} /><BrierVisual quality={quality} /><PromptComparison prompts={prompts} /><ClaimLedger page={page} loadingMore={loadingMore} onLoadMore={onLoadMore} /></div>;
}

function CalibrationVisual({ quality }: { quality: Record<string, any> }) {
  const points = calibrationPoints(quality);
  if (!points.length) return <EmptyVisual title="Calibration will appear after resolved forecasts" detail="No valid resolved events are available yet. Pending and unsupported outcomes are not counted as failures." />;
  const width = 640;
  const height = 260;
  const x = (value: number) => 52 + value * 550;
  const y = (value: number) => 220 - value * 180;
  return <Card><CardHeader><CardTitle>Calibration curve</CardTitle><p className="text-sm text-muted-foreground">Predicted probability versus observed event rate. A well-calibrated model follows the diagonal.</p></CardHeader><CardContent><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Forecast calibration curve" className="h-64 w-full"><line x1="52" y1="220" x2="602" y2="40" stroke="currentColor" strokeOpacity=".2" strokeDasharray="5 5" /><line x1="52" y1="220" x2="602" y2="220" stroke="currentColor" strokeOpacity=".3" /><line x1="52" y1="220" x2="52" y2="40" stroke="currentColor" strokeOpacity=".3" /><polyline points={points.map((point) => `${x(point.predicted)},${y(point.observed)}`).join(" ")} fill="none" stroke="#7c3aed" strokeWidth="3" />{points.map((point) => <circle key={point.label} cx={x(point.predicted)} cy={y(point.observed)} r="6" fill="#7c3aed"><title>{`${point.label}: predicted ${percent(point.predicted)}, observed ${percent(point.observed)}, n=${point.count}`}</title></circle>)}<text x="52" y="245" fontSize="12">0%</text><text x="570" y="245" fontSize="12">100%</text><text x="9" y="45" fontSize="12">100%</text><text x="8" y="224" fontSize="12">0%</text><text x="250" y="258" fontSize="12">Predicted probability</text><text x="12" y="150" fontSize="12" transform="rotate(-90 12 150)">Observed rate</text></svg><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">{points.map((point) => <div key={point.label} className="rounded-md border border-border p-2 text-xs"><div className="font-medium">{point.label}</div><div className="mt-1 text-muted-foreground">pred {percent(point.predicted)} · actual {percent(point.observed)}</div><div className="text-muted-foreground">{point.count} cases</div></div>)}</div></CardContent></Card>;
}

function BrierVisual({ quality }: { quality: Record<string, any> }) {
  const series = Array.isArray(quality.brier_time_series) ? quality.brier_time_series.filter((point: Record<string, any>) => point.brier_score != null) : [];
  if (!series.length) return null;
  const max = Math.max(0.01, ...series.map((point: Record<string, any>) => Number(point.brier_score)));
  const line = series.map((point: Record<string, any>, index: number) => `${20 + (index / Math.max(1, series.length - 1)) * 560},${210 - (Number(point.brier_score) / max) * 170}`).join(" ");
  return <Card><CardHeader><CardTitle>Brier score over time</CardTitle><p className="text-sm text-muted-foreground">Lower error is better; this view follows resolved outcome dates.</p></CardHeader><CardContent><svg viewBox="0 0 600 240" role="img" aria-label="Brier score over time" className="h-56 w-full"><line x1="20" y1="210" x2="580" y2="210" stroke="currentColor" strokeOpacity=".3" /><polyline points={line} fill="none" stroke="#2563eb" strokeWidth="3" />{series.map((point: Record<string, any>, index: number) => <circle key={String(point.resolved_at)} cx={20 + (index / Math.max(1, series.length - 1)) * 560} cy={210 - (Number(point.brier_score) / max) * 170} r="5" fill="#2563eb"><title>{`${date(point.resolved_at)}: ${Number(point.brier_score).toFixed(3)}`}</title></circle>)}</svg><div className="mt-2 flex flex-wrap justify-between text-xs text-muted-foreground"><span>{date(series[0].resolved_at)}</span><span>{date(series[series.length - 1].resolved_at)}</span></div></CardContent></Card>;
}

function PromptComparison({ prompts }: { prompts: ResearchPromptPage | null }) {
  const rows = prompts?.rows ?? [];
  if (!rows.length) return <EmptyVisual title="Prompt comparison will appear after prompt history accumulates" detail="No prompt versions are persisted yet." />;
  const active = rows.find((row) => row.active === true) ?? rows[0];
  const challenger = rows.find((row) => row.version !== active.version && row.parent_version === active.version) ?? rows.find((row) => row.version !== active.version);
  return <Card><CardHeader><CardTitle>Prompt experiment</CardTitle><p className="text-sm text-muted-foreground">Compare the current advisory baseline with the latest candidate when both have matched outcomes.</p></CardHeader><CardContent><div className="grid gap-4 md:grid-cols-2"><PromptCard title="Active baseline" row={active} /><PromptCard title="Candidate under test" row={challenger} /></div><Link className="mt-4 inline-block text-sm font-medium text-primary hover:underline" to={active.version ? `/research/prompts/${encodeURIComponent(String(active.version))}?section=predictions` : "/research?section=predictions"}>Inspect prompt lineage →</Link></CardContent></Card>;
}

function PromptCard({ title, row }: { title: string; row?: Record<string, any> }) {
  const claims = Number(row?.claim_count ?? 0);
  const resolved = Number(row?.resolved_claim_count ?? 0);
  const coverage = claims ? resolved / claims : null;
  return <div className="rounded-xl border border-border p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">{title}</p><p className="mt-2 text-lg font-semibold">{row ? humanize(row.version) : "No candidate yet"}</p>{row ? <div className="mt-3 grid grid-cols-2 gap-3 text-sm"><MiniStat label="Brier" value={promptMetric(row.brier_score, "brier")} /><MiniStat label="Accuracy" value={promptMetric(row.directional_accuracy, "percent")} /><MiniStat label="Coverage" value={promptMetric(coverage, "percent")} /><MiniStat label="Cost" value={money(row.cost_usd)} /></div> : <p className="mt-2 text-sm text-muted-foreground">The active prompt needs a comparable challenger before a winner can be declared.</p>}</div>;
}

export function ComparisonVisual({ row }: { row: Record<string, any> }) {
  const metrics = comparisonSides(row);
  if (!metrics) {
    const matched = Number(row.matched_outcomes ?? row.matched_frozen_packets ?? 0);
    const correlation = numberValue(row.metrics?.return_correlation);
    return <div className="mt-4 rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground"><div className="flex items-center justify-between gap-3"><span>Comparison evidence</span><span>{matched ? `${matched} comparable cases` : "Waiting for paired cases"}</span></div>{correlation != null ? <div className="mt-2">Return path agreement: {percent(correlation)}</div> : null}<p className="mt-2">A paired return chart will appear when both sides have the same resolved evidence window.</p></div>;
  }
  const pairs = comparisonMetricNames.flatMap(([key, label, kind]) => {
    const baseline = numberValue(metrics.baseline[key]);
    const challenger = numberValue(metrics.challenger[key]);
    return baseline == null && challenger == null ? [] : [{ key, label, kind, baseline, challenger }];
  });
  if (!pairs.length) return <div className="mt-4 rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground">Paired outcomes are present, but no displayable performance metric is recorded yet.</div>;
  return <div className="mt-4 rounded-lg border border-border p-3" aria-label="Baseline and challenger comparison"><div className="mb-3 flex flex-wrap gap-4 text-xs text-muted-foreground"><span><i className="mr-1 inline-block size-2 rounded-full bg-slate-500" />Baseline</span><span><i className="mr-1 inline-block size-2 rounded-full bg-violet-600" />Challenger</span></div><div className="space-y-3">{pairs.slice(0, 4).map((pair) => { const max = Math.max(0.01, Math.abs(pair.baseline ?? 0), Math.abs(pair.challenger ?? 0)); return <div key={pair.key}><div className="flex items-center justify-between gap-3 text-xs"><span className="font-medium">{pair.label}</span><span className="text-muted-foreground">{formatComparison(pair.baseline, pair.kind)} / {formatComparison(pair.challenger, pair.kind)}</span></div><div className="mt-1 grid gap-1">{[["Baseline", pair.baseline, "bg-slate-500"], ["Challenger", pair.challenger, "bg-violet-600"]].map(([name, value, color]) => <div key={String(name)} className="flex items-center gap-2"><span className="w-16 text-[10px] text-muted-foreground">{name}</span><div className="h-1.5 flex-1 rounded-full bg-muted"><div className={`h-full rounded-full ${color}`} style={{ width: `${value == null ? 0 : Math.min(100, Math.max(4, Math.abs(Number(value)) / max * 100))}%` }} /></div></div>)}</div></div>; })}</div></div>;
}

const comparisonMetricNames: Array<[string, string, "money" | "percent" | "brier" | "number"]> = [
  ["pnl", "P&L", "money"], ["net_pnl", "P&L", "money"], ["return", "Return", "percent"], ["return_pct", "Return", "percent"],
  ["expectancy", "Expectancy", "percent"], ["brier_score", "Brier", "brier"], ["directional_accuracy", "Accuracy", "percent"],
  ["lower_95", "Lower bound", "percent"], ["max_drawdown", "Max drawdown", "percent"], ["matched_outcomes", "Comparable cases", "number"],
];

function comparisonSides(row: Record<string, any>): { baseline: Record<string, any>; challenger: Record<string, any> } | null {
  const source = row.metrics && typeof row.metrics === "object" ? row.metrics : row.scorecard;
  if (!source || typeof source !== "object") return null;
  const candidates = [source, source.matched_cohort, source.comparison].filter((value): value is Record<string, any> => Boolean(value && typeof value === "object"));
  const side = (names: string[]) => {
    for (const candidate of candidates) {
      for (const name of names) {
        const value = candidate[name];
        if (value && typeof value === "object") return value.full && typeof value.full === "object" ? value.full : value;
      }
    }
    return null;
  };
  const baseline = side(["champion", "baseline", "active", "current"]);
  const challenger = side(["challenger", "candidate", "tested"]);
  return baseline && challenger ? { baseline, challenger } : null;
}

function formatComparison(value: number | null, kind: "money" | "percent" | "brier" | "number"): string {
  if (value == null) return "—";
  if (kind === "money") return money(value, true);
  if (kind === "percent") return percent(value, true);
  if (kind === "brier") return value.toFixed(3);
  return value.toLocaleString();
}

function ClaimLedger({ page, loadingMore, onLoadMore }: { page: ResearchClaimPage; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  return <Card><CardHeader><CardTitle>Forecast review queue</CardTitle><p className="text-sm text-muted-foreground">Use a claim to inspect its statement and outcome evidence. Internal identifiers stay in the detail drawer.</p></CardHeader><CardContent className="p-0">{page.rows.length ? <div className="divide-y divide-border">{page.rows.map((row) => <Link key={String(row.claim_id)} className="grid gap-2 px-4 py-4 hover:bg-accent sm:grid-cols-[1fr_auto_auto] sm:items-center" to={`/research/predictions/${row.claim_id}?section=predictions`}><div><div className="font-semibold">{humanize(row.symbol, "Unattributed forecast")}</div><p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{text(row.statement, "No forecast statement recorded.")}</p></div><div className="text-sm">{row.probability == null ? "Probability unavailable" : `${(Number(row.probability) * 100).toFixed(0)}%`}<div className="text-xs text-muted-foreground">{humanize(row.horizon, "Horizon not recorded")}</div></div><StatusBadge tone={statusTone(row.maturity_state)}>{predictionState(row.maturity_state)}</StatusBadge></Link>)}</div> : <div className="p-6 text-sm text-muted-foreground">No forecasts have been issued. That is a collection state, not a forecast failure.</div>}{page.next_cursor ? <LoadMore loading={loadingMore} label="Load older forecasts" onClick={() => onLoadMore(page.next_cursor!)} /> : null}</CardContent></Card>;
}

function ExperimentsSection({ page, loadingMore, onLoadMore }: { page: ResearchExperimentPage | null; loadingMore: boolean; onLoadMore: (cursor: string) => void }) {
  if (!page) return <ResearchSkeleton />;
  return <div className="space-y-4"><div className="grid gap-4 lg:grid-cols-2">{page.rows.length ? page.rows.map((row) => <Link key={String(row.experiment_id)} to={`/research/experiments/${row.experiment_id}?section=experiments`} className="rounded-xl border border-border bg-card p-5 hover:bg-accent"><div className="flex items-start justify-between gap-3"><div><p className="text-xs uppercase tracking-wide text-muted-foreground">Comparison</p><h2 className="mt-1 text-lg font-semibold">{humanize(row.experiment_kind, "Strategy comparison")}</h2></div><StatusBadge tone={row.distinctness === "matched" || row.distinctness === "distinct" ? "good" : "warn"}>{statusLabel(row.distinctness, "Collecting cases")}</StatusBadge></div><div className="mt-5 grid grid-cols-2 gap-3"><MiniStat label="Baseline" value={humanize(row.champion_strategy_key ?? row.active_prompt_version, "Not selected")} /><MiniStat label="Challenger" value={humanize(row.challenger_strategy_key ?? row.candidate_prompt_version, "Not selected")} /><MiniStat label="Matched cases" value={String(row.matched_outcomes ?? "—")} /><MiniStat label="Observed" value={shortDate(row.observed_at)} /></div><ComparisonVisual row={row} /><p className="mt-4 text-sm text-muted-foreground">{text(row.explanation, "Matched comparison evidence is still being collected.")}</p></Link>) : <EmptyVisual title="No comparisons are ready yet" detail="A challenger is only compared after the same evidence window is available for both sides." />}</div>{page.next_cursor ? <LoadMore loading={loadingMore} label="Load older comparisons" onClick={() => onLoadMore(page.next_cursor!)} /> : null}</div>;
}

function EmptyVisual({ title, detail }: { title: string; detail: string }) {
  return <Card><CardContent className="flex min-h-48 items-center justify-center p-8 text-center"><div><div className="mx-auto mb-4 flex size-12 items-center justify-center rounded-full bg-primary/10 text-2xl text-primary">·</div><h2 className="font-semibold">{title}</h2><p className="mt-2 max-w-xl text-sm text-muted-foreground">{detail}</p></div></CardContent></Card>;
}

function LoadMore({ loading, label, onClick }: { loading: boolean; label: string; onClick: () => void }) {
  return <div className="border-t border-border pt-3"><button type="button" className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50" disabled={loading} onClick={onClick}>{loading ? "Loading older records…" : label}</button></div>;
}

function ResearchSkeleton() {
  return <div className="grid gap-4 lg:grid-cols-2" aria-label="Loading research"><div className="h-80 animate-pulse rounded-xl bg-muted" /><div className="h-80 animate-pulse rounded-xl bg-muted" /></div>;
}
