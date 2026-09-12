import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { loadResearchArtifact, loadResearchExperiment, loadResearchPrediction, loadResearchPrompt, loadResearchRun, loadResearchStrategy } from "@/api/research";
import { StoredEvidence, TechnicalDetails } from "@/components/market/StoredEvidence";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { eventLabel } from "@/presentation/actions";
import { gateLabel, humanize, percent, dateTime, statusLabel, statusTone } from "@/presentation/labels";
import { ComparisonVisual } from "@/pages/ResearchWorkbenchRoute";

const text = (value: unknown, fallback = "—") => value == null || value === "" ? fallback : String(value);

export function ResearchStrategyDetailRoute() {
  const { revisionId = "" } = useParams();
  return <ResearchDetailLoader recordKey={revisionId} load={(signal) => loadResearchStrategy(revisionId, signal)} title="Strategy improvement" eyebrow="Research · Strategies" render={(row) => <><DetailGrid items={[["State", statusLabel(row.status, "Not started")], ["Family", humanize(row.strategy_family, "General strategy")], ["Created", dateTime(row.created_at)], ["Promoted", dateTime(row.promoted_at, "Not promoted")]]} /><DecisionPanel title="What is being tested"><p>{text(row.hypothesis, "No hypothesis is recorded yet.")}</p><p className="mt-3 text-sm text-muted-foreground">{text(row.hypothesis_falsification, row.falsification_rule as string || "No falsification rule recorded.")}</p></DecisionPanel><ReadinessCard row={row} /><GateMatrix gates={Array.isArray(row.gates) ? row.gates : []} /><Link className="inline-block text-sm font-medium text-primary hover:underline" to={`/portfolio/paper?strategy_revision=${encodeURIComponent(revisionId)}`}>View this strategy’s paper performance →</Link><TechnicalDetails><StoredEvidence title="Parameters and implementation" value={{ parameters: row.parameters, implementation_id: row.implementation_id, implementation_version: row.implementation_version }} /><StoredEvidence title="All trials and evaluations" value={{ trials: row.trials, evaluations: row.evaluations }} /><StoredEvidence title="Source and validation records" value={{ manifest: row.manifest, dossier: row.validation_dossier, artifact: row.artifact }} /></TechnicalDetails></>} />;
}

export function ResearchPredictionDetailRoute() {
  const { claimId = "" } = useParams();
  return <ResearchDetailLoader recordKey={claimId} load={(signal) => loadResearchPrediction(claimId, signal)} title="Forecast review" eyebrow="Research · Forecast quality" render={(row) => <><DetailGrid items={[["Symbol", row.symbol], ["Probability", row.probability == null ? "Unavailable" : percent(row.probability)], ["Horizon", humanize(row.horizon, "Not recorded")], ["State", statusLabel(row.maturity_state, "Awaiting outcome")]]} /><DecisionPanel title="What was forecast"><p className="text-lg leading-7">{text(row.statement, "No forecast statement recorded.")}</p><p className="mt-3 text-sm text-muted-foreground">{humanize(row.direction, "Direction unavailable")} · {humanize(row.target, "Target unavailable")}</p></DecisionPanel><OutcomePanel row={row} />{row.run_id ? <Link className="text-sm font-medium text-primary hover:underline" to={`/research/runs/${encodeURIComponent(String(row.run_id))}`}>Open the original research run →</Link> : null}<TechnicalDetails><StoredEvidence title="Claim event contract" value={row.event_contract} /><StoredEvidence title="Original sources and outcome evidence" value={{ source_refs: row.source_refs, outcome_metadata: row.outcome_metadata }} /><StoredEvidence title="Resolution attempts" value={row.resolution_attempts} /></TechnicalDetails></>} />;
}

