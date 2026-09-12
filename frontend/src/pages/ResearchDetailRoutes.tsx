import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { loadResearchArtifact, loadResearchRun, loadResearchExperiment, loadResearchPrediction, loadResearchPrompt, loadResearchStrategy } from "@/api/research";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { EvidenceFields, StoredEvidence } from "@/components/market/StoredEvidence";

const date = (value: unknown) => value ? new Date(String(value)).toLocaleString() : "—";
const text = (value: unknown, fallback = "—") => value == null || value === "" ? fallback : String(value);

export function ResearchStrategyDetailRoute() {
  const { revisionId = "" } = useParams();
  return <ResearchDetailLoader recordKey={revisionId} load={(signal) => loadResearchStrategy(revisionId, signal)} title="Strategy revision" eyebrow="Research · Strategies" render={(row) => <><DetailGrid items={[["Status", row.status], ["Family", row.strategy_family], ["Authority", row.authority_group], ["Created", date(row.created_at)], ["Promoted", date(row.promoted_at)], ["Evidence", row.evidence_status]]} /><Card><CardHeader><CardTitle>Hypothesis and implementation</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><p>{text(row.hypothesis, "No registered hypothesis.")}</p><p className="text-muted-foreground">Falsification: {text(row.hypothesis_falsification, row.falsification_rule as string || "not registered")}</p><p className="text-muted-foreground">Implementation: {text(row.implementation_id, "unavailable")} · {text(row.implementation_version, "unavailable")}</p></CardContent></Card><Link className="inline-block text-sm underline" to={`/portfolio/paper?strategy_revision=${encodeURIComponent(revisionId)}`}>Inspect this revision’s canonical paper performance</Link><StoredEvidence title="Parameters" value={row.parameters} /><StoredEvidence title="All trials, including failures" value={row.trials} /><GateMatrix gates={Array.isArray(row.gates) ? row.gates : []} /><RecordList title="Evaluations" rows={row.evaluations} label={(item) => `${text(item.evaluation_type)} · ${text(item.verdict)}`} detail={(item) => date(item.evaluated_at)} /></>} />;
}

export function ResearchPredictionDetailRoute() {
  const { claimId = "" } = useParams();
  return <ResearchDetailLoader recordKey={claimId} load={(signal) => loadResearchPrediction(claimId, signal)} title="Forecast claim" eyebrow="Research · Predictions" render={(row) => <><DetailGrid items={[["Symbol", row.symbol], ["Kind", row.claim_kind], ["Probability", row.probability == null ? "—" : `${Number(row.probability) * 100}%`], ["Horizon", row.horizon], ["Issued", date(row.issued_at)], ["Information cutoff", date(row.information_cutoff)], ["Maturity", row.maturity_state], ["Scoring", row.scoring_version]]} /><Card><CardHeader><CardTitle>Stored claim</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><p>{text(row.statement)}</p><p className="text-muted-foreground">Direction: {text(row.direction)} · Target: {text(row.target)}</p><p className="text-muted-foreground">Prompt: <Link className="underline" to={`/research/prompts/${encodeURIComponent(String(row.prompt_version))}`}>{text(row.prompt_version)}</Link> · Provider/model: {text(row.provider)} / {text(row.model)}</p></CardContent></Card><StoredEvidence title="Machine event contract" value={row.event_contract} /><Card><CardHeader><CardTitle>Observed outcome</CardTitle></CardHeader><CardContent className="grid gap-3 text-sm sm:grid-cols-2"><DetailGrid items={[["Status", row.outcome_status], ["Resolved", date(row.resolved_at)], ["Measured through", date(row.measured_through)], ["Actual return", row.actual_return], ["Direction", row.actual_direction], ["Brier", row.calibration_error], ["Event truth", row.invalidation_actual]]} /></CardContent></Card>{row.run_id ? <Link className="text-sm underline" to={`/research/runs/${encodeURIComponent(String(row.run_id))}`}>Inspect the original generating run and frozen packet</Link> : null}<StoredEvidence title="Original sources" value={row.source_refs} /><StoredEvidence title="Outcome observation evidence" value={row.outcome_metadata} /><RecordList title="Resolution attempts" rows={row.resolution_attempts} label={(item) => text(item.reason)} detail={(item) => `${text(item.status)} · ${date(item.created_at)}`} /></>} />;
}

export function ResearchPromptDetailRoute() {
  const { version = "" } = useParams();
  return <ResearchDetailLoader recordKey={version} load={(signal) => loadResearchPrompt(version, signal)} title="Prompt version" eyebrow="Research · Predictions" render={(row) => <><DetailGrid items={[["Version", row.version], ["Parent", row.parent_version], ["Semantic hash", row.semantic_effective_hash], ["Created", date(row.created_at)], ["Responses", row.response_count], ["Claims", row.claim_count], ["Authority", "advisory prompt only"]]} /><Card><CardHeader><CardTitle>Mutation rationale</CardTitle></CardHeader><CardContent className="text-sm"><p>{text(row.mutation_rationale, "No rationale recorded.")}</p><p className="mt-2 text-muted-foreground">Approved fields: {Array.isArray(row.approved_change_set) ? row.approved_change_set.join(", ") : "none"}</p></CardContent></Card><StoredEvidence title="Effective template after inheritance" value={row.effective_template} /><Diff rows={row.effective_diff ?? {}} /><StoredEvidence title="Activation and rollback history" value={row.promotion_history} /><StoredEvidence title="Source failure cases" value={row.source_failure_cases} /></>} />;
}

