import {
  Activity, ArrowRight, BarChart3, BookOpenCheck, Check, CircleDashed, FlaskConical,
  Microscope, ShieldCheck, Target, X,
} from "lucide-react";
import { useEffect, useId, useState, type ComponentType, type KeyboardEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  loadOptionHistorySnapshots, loadOptionsCandidates, loadOptionsLearningProgress, loadOptionsPaperJournal,
  loadOptionsShadowObservations,
  loadOptionsWorkspace, type OptionHistorySnapshot, type OptionsDecisionBrief, type OptionsDecisionCandidate,
  type OptionsLearningProgress, type OptionsPaperJournalRow, type OptionsWorkspacePayload,
} from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { WorkspacePage } from "../workspacePage";
import { blockerCopy, decisionPresentation, sentence, summaryNumber } from "./decisionDesk";
import { JournalDesk } from "./journalDesk";

type View = "desk" | "evidence" | "record";
const VIEWS: View[] = ["desk", "evidence", "record"];
type EvidenceComponent = ComponentType<{ embedded?: boolean }>;

export function DecisionFirstOptionsChainPage({ EvidenceWorkspace }: { EvidenceWorkspace: EvidenceComponent }) {
  const [search, setSearch] = useSearchParams();
  const view = normalizeView(search.get("tab"));
  const lane = search.get("lane") === "anomaly" ? "anomaly" : "thesis";
  const [brief, setBrief] = useState<OptionsDecisionBrief | null>(null);
  const [workspace, setWorkspace] = useState<OptionsWorkspacePayload | null>(null);
  const [snapshots, setSnapshots] = useState<OptionHistorySnapshot[]>([]);
  const [thesisCandidates, setThesisCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [anomalyCandidates, setAnomalyCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [journal, setJournal] = useState<OptionsPaperJournalRow[]>([]);
  const [journalCount, setJournalCount] = useState(0);
  const [shadow, setShadow] = useState<OptionsPaperJournalRow[]>([]);
  const [shadowCount, setShadowCount] = useState(0);
  const [learning, setLearning] = useState<OptionsLearningProgress[]>([]);
  const [error, setError] = useState<string | null>(null);
  const panelId = useId();
  const select = (next: Partial<{ view: View; lane: "thesis" | "anomaly" }>) => {
    const values = new URLSearchParams(search);
    if (next.view) values.set("tab", next.view);
    if (next.lane) values.set("lane", next.lane);
    setSearch(values, { replace: true });
  };

  useEffect(() => {
    const controller = new AbortController();
    void loadOptionHistorySnapshots("QQQ", controller.signal)
      .then((result) => setSnapshots(result.rows))
      .catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    let controller: AbortController | null = null;
    const refresh = () => {
      controller?.abort();
      controller = new AbortController();
      void loadOptionsWorkspace("QQQ", lane, controller.signal)
        .then((next) => { setWorkspace(next); setBrief(next.decision_brief); setError(null); })
        .catch((cause: unknown) => ignoreAbort(cause, setError));
    };
    refresh();
    const interval = window.setInterval(refresh, regularSessionNow() ? 30_000 : 300_000);
    const onFocus = () => refresh();
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      controller?.abort();
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [lane]);

  useEffect(() => {
    if (view !== "desk") return;
    const controller = new AbortController();
    void Promise.all([
      loadOptionsCandidates({ symbol: "QQQ", lane: "thesis", scope: "current", limit: 12 }, controller.signal),
      loadOptionsCandidates({ symbol: "QQQ", lane: "anomaly", scope: "current", limit: 12 }, controller.signal),
    ]).then(([thesis, anomaly]) => {
      setThesisCandidates(thesis.rows);
      setAnomalyCandidates(anomaly.rows);
      setError(null);
    }).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, [view, workspace?.capture_generation_id]);

  useEffect(() => {
    if (view !== "record") return;
    const controller = new AbortController();
    void Promise.all([
      loadOptionsPaperJournal("QQQ", controller.signal),
      loadOptionsShadowObservations("QQQ", controller.signal),
      loadOptionsLearningProgress("QQQ", controller.signal),
    ]).then(([nextJournal, nextShadow, nextLearning]) => {
      setJournal(nextJournal.rows);
      setJournalCount(nextJournal.count);
      setShadow(nextShadow.rows);
      setShadowCount(nextShadow.count);
      setLearning(nextLearning.rows);
      setError(null);
    }).catch((cause: unknown) => ignoreAbort(cause, setError));
    return () => controller.abort();
  }, [view]);

  const capture = brief?.readiness.capture;
  const analysis = brief?.readiness.analysis;
  const canary = brief?.readiness.canary;
  return (
    <WorkspacePage
      eyebrow="QQQ · paper underwriting"
      title="Options Trade Desk"
      subtitle="A decision workstation for one question: is there a QQQ options setup worth taking on paper right now?"
      metrics={[
        ["Verdict", brief ? verdictMetric(brief) : "Loading", brief ? decisionPresentation(brief).detail : "Reading the latest complete evidence.", brief ? decisionPresentation(brief).tone : "muted"],
        ["Market data", capture?.capture_state === "complete" ? "Current" : "Unavailable", capture ? `${capture.complete_captures.toLocaleString()} complete captures · latest ${(capture.completeness ?? 0).toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 })}` : "Loading capture health.", capture?.capture_state === "complete" ? "good" : "warn"],
        ["Model run", analysis ? `${analysis.succeeded_groups}/${analysis.fit_attempts} fits` : "Loading", analysis ? `${analysis.eligible_groups} eligible groups · ${analysis.solver_failures} solver failures` : "Reading the latest analysis.", analysis && analysis.fit_attempts > 0 && analysis.succeeded_groups === analysis.fit_attempts ? "good" : "warn"],
        ["Reliability gate", canary ? `${canary.qualified_regular_sessions}/${canary.required_regular_sessions} sessions` : "Loading", canary && canary.qualified_regular_sessions >= canary.required_regular_sessions ? "Qualified for the current revision." : "Paper readiness remains gated; research evidence is still usable.", canary && canary.qualified_regular_sessions >= canary.required_regular_sessions ? "good" : "info"],
      ]}
      actions={<DeskActions lane={lane} mode={brief?.mode} onLane={(next) => select({ lane: next })} />}
    >
      {error ? <p role="alert" className="rounded border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
      <ViewNav active={view} panelId={panelId} onSelect={(next) => select({ view: next })} />
      <div id={`${panelId}-panel`} role="tabpanel" aria-labelledby={`${panelId}-${view}`}>
        {view === "desk" ? <TradeDesk brief={brief} workspace={workspace} thesis={thesisCandidates} anomaly={anomalyCandidates} snapshots={snapshots} onEvidence={() => select({ view: "evidence" })} onRecord={() => select({ view: "record" })} /> : null}
        {view === "evidence" ? <EvidenceWorkspace embedded /> : null}
        {view === "record" ? <JournalDesk journal={journal} journalCount={journalCount} shadow={shadow} shadowCount={shadowCount} learning={learning} /> : null}
      </div>
    </WorkspacePage>
  );
}

function DeskActions({ lane, mode, onLane }: { lane: "thesis" | "anomaly"; mode?: string; onLane: (lane: "thesis" | "anomaly") => void }) {
  return <div className="flex flex-wrap items-center gap-2">
    <div className="flex rounded-md border border-border bg-muted p-1" aria-label="Research lens">
      <button type="button" onClick={() => onLane("thesis")} className={toggleClass(lane === "thesis")}>Thesis lens</button>
      <button type="button" onClick={() => onLane("anomaly")} className={toggleClass(lane === "anomaly")}>Anomaly lens</button>
    </div>
    <StatusBadge tone={mode === "paper" ? "warn" : "info"}>{mode ?? "shadow"} only</StatusBadge>
  </div>;
}

function ViewNav({ active, panelId, onSelect }: { active: View; panelId: string; onSelect: (view: View) => void }) {
  const specs: Array<[View, ReactNode, string, string]> = [
    ["desk", <Target className="size-4" />, "Trade desk", "Verdict, gates, and candidates"],
    ["evidence", <Microscope className="size-4" />, "Market evidence", "Chain, volatility, and history"],
    ["record", <BookOpenCheck className="size-4" />, "Journal", "Paper trades, shadow research, calibration"],
  ];
  return <div role="tablist" aria-label="Options trade desk views" className="grid gap-2 rounded-xl border border-border bg-muted/50 p-2 md:grid-cols-3">
    {specs.map(([value, icon, title, detail]) => <button
      key={value}
      id={`${panelId}-${value}`}
      role="tab"
      type="button"
      aria-selected={active === value}
      aria-controls={`${panelId}-panel`}
      tabIndex={active === value ? 0 : -1}
      onKeyDown={(event) => viewKey(event, value, onSelect)}
      onClick={() => onSelect(value)}
      className={`flex min-h-14 items-center gap-3 rounded-lg px-3 text-left outline-offset-2 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary ${active === value ? "border border-border bg-background shadow-sm" : "text-muted-foreground hover:bg-background/60 hover:text-foreground"}`}
    >
      <span className={active === value ? "text-primary" : ""}>{icon}</span>
      <span><strong className="block text-sm font-medium text-foreground">{title}</strong><span className="hidden text-xs sm:block">{detail}</span></span>
    </button>)}
  </div>;
}

function TradeDesk({ brief, workspace, thesis, anomaly, snapshots, onEvidence, onRecord }: { brief: OptionsDecisionBrief | null; workspace: OptionsWorkspacePayload | null; thesis: OptionsDecisionCandidate[]; anomaly: OptionsDecisionCandidate[]; snapshots: OptionHistorySnapshot[]; onEvidence: () => void; onRecord: () => void }) {
  if (!brief) return <Empty text="Loading the current QQQ underwriting decision…" />;
  const presentation = decisionPresentation(brief);
  const candidates = [...thesis, ...anomaly];
  return <div className="space-y-4">
    <section className={`relative overflow-hidden rounded-xl border p-5 sm:p-6 ${verdictClass(presentation.tone)}`}>
      <div className="absolute right-0 top-0 h-32 w-32 translate-x-10 -translate-y-12 rounded-full bg-current opacity-[0.04]" />
      <div className="relative grid gap-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] opacity-70">{presentation.eyebrow}</p>
          <h2 className="mt-2 max-w-3xl text-2xl font-semibold tracking-tight sm:text-3xl">{presentation.title}</h2>
          <p className="mt-3 max-w-3xl text-sm leading-6 opacity-80">{presentation.detail}</p>
          <div className="mt-4 flex flex-wrap gap-2 text-xs">
            <Pill icon={<Activity className="size-3.5" />}>Evidence {formatDateTime(brief.as_of)}</Pill>
            <Pill icon={<ShieldCheck className="size-3.5" />}>Paper only</Pill>
            <Pill icon={<FlaskConical className="size-3.5" />}>{workspace?.active_revision ?? "r3"}</Pill>
          </div>
        </div>
        {presentation.action === "thesis"
          ? <Link to="/thesis-monitor?symbol=QQQ" className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-foreground px-4 text-sm font-medium text-background hover:opacity-90">{presentation.actionLabel}<ArrowRight className="size-4" /></Link>
          : <button type="button" onClick={presentation.action === "record" ? onRecord : onEvidence} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-foreground px-4 text-sm font-medium text-background hover:opacity-90">{presentation.actionLabel}<ArrowRight className="size-4" /></button>}
      </div>
    </section>

    <section className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
      <div className="rounded-xl border border-border bg-card p-4 sm:p-5">
        <SectionTitle icon={<BarChart3 className="size-4" />} title="Decision funnel" detail="Latest complete generation; counts are evidence volume, not trade conviction." />
        <div className="mt-5 grid gap-2 sm:grid-cols-4">
          <FunnelStep label="Complete captures" value={brief.readiness.capture.complete_captures} state="done" />
          <FunnelStep label="Contracts scored" value={summaryNumber(brief, "relative_values")} state="done" />
          <FunnelStep label="Model groups fit" value={brief.readiness.analysis.succeeded_groups} state={brief.readiness.analysis.succeeded_groups ? "done" : "open"} />
          <FunnelStep label="Trade candidates" value={summaryNumber(brief, "decision_candidates") || candidates.length} state={candidates.length ? "active" : "open"} />
        </div>
        <p className="mt-4 text-xs leading-5 text-muted-foreground">The model evaluated {summaryNumber(brief, "relative_values").toLocaleString()} relative-value rows. Zero candidates is a valid “no trade” result, not missing data.</p>
      </div>
      <GateStack brief={brief} />
    </section>

    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(320px,0.72fr)]">
      <BlockerBoard brief={brief} />
      <NextMove brief={brief} snapshots={snapshots} onEvidence={onEvidence} />
    </section>

    {candidates.length ? <CandidateBoard thesis={thesis} anomaly={anomaly} state={brief.state} /> : null}
  </div>;
}

function GateStack({ brief }: { brief: OptionsDecisionBrief }) {
  const { readiness } = brief;
  const neutralThesis = readiness.thesis.present && readiness.thesis.blocker === "thesis_direction_required";
  const gates = [
    { label: "Capture", value: readiness.capture.capture_state === "complete" ? "Complete" : "Open", detail: `${readiness.capture.complete_captures} complete generations`, done: readiness.capture.capture_state === "complete" },
    { label: "Model fit", value: `${readiness.analysis.succeeded_groups}/${readiness.analysis.fit_attempts}`, detail: `${readiness.analysis.solver_failures} solver failures`, done: readiness.analysis.fit_attempts > 0 && readiness.analysis.succeeded_groups === readiness.analysis.fit_attempts },
    {
      label: "QQQ thesis",
      value: readiness.thesis.eligible ? readiness.thesis.revision ?? "Eligible" : neutralThesis ? "Neutral" : "Pending",
      detail: readiness.thesis.invalidation ?? "Automatic directional view and invalidation pending",
      done: readiness.thesis.eligible,
    },
    { label: "Canary", value: `${readiness.canary.qualified_regular_sessions}/${readiness.canary.required_regular_sessions}`, detail: readiness.canary.qualified_regular_sessions >= readiness.canary.required_regular_sessions ? "Reliability gate complete" : "Qualified regular sessions", done: readiness.canary.qualified_regular_sessions >= readiness.canary.required_regular_sessions },
  ];
  return <div className="rounded-xl border border-border bg-card p-4 sm:p-5">
    <SectionTitle icon={<ShieldCheck className="size-4" />} title="Underwriting gates" detail="Every gate must clear before PAPER_READY." />
    <div className="mt-4 divide-y divide-border">
      {gates.map((gate) => <div key={gate.label} className="grid grid-cols-[auto_1fr_auto] items-center gap-3 py-3 first:pt-0 last:pb-0">
        <span className={`grid size-7 place-items-center rounded-full ${gate.done ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" : "bg-amber-500/15 text-amber-700 dark:text-amber-300"}`}>{gate.done ? <Check className="size-4" /> : <CircleDashed className="size-4" />}</span>
        <span><strong className="block text-sm font-medium">{gate.label}</strong><span className="text-xs text-muted-foreground">{gate.detail}</span></span>
        <span className="text-sm font-semibold tabular-nums">{gate.value}</span>
      </div>)}
    </div>
  </div>;
}

function BlockerBoard({ brief }: { brief: OptionsDecisionBrief }) {
  const rows = brief.readiness.top_blockers.slice(0, 6);
  const max = Math.max(...rows.map((row) => row.count), 1);
  return <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
    <SectionTitle icon={<X className="size-4" />} title="Why the chain produced no trade" detail="Top evidence-quality failures. Counts overlap because one contract can fail several gates." />
    {rows.length ? <div className="mt-4 space-y-4">{rows.map((row) => {
      const copy = blockerCopy(row.blocker);
      return <div key={row.blocker}>
        <div className="flex items-baseline justify-between gap-3"><strong className="text-sm font-medium">{copy.label}</strong><span className="text-sm tabular-nums text-muted-foreground">{row.count.toLocaleString()}</span></div>
        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-amber-500/70" style={{ width: `${Math.max(4, (row.count / max) * 100)}%` }} /></div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{copy.detail}</p>
      </div>;
    })}</div> : <Empty text="No active evidence blockers were reported." />}
  </section>;
}

function NextMove({ brief, snapshots, onEvidence }: { brief: OptionsDecisionBrief; snapshots: OptionHistorySnapshot[]; onEvidence: () => void }) {
  const thesisMissing = !brief.readiness.thesis.eligible;
  const neutralThesis = brief.readiness.thesis.present && brief.readiness.thesis.blocker === "thesis_direction_required";
  return <aside className="rounded-xl border border-border bg-card p-4 sm:p-5">
    <SectionTitle icon={<ArrowRight className="size-4" />} title="Expert workflow" detail="Resolve the first open decision gate; do not browse tabs hoping a trade appears." />
    <ol className="mt-4 space-y-4">
      <WorkflowStep
        number="01"
        title={neutralThesis ? "Wait for directional evidence" : thesisMissing ? "Run the automatic thesis monitor" : "Thesis is current"}
        detail={neutralThesis
          ? "The current automatic thesis is neutral. Do not force a directional options trade; the monitor will reassess new independent evidence."
          : thesisMissing
            ? "Canonical Thesis Monitor owns the QQQ direction, horizon, catalyst, and invalidation."
            : brief.readiness.thesis.invalidation ?? "A valid thesis revision is attached."}
        done={!thesisMissing}
      />
      <WorkflowStep number="02" title="Check execution-quality evidence" detail={`${brief.readiness.analysis.eligible_groups} eligible model groups from the latest complete capture. Inspect spread, OI, quote age, and skew.`} done={brief.readiness.analysis.succeeded_groups > 0} />
      <WorkflowStep number="03" title="Wait for a qualified setup" detail="A paper action appears only after thesis, canary, calibration, and conservative re-quote gates all clear." done={brief.state === "PAPER_READY"} />
    </ol>
    <button type="button" onClick={onEvidence} className="mt-5 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-md border border-border px-3 text-sm font-medium hover:bg-muted">Open advanced evidence <span className="text-muted-foreground">({snapshots.length} recent)</span><ArrowRight className="size-4" /></button>
  </aside>;
}

function CandidateBoard({ thesis, anomaly, state }: { thesis: OptionsDecisionCandidate[]; anomaly: OptionsDecisionCandidate[]; state: string }) {
  return <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
    <SectionTitle icon={<Target className="size-4" />} title="Qualified research candidates" detail="Thesis and anomaly lanes are separate research lenses, not one universal ranking." />
    <div className="mt-4 grid gap-4 xl:grid-cols-2">
      <CandidateList title="Thesis-led" rows={thesis} state={state} empty="No thesis-led setup cleared the current gates." />
      <CandidateList title="Anomaly research" rows={anomaly} state={state} empty="No anomaly setup cleared the current gates." />
    </div>
  </section>;
}

function CandidateList({ title, rows, state, empty }: { title: string; rows: OptionsDecisionCandidate[]; state: string; empty: string }) {
  return <div className="rounded-lg border border-border p-3"><h3 className="text-sm font-semibold">{title}</h3>{rows.length ? <div className="mt-2 divide-y divide-border">{rows.map((row) => <Candidate key={row.decision_id} candidate={row} state={state} />)}</div> : <p className="mt-2 text-sm text-muted-foreground">{empty}</p>}</div>;
}

function Candidate({ candidate, state }: { candidate: OptionsDecisionCandidate; state: string }) {
  return <article className="py-3 first:pt-1 last:pb-1">
    <div className="flex items-start justify-between gap-3">
      <div><strong className="text-sm">{sentence(candidate.structure)}</strong><p className="text-xs text-muted-foreground">{candidate.expiration} · {candidate.option_type.toUpperCase()} {money(candidate.strike)}</p></div>
      <StatusBadge tone={tone(state)}>{candidate.paper_state}</StatusBadge>
    </div>
    <dl className="mt-3 grid grid-cols-3 gap-2 text-xs">
      <Metric label="Entry" value={money(candidate.conservative_entry.price)} />
      <Metric label="Max loss" value={money(candidate.one_unit_max_loss)} />
      <Metric label="Modeled edge" value={money(candidate.modeled_net_edge)} />
    </dl>
  </article>;
}

function FunnelStep({ label, value, state }: { label: string; value: number; state: "done" | "active" | "open" }) {
  return <div className={`relative rounded-lg border p-3 ${state === "active" ? "border-emerald-500/40 bg-emerald-500/5" : "border-border bg-muted/30"}`}>
    <span className="text-2xl font-semibold tabular-nums">{value.toLocaleString()}</span>
    <span className="mt-1 block text-xs text-muted-foreground">{label}</span>
  </div>;
}
function WorkflowStep({ number: step, title, detail, done }: { number: string; title: string; detail: string; done: boolean }) {
  return <li className="grid grid-cols-[auto_1fr] gap-3"><span className={`grid size-8 place-items-center rounded-full text-xs font-semibold ${done ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" : "bg-muted text-muted-foreground"}`}>{done ? <Check className="size-4" /> : step}</span><span><strong className="block text-sm font-medium">{title}</strong><span className="mt-0.5 block text-xs leading-5 text-muted-foreground">{detail}</span></span></li>;
}
function SectionTitle({ icon, title, detail }: { icon: ReactNode; title: string; detail: string }) { return <div className="flex items-start gap-3"><span className="mt-0.5 text-primary">{icon}</span><span><h2 className="font-semibold">{title}</h2><p className="mt-0.5 text-xs leading-5 text-muted-foreground">{detail}</p></span></div>; }
function Pill({ icon, children }: { icon: ReactNode; children: ReactNode }) { return <span className="inline-flex items-center gap-1.5 rounded-full border border-current/15 bg-background/40 px-2.5 py-1">{icon}{children}</span>; }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt><dd className="mt-0.5 font-medium tabular-nums">{value}</dd></div>; }
function Empty({ text }: { text: string }) { return <p className="mt-4 rounded-lg border border-dashed border-border p-5 text-sm leading-6 text-muted-foreground">{text}</p>; }
function verdictMetric(brief: OptionsDecisionBrief) { return decisionPresentation(brief).title.replace(/^No trade — /, "No trade · ").replace(/^Wait — /, "Wait · "); }
function verdictClass(toneValue: "good" | "warn" | "info" | "muted") { return toneValue === "good" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-950 dark:text-emerald-50" : toneValue === "warn" ? "border-amber-500/30 bg-amber-500/10 text-amber-950 dark:text-amber-50" : toneValue === "info" ? "border-sky-500/30 bg-sky-500/10 text-sky-950 dark:text-sky-50" : "border-border bg-card text-foreground"; }
function toggleClass(active: boolean) { return `min-h-9 rounded px-3 text-xs font-medium transition-colors ${active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"}`; }
function tone(state?: string): "good" | "warn" | "info" | "muted" { return state === "PAPER_READY" ? "good" : state === "REJECT" ? "warn" : state === "WATCH" ? "info" : "muted"; }
function regularSessionNow() { const now = new Date(); const day = now.getDay(); const minutes = now.getHours() * 60 + now.getMinutes(); return day >= 1 && day <= 5 && minutes >= 9 * 60 + 30 && minutes <= 16 * 60; }
function formatDateTime(value: string | null) { return value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "pending"; }
function money(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "currency", currency: "USD" }); }
function percent(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }); }
function number(value: number | null | undefined, digits = 0) { return value === null || value === undefined ? "—" : value.toFixed(digits); }
function normalizeView(value: string | null): View { return value === "evidence" ? "evidence" : value === "record" || value === "journal" || value === "learn" ? "record" : "desk"; }
function ignoreAbort(cause: unknown, setError: (value: string | null) => void) { if ((cause as { name?: string }).name !== "AbortError") setError(cause instanceof Error ? cause.message : "Unable to load options decision data"); }
function viewKey(event: KeyboardEvent<HTMLButtonElement>, current: View, select: (view: View) => void) { if (!(["ArrowLeft", "ArrowRight", "Home", "End"] as string[]).includes(event.key)) return; event.preventDefault(); const index = VIEWS.indexOf(current); const next = event.key === "Home" ? 0 : event.key === "End" ? VIEWS.length - 1 : (index + (event.key === "ArrowRight" ? 1 : VIEWS.length - 1)) % VIEWS.length; select(VIEWS[next]); document.getElementById((event.currentTarget.parentElement?.querySelectorAll("[role=tab]")[next] as HTMLElement | undefined)?.id ?? "")?.focus(); }