export function ResearchPromptDetailRoute() {
  const { version = "" } = useParams();
  return <ResearchDetailLoader recordKey={version} load={(signal) => loadResearchPrompt(version, signal)} title="Prompt experiment" eyebrow="Research · Forecast quality" render={(row) => <><DetailGrid items={[["Version", humanize(row.version)], ["Parent", humanize(row.parent_version, "No parent")], ["Created", dateTime(row.created_at)], ["Claims", row.claim_count ?? 0]]} /><DecisionPanel title="Why this prompt exists"><p>{text(row.mutation_rationale, "No mutation rationale is recorded.")}</p><p className="mt-3 text-sm text-muted-foreground">{Array.isArray(row.approved_change_set) && row.approved_change_set.length ? `Change areas: ${row.approved_change_set.map((item: unknown) => humanize(item)).join(", ")}.` : "No approved change areas recorded."}</p></DecisionPanel><Card><CardHeader><CardTitle>Promotion history</CardTitle></CardHeader><CardContent><RecordList rows={Array.isArray(row.promotion_history) ? row.promotion_history : []} label={(item) => eventLabel(item.event_type ?? item.status)} detail={(item) => dateTime(item.event_at ?? item.created_at)} /></CardContent></Card><TechnicalDetails><StoredEvidence title="Effective template" value={row.effective_template} /><StoredEvidence title="Semantic diff" value={row.effective_diff} /><StoredEvidence title="Failure cases and raw promotion records" value={{ source_failure_cases: row.source_failure_cases, promotion_history: row.promotion_history, semantic_effective_hash: row.semantic_effective_hash }} /></TechnicalDetails></>} />;
}

export function ResearchExperimentDetailRoute() {
  const { experimentId = "" } = useParams();
  return <ResearchDetailLoader recordKey={experimentId} load={(signal) => loadResearchExperiment(experimentId, signal)} title="Experiment comparison" eyebrow="Research · Comparisons" render={(row) => <><DetailGrid items={[["Type", humanize(row.experiment_kind, "Comparison")], ["State", statusLabel(row.distinctness, "Collecting cases")], ["Input cutoff", dateTime(row.input_cutoff)], ["Observed", dateTime(row.observed_at)]]} /><DecisionPanel title="What is being compared"><p>{text(row.explanation, "Matched evidence is still being collected for this comparison.")}</p><div className="mt-5 grid gap-4 sm:grid-cols-3"><Metric label="Baseline" value={humanize(row.champion_strategy_key ?? row.active_prompt_version, "Not selected")} /><Metric label="Challenger" value={humanize(row.challenger_strategy_key ?? row.candidate_prompt_version, "Not selected")} /><Metric label="Comparable cases" value={text(row.matched_outcomes, "—")} /></div><ComparisonVisual row={row} /></DecisionPanel><TechnicalDetails><StoredEvidence title="Stored comparison metrics" value={row.metrics ?? row.scorecard} /><StoredEvidence title="Comparison contract" value={row} /></TechnicalDetails></>} />;
}

export function ResearchRunDetailRoute() {
  const { runId = "" } = useParams();
  return <ResearchDetailLoader recordKey={runId} load={(signal) => loadResearchRun(runId, signal)} title="Research run" eyebrow="Research · Technical record" render={(row) => <><DecisionPanel title="Original research run"><p>This record preserves the inputs and outputs of a research request. It is available for audit, not as a trader decision.</p><p className="mt-3 text-sm text-muted-foreground">{statusLabel(row.run?.status, "Run status unavailable")}</p></DecisionPanel><TechnicalDetails><StoredEvidence title="Prompt and provider input" value={row.run?.request?.prompt_artifact} /><StoredEvidence title="Run parameters" value={row.run} /><StoredEvidence title="Frozen evidence packet" value={row.packet} /><StoredEvidence title="Stored output and claims" value={{ response: row.response, claims: row.claims }} /></TechnicalDetails></>} />;
}

export function ResearchArtifactDetailRoute() {
  const { artifactId = "" } = useParams();
  return <ResearchDetailLoader recordKey={artifactId} load={(signal) => loadResearchArtifact(artifactId, signal)} title="Evidence artifact" eyebrow="Research · Technical record" render={(row) => <><DetailGrid items={[["Retention", statusLabel(row.retention_state, "Unknown")], ["Evidence quality", statusLabel(row.quality_status, "Unavailable")], ["Content", row.content_available ? "Available" : "Not available"]]} /><DecisionPanel title="Evidence availability"><p>{row.content_available ? "The bounded artifact is available for review." : "The artifact record is retained, but its content is not currently available."}</p></DecisionPanel><TechnicalDetails><StoredEvidence title="Known artifact records" value={row.records} /></TechnicalDetails></>} />;
}