export function ResearchExperimentDetailRoute() {
  const { experimentId = "" } = useParams();
  return <ResearchDetailLoader recordKey={experimentId} load={(signal) => loadResearchExperiment(experimentId, signal)} title="Experiment comparison" eyebrow="Research · Experiments" render={(row) => <><DetailGrid items={[["Type", row.experiment_kind], ["Distinctness", row.distinctness], ["Input cutoff", date(row.input_cutoff)], ["Observed", date(row.observed_at)], ["Available", date(row.available_at)], ["Input hash", row.input_hash]]} /><Card><CardHeader><CardTitle>Comparison contract</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><p>{text(row.explanation, "No comparison explanation recorded.")}</p><p className="text-muted-foreground">Baseline: {text(row.champion_strategy_key, row.active_prompt_version as string || "—")} · Challenger: {text(row.challenger_strategy_key, row.candidate_prompt_version as string || "—")}</p></CardContent></Card><Card><CardHeader><CardTitle>Stored metrics</CardTitle></CardHeader><CardContent><EvidenceFields value={row.metrics ?? row.scorecard} /></CardContent></Card></>} />;
}

export function ResearchRunDetailRoute() {
  const { runId = "" } = useParams();
  return <ResearchDetailLoader recordKey={runId} load={(signal) => loadResearchRun(runId, signal)} title="Original research run" eyebrow="Research · Run" render={(row) => <><StoredEvidence title="Exact prompt and provider input at issue time" value={row.run?.request?.prompt_artifact ?? "Not retained for this legacy run; no original prompt is reconstructed."} /><StoredEvidence title="Run and provider parameters" value={row.run} /><StoredEvidence title="Frozen evidence packet" value={row.packet} /><StoredEvidence title="Stored output and validators" value={row.response} /><StoredEvidence title="Every issued claim" value={row.claims} /></>} />;
}

export function ResearchArtifactDetailRoute() {
  const { artifactId = "" } = useParams();
  return <ResearchDetailLoader recordKey={artifactId} load={(signal) => loadResearchArtifact(artifactId, signal)} title="Stored artifact" eyebrow="Research · Evidence" render={(row) => <><DetailGrid items={[["Retention", row.retention_state], ["Coverage", row.quality_status], ["Content available", row.content_available]]} /><StoredEvidence title="Known artifact records and bounded preview" value={row.records} /></>} />;
}

function ResearchDetailLoader({ recordKey, load, title, eyebrow, render }: { recordKey: string; load: (signal: AbortSignal) => Promise<Record<string, any>>; title: string; eyebrow: string; render: (row: Record<string, any>) => ReactNode }) {
  const location = useLocation();
  const [row, setRow] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { const controller = new AbortController(); setRow(null); setError(null); void load(controller.signal).then((value) => { if (!controller.signal.aborted) setRow(value); }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Research detail unavailable."); }); return () => controller.abort(); }, [recordKey]);
  return <div className="space-y-5"><PageHeader eyebrow={eyebrow} title={title} actions={<Link className="rounded-md border border-border px-3 py-2 text-sm underline-offset-4 hover:underline" to={`/research${location.search || "?section=overview"}`}>Back to Research</Link>} />{error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}{!row && !error ? <p className="text-sm text-muted-foreground">Loading stored evidence…</p> : null}{row ? render(row) : null}</div>;
}

function DetailGrid({ items }: { items: Array<[string, unknown]> }) {
  return <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{items.map(([label, value]) => <div key={label} className="rounded-md border border-border p-3"><div className="text-xs uppercase text-muted-foreground">{label}</div><div className="mt-1 break-words text-sm font-medium">{text(value)}</div></div>)}</div>;
}

function GateMatrix({ gates }: { gates: Array<Record<string, any>> }) {
  return <Card><CardHeader><CardTitle>Promotion gate matrix</CardTitle></CardHeader><CardContent>{gates.length ? <div className="space-y-2">{gates.map((gate) => <div key={String(gate.gate_id)} className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2 text-sm last:border-0"><div className="min-w-0 flex-1"><strong>{text(gate.gate_code)}</strong><EvidenceFields value={gate.metrics} /><EvidenceFields value={gate.evidence} /></div><StatusBadge tone={gate.verdict === "pass" ? "good" : gate.verdict === "unavailable" ? "warn" : "bad"}>{text(gate.verdict)}</StatusBadge></div>)}</div> : <p className="text-sm text-muted-foreground">No backend gate result is recorded for this revision.</p>}</CardContent></Card>;
}

function RecordList({ title, rows, label, detail }: { title: string; rows: Array<Record<string, any>>; label: (row: Record<string, any>) => string; detail: (row: Record<string, any>) => string }) {
  return <Card><CardHeader><CardTitle>{title}</CardTitle></CardHeader><CardContent>{rows.length ? <div className="space-y-2">{rows.map((row, index) => <div key={String(row.evaluation_id ?? row.attempt_id ?? index)} className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2 text-sm last:border-0"><span>{label(row)}</span><span className="text-muted-foreground">{detail(row)}</span></div>)}</div> : <p className="text-sm text-muted-foreground">No records are available in this scope.</p>}</CardContent></Card>;
}

function Diff({ rows }: { rows: Record<string, any> }) {
  const entries = Object.entries(rows);
  return <Card><CardHeader><CardTitle>Effective prompt diff</CardTitle></CardHeader><CardContent>{entries.length ? <div className="space-y-2">{entries.map(([key, value]) => <div key={key} className="rounded-md border border-border p-3 text-sm"><strong>{key}</strong><div className="mt-1 text-muted-foreground"><p>Before</p><EvidenceFields value={value?.before} /><p className="mt-2">After</p><EvidenceFields value={value?.after} /></div></div>)}</div> : <p className="text-sm text-muted-foreground">No semantic change from the parent; rationale alone does not create a new experiment.</p>}</CardContent></Card>;
}
