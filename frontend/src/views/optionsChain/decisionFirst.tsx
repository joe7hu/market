import { useEffect, useId, useState, type ComponentType, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  loadOptionHistorySnapshots, loadOptionRelativeValues, loadOptionsCandidates, loadOptionsDecisionBrief,
  loadOptionsLearningProgress, loadOptionsPaperJournal, type OptionHistorySnapshot, type OptionRelativeValue,
  type OptionsDecisionBrief, type OptionsDecisionCandidate, type OptionsLearningProgress, type OptionsPaperJournalRow,
} from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { WorkspacePage } from "../workspacePage";

type Tab = "decision" | "discover" | "evidence" | "journal" | "learn";
const TABS: Tab[] = ["decision", "discover", "evidence", "journal", "learn"];

export function DecisionFirstOptionsChainPage({ EvidenceWorkspace }: { EvidenceWorkspace: ComponentType }) {
  const [search, setSearch] = useSearchParams();
  const tab = isTab(search.get("tab")) ? search.get("tab") as Tab : "decision";
  const lane = search.get("lane") === "anomaly" ? "anomaly" : "thesis";
  const [brief, setBrief] = useState<OptionsDecisionBrief | null>(null);
  const [snapshots, setSnapshots] = useState<OptionHistorySnapshot[]>([]);
  const [thesisCandidates, setThesisCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [anomalyCandidates, setAnomalyCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [rejections, setRejections] = useState<OptionRelativeValue[]>([]);
  const [journal, setJournal] = useState<OptionsPaperJournalRow[]>([]);
  const [learning, setLearning] = useState<OptionsLearningProgress[]>([]);
  const [error, setError] = useState<string | null>(null);
  const panelId = useId();
  const select = (next: Partial<{ tab: Tab; lane: "thesis" | "anomaly" }>) => {
    const values = new URLSearchParams(search);
    if (next.tab) values.set("tab", next.tab);
    if (next.lane) values.set("lane", next.lane);
    setSearch(values, { replace: true });
  };

  useEffect(() => {
    const controller = new AbortController();
    void loadOptionHistorySnapshots("QQQ", controller.signal).then((result) => setSnapshots(result.rows)).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void loadOptionsDecisionBrief("QQQ", lane, controller.signal).then((next) => { setBrief(next); setError(null); }).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, [lane]);
  useEffect(() => {
    if (tab !== "discover") return;
    const controller = new AbortController();
    void Promise.all([
      loadOptionsCandidates({ symbol: "QQQ", lane: "thesis", limit: 100 }, controller.signal),
      loadOptionsCandidates({ symbol: "QQQ", lane: "anomaly", limit: 100 }, controller.signal),
      loadOptionRelativeValues({ symbol: "QQQ", classification: "rejected", limit: 50 }, controller.signal),
    ]).then(([thesis, anomaly, rejected]) => { setThesisCandidates(thesis.rows); setAnomalyCandidates(anomaly.rows); setRejections(rejected.rows); setError(null); }).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, [tab]);
  useEffect(() => {
    if (tab !== "journal") return;
    const controller = new AbortController();
    void loadOptionsPaperJournal("QQQ", controller.signal).then((payload) => { setJournal(payload.rows); setError(null); }).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, [tab]);
  useEffect(() => {
    if (tab !== "learn") return;
    const controller = new AbortController();
    void loadOptionsLearningProgress("QQQ", controller.signal).then((payload) => { setLearning(payload.rows); setError(null); }).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, [tab]);

  return <WorkspacePage eyebrow="QQQ underwriting" title="Options Decision System" subtitle="Paper-only underwriting. Options Radar remains the broad discovery surface." metrics={[
    ["State", brief?.state ?? "COLLECTING", "Paper-only; no live order submission.", tone(brief?.state)],
    ["Lane", lane === "thesis" ? "Thesis-led" : "Anomaly research", "Lanes are deliberately not universally ranked.", "info"],
    ["Captures", `${snapshots.length}`, snapshots[0] ? "Latest complete generation is available as evidence." : "Waiting for a complete capture.", snapshots[0] ? "good" : "warn"],
  ]} actions={<div className="flex items-center gap-2"><label className="text-xs">Lane <select aria-label="Decision lane" className="ml-1 min-h-11 rounded border bg-background px-2 text-sm" value={lane} onChange={(event) => select({ lane: event.target.value as "thesis" | "anomaly" })}><option value="thesis">Thesis</option><option value="anomaly">Anomaly</option></select></label><StatusBadge tone={brief?.mode === "paper" ? "warn" : "info"}>{brief?.mode ?? "shadow"} mode</StatusBadge></div>}>
    {error ? <p role="alert" className="rounded border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
    <div role="tablist" aria-label="Options decision workspaces" className="grid w-full grid-cols-5 gap-1 rounded-md border border-border bg-muted p-1">
      {TABS.map((value) => <button key={value} id={`${panelId}-${value}`} role="tab" type="button" aria-selected={tab === value} aria-controls={`${panelId}-panel`} tabIndex={tab === value ? 0 : -1} onKeyDown={(event) => tabKey(event, value, select)} onClick={() => select({ tab: value })} className={`min-h-11 rounded px-1 text-xs font-medium sm:px-3 sm:text-sm ${tab === value ? "bg-background shadow-sm" : "text-muted-foreground"}`}>{label(value)}</button>)}
    </div>
    <div id={`${panelId}-panel`} role="tabpanel" aria-labelledby={`${panelId}-${tab}`} className="pt-1">
      {tab === "decision" ? <Decision brief={brief} candidate={brief?.strongest_candidate} /> : null}
      {tab === "discover" ? <Discover thesis={thesisCandidates} anomaly={anomalyCandidates} rejections={rejections} /> : null}
      {tab === "evidence" ? <EvidenceWorkspace /> : null}
      {tab === "journal" ? <Journal rows={journal} /> : null}
      {tab === "learn" ? <Learn rows={learning} /> : null}
    </div>
  </WorkspacePage>;
}

function Decision({ brief, candidate }: { brief: OptionsDecisionBrief | null; candidate: OptionsDecisionCandidate | null | undefined }) {
  if (!brief) return <Empty text="Loading the compact decision brief…" />;
  const readiness = brief.readiness;
  return <div className="space-y-4"><section className="rounded-lg border border-border bg-card p-4"><div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-lg font-medium">Readiness checklist</h2><StatusBadge tone={tone(brief.state)}>{brief.state}</StatusBadge></div><dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4"><Metric label="Eligible / fit / solver" value={`${readiness.analysis.eligible_groups} / ${readiness.analysis.fit_attempts} / ${readiness.analysis.succeeded_groups}`} /><Metric label="Canary sessions" value={`${readiness.canary.qualified_regular_sessions} / ${readiness.canary.required_regular_sessions}`} /><Metric label="Thesis" value={readiness.thesis.eligible ? `Eligible ${readiness.thesis.revision ?? ""}` : "QQQ thesis v2 required"} /><Metric label="Next action" value={readiness.next_required_action.replaceAll("_", " ")} /></dl>{readiness.top_blockers.length ? <p className="mt-3 text-sm text-muted-foreground">Blockers: {readiness.top_blockers.map((item) => `${item.blocker} (${item.count})`).join(" · ")}</p> : null}<Link to="/theses?symbol=QQQ" className="mt-3 inline-flex min-h-11 items-center rounded border px-3 text-sm underline-offset-2 hover:underline">Open QQQ thesis</Link></section>{candidate ? <Candidate candidate={candidate} state={brief.state} /> : <Empty text={brief.summary.message ?? "No QQQ candidate has cleared the evidence gate."} />}</div>;
}

function Candidate({ candidate, state }: { candidate: OptionsDecisionCandidate; state: string }) { return <section className="rounded-lg border border-border bg-card p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-medium">{candidate.structure.replaceAll("_", " ")}</h2><p className="mt-1 text-sm text-muted-foreground">{candidate.option_type.toUpperCase()} {candidate.expiration} · {money(candidate.strike)}</p><StatusBadge tone={tone(state)}>{state}</StatusBadge></div><p className="max-w-lg text-sm text-muted-foreground">Historical evidence is not a fill guarantee. A paper action still requires a later coherent quote cohort.</p></div><dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4"><Metric label="Conservative entry" value={money(candidate.conservative_entry.price)} /><Metric label="One-unit maximum loss" value={money(candidate.one_unit_max_loss)} /><Metric label="Fair value" value={`${money(candidate.fair_value_interval.low)} – ${money(candidate.fair_value_interval.high)}`} /><Metric label="EV / lower 95%" value={`${money(candidate.expected_value_interval.expected)} / ${money(candidate.expected_value_interval.lower_95)}`} /><Metric label="Modeled net edge" value={money(candidate.modeled_net_edge)} /><Metric label="Quote age / skew" value={`${seconds(candidate.quote_quality.max_quote_age_seconds)} / ${seconds(candidate.quote_quality.interleg_skew_seconds)}`} /><Metric label="Thesis invalidation" value={candidate.thesis.invalidation ?? "Required"} /><Metric label="Comparable outcomes" value={`${candidate.comparable_exact_structure_outcomes.sample_size ?? 0} mature`} /></dl><div className="mt-4 grid gap-2 text-sm sm:grid-cols-2">{candidate.legs.map((leg) => <div key={`${leg.contract_id}-${leg.side}`} className="rounded border p-2"><strong>{leg.side} {leg.option_type} {money(leg.strike)}</strong><br />Bid/ask {money(leg.bid)} / {money(leg.ask)} · OI {number(leg.open_interest)}</div>)}</div>{candidate.blockers.length ? <p className="mt-3 text-sm text-muted-foreground">Blockers: {candidate.blockers.join(" · ")}</p> : null}</section>; }

function Discover({ thesis, anomaly, rejections }: { thesis: OptionsDecisionCandidate[]; anomaly: OptionsDecisionCandidate[]; rejections: OptionRelativeValue[] }) { return <div className="grid gap-4 xl:grid-cols-2"><CandidateList title="Thesis-led candidates" rows={thesis} empty="No thesis-led QQQ candidates. Add a valid QQQ thesis before treating research evidence as underwriting." /><CandidateList title="Anomaly research" rows={anomaly} empty="No anomaly research candidates. This lane is evidence, never a universal rank." /><section className="rounded-lg border border-border bg-card p-4 xl:col-span-2"><h2 className="font-medium">Rejected relative-value evidence</h2>{rejections.length ? <div className="mt-3 divide-y text-sm">{rejections.map((row) => <div className="grid gap-1 py-3 sm:grid-cols-[1fr_1fr_2fr]" key={row.id}><span>{row.option_type} {row.expiration} · {money(row.strike)}</span><span>{row.quality_status}</span><span>{row.blockers.join(" · ") || "No modeled edge"}</span></div>)}</div> : <Empty text="No rejected rows are available for the current complete capture." />}</section></div>; }
function CandidateList({ title, rows, empty }: { title: string; rows: OptionsDecisionCandidate[]; empty: string }) { return <section className="rounded-lg border border-border bg-card p-4"><h2 className="font-medium">{title}</h2>{rows.length ? <div className="mt-3 divide-y text-sm">{rows.map((row) => <div className="grid gap-1 py-3 sm:grid-cols-4" key={row.decision_id}><span>{row.structure.replaceAll("_", " ")}</span><span>{row.expiration} {money(row.strike)}</span><span>{row.paper_state}</span><span>{money(row.modeled_net_edge)}</span></div>)}</div> : <Empty text={empty} />}</section>; }
function Journal({ rows }: { rows: OptionsPaperJournalRow[] }) { return <section className="rounded-lg border border-border bg-card p-4"><h2 className="font-medium">Paper journal</h2>{rows.length ? <div className="mt-3 divide-y text-sm">{rows.map((row) => <div className="grid gap-1 py-3 sm:grid-cols-4" key={row.shadow_id}><span>{row.lifecycle}</span><span>{row.structure ?? "—"}</span><span>{row.conservative_fill_basis ?? row.pending_entry_reason ?? "pending"}</span><span>{row.latest_mark === null ? "Missing mark" : `${money(row.latest_mark)} · ${percent(row.current_return)}`}</span><span className="sm:col-span-4 text-muted-foreground">{row.assignment_warning}</span></div>)}</div> : <Empty text="No system shadows or paper actions exist yet." />}</section>; }
function Learn({ rows }: { rows: OptionsLearningProgress[] }) { return <section className="rounded-lg border border-border p-4"><h2 className="font-medium">Learning by exact structure, regime, and revision</h2>{rows.length ? <div className="mt-3 divide-y text-sm">{rows.map((row) => <div className="grid gap-1 py-3 sm:grid-cols-4" key={`${row.structure}-${row.market_regime}-${row.model_revision}`}><span>{row.structure.replaceAll("_", " ")}</span><span>{row.market_regime ?? "Regime unavailable"}</span><span>{row.mature_outcomes} / {row.required_mature_outcomes} mature</span><span>Lower 95% {percent(row.lower_95_expectancy)} · Brier {number(row.brier_score, 3)}</span><span className="sm:col-span-4 text-muted-foreground">{row.missing_prerequisites.join(" · ") || "Exact calibration gates complete"}</span></div>)}</div> : <Empty text="Collecting exact structure, regime, and model-revision outcomes. PAPER_READY still requires 30 mature outcomes." />}</section>; }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-0.5 font-medium">{value}</dd></div>; }
function Empty({ text }: { text: string }) { return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">{text}</p>; }
function label(tab: Tab) { return tab === "decision" ? "Decision" : tab === "discover" ? "Discover" : tab === "evidence" ? "Evidence" : tab === "journal" ? "Journal" : "Learn"; }
function tone(state?: string): "good" | "warn" | "info" | "muted" { return state === "PAPER_READY" ? "good" : state === "REJECT" ? "warn" : state === "WATCH" ? "info" : "muted"; }
function money(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "currency", currency: "USD" }); }
function percent(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }); }
function number(value: number | null | undefined, digits = 0) { return value === null || value === undefined ? "—" : value.toFixed(digits); }
function seconds(value: number | null) { return value === null ? "—" : `${value.toFixed(1)}s`; }
function isTab(value: string | null): value is Tab { return value !== null && TABS.includes(value as Tab); }
function ignoreAbort(cause: unknown, setError: (value: string | null) => void) { if ((cause as { name?: string }).name !== "AbortError") setError(cause instanceof Error ? cause.message : "Unable to load options decision data"); }
function tabKey(event: KeyboardEvent<HTMLButtonElement>, current: Tab, select: (value: { tab: Tab }) => void) { if (!(["ArrowLeft", "ArrowRight", "Home", "End"] as string[]).includes(event.key)) return; event.preventDefault(); const index = TABS.indexOf(current); const next = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : (index + (event.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length; select({ tab: TABS[next] }); document.getElementById((event.currentTarget.parentElement?.querySelectorAll("[role=tab]")[next] as HTMLElement | undefined)?.id ?? "")?.focus(); }