function ResearchDetailLoader({ recordKey, load, title, eyebrow, render }: { recordKey: string; load: (signal: AbortSignal) => Promise<Record<string, any>>; title: string; eyebrow: string; render: (row: Record<string, any>) => ReactNode }) {
  const location = useLocation();
  const [row, setRow] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { const controller = new AbortController(); setRow(null); setError(null); void load(controller.signal).then((value) => { if (!controller.signal.aborted) setRow(value); }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Research detail unavailable."); }); return () => controller.abort(); }, [recordKey]);
  return <div className="space-y-5"><PageHeader eyebrow={eyebrow} title={title} actions={<Link className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent" to={`/research${location.search || "?section=overview"}`}>Back to Research</Link>} />{error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}{!row && !error ? <p className="text-sm text-muted-foreground">Loading research decision…</p> : null}{row ? render(row) : null}</div>;
}

function DetailGrid({ items }: { items: Array<[string, unknown]> }) {
  return <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{items.map(([label, value]) => <div key={label} className="rounded-xl border border-border bg-card p-4"><div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div><div className="mt-2 break-words text-sm font-semibold">{humanize(value)}</div></div>)}</div>;
}

function DecisionPanel({ title, children }: { title: string; children: ReactNode }) {
  return <Card><CardHeader><CardTitle>{title}</CardTitle></CardHeader><CardContent className="text-sm leading-6">{children}</CardContent></Card>;
}

function ReadinessCard({ row }: { row: Record<string, any> }) {
  const evaluations = Number(row.evaluation_count ?? row.evaluations?.length ?? 0);
  const paper = Number(row.paper_order_count ?? 0);
  return <Card><CardHeader><CardTitle>Readiness</CardTitle><p className="text-sm text-muted-foreground">A candidate is promoted only after the recorded checks pass.</p></CardHeader><CardContent className="grid gap-4 sm:grid-cols-3"><Metric label="Historical checks" value={String(evaluations)} /><Metric label="Paper orders" value={String(paper)} /><Metric label="Current state" value={statusLabel(row.promotability ?? row.status, "Collecting evidence")} /></CardContent></Card>;
}

function OutcomePanel({ row }: { row: Record<string, any> }) {
  return <Card><CardHeader><CardTitle>What happened</CardTitle></CardHeader><CardContent className="grid gap-4 sm:grid-cols-3"><Metric label="Outcome" value={statusLabel(row.outcome_status ?? row.maturity_state, "Awaiting outcome")} /><Metric label="Actual return" value={percent(row.actual_return)} /><Metric label="Forecast error" value={row.calibration_error == null ? "—" : Number(row.calibration_error).toFixed(3)} /><p className="text-sm text-muted-foreground sm:col-span-3">Resolved {dateTime(row.resolved_at, "Not resolved")} · measured through {dateTime(row.measured_through, "Not recorded")}.</p></CardContent></Card>;
}

function GateMatrix({ gates }: { gates: Array<Record<string, any>> }) {
  return <Card><CardHeader><CardTitle>Promotion progress</CardTitle></CardHeader><CardContent>{gates.length ? <div className="space-y-3">{gates.map((gate, index) => <div key={String(gate.gate_id ?? index)} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><div><p className="font-medium">{gateLabel(gate.gate_code)}</p><p className="mt-1 text-xs text-muted-foreground">{gate.verdict === "pass" ? "Requirement met" : "More evidence or review is required."}</p></div><StatusBadge tone={statusTone(gate.verdict)}>{statusLabel(gate.verdict, "Pending")}</StatusBadge></div>)}</div> : <p className="text-sm text-muted-foreground">No promotion checks are recorded yet.</p>}</CardContent></Card>;
}

function RecordList({ rows, label, detail }: { rows: Array<Record<string, any>>; label: (row: Record<string, any>) => string; detail: (row: Record<string, any>) => string }) {
  return rows.length ? <div className="space-y-2">{rows.slice(0, 12).map((row, index) => <div key={String(row.id ?? row.evaluation_id ?? index)} className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2 text-sm last:border-0"><span>{label(row)}</span><span className="text-muted-foreground">{detail(row)}</span></div>)}</div> : <p className="text-sm text-muted-foreground">No records are available.</p>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div><div className="mt-1 break-words text-lg font-semibold">{value}</div></div>;
}
