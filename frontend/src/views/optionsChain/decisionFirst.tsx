import {
  Activity, ArrowRight, BookOpenCheck, Check, CircleDashed, FlaskConical,
  Microscope, ShieldCheck, Target, X,
} from "lucide-react";
import { useEffect, useId, useState, type ComponentType, type KeyboardEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  loadOptionsCandidates, loadOptionsLearningProgress, loadOptionsPaperJournal,
  loadOptionsShadowObservations,
  loadOptionsWorkspace, type OptionsDecisionBrief, type OptionsDecisionCandidate,
  type OptionsLearningProgress, type OptionsPaperJournalRow, type OptionsWorkspacePayload,
} from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { WorkspacePage } from "../workspacePage";
import { blockerCopy, decisionPresentation, sentence } from "./decisionDesk";
import { JournalDesk } from "./journalDesk";

type View = "desk" | "evidence" | "record";
const VIEWS: View[] = ["desk", "evidence", "record"];
type EvidenceComponent = ComponentType<{ embedded?: boolean }>;

export function DecisionFirstOptionsChainPage({ EvidenceWorkspace }: { EvidenceWorkspace: EvidenceComponent }) {
  const [search, setSearch] = useSearchParams();
  const view = normalizeView(search.get("tab"));
  const [brief, setBrief] = useState<OptionsDecisionBrief | null>(null);
  const [workspace, setWorkspace] = useState<OptionsWorkspacePayload | null>(null);
  const [thesisCandidates, setThesisCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [anomalyCandidates, setAnomalyCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [journal, setJournal] = useState<OptionsPaperJournalRow[]>([]);
  const [journalCount, setJournalCount] = useState(0);
  const [shadow, setShadow] = useState<OptionsPaperJournalRow[]>([]);
  const [shadowCount, setShadowCount] = useState(0);
  const [learning, setLearning] = useState<OptionsLearningProgress[]>([]);
  const [error, setError] = useState<string | null>(null);
  const panelId = useId();
  const select = (next: View) => {
    setSearch(optionsViewSearch(search, next));
  };

  useEffect(() => {
    if (search.get("symbol") === "QQQ" && !search.has("lane")) return;
    setSearch(optionsViewSearch(search, view), { replace: true });
  }, [search, setSearch, view]);

  useEffect(() => {
    let controller: AbortController | null = null;
    const refresh = () => {
      controller?.abort();
      controller = new AbortController();
      void loadOptionsWorkspace("QQQ", "thesis", controller.signal)
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
  }, []);

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
      actions={<DeskActions mode={brief?.mode} />}
    >
      {error ? <p role="alert" className="rounded border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
      <ViewNav active={view} panelId={panelId} onSelect={select} />
      <div id={`${panelId}-panel`} role="tabpanel" aria-labelledby={`${panelId}-${view}`}>
        {view === "desk" ? <TradeDesk brief={brief} workspace={workspace} thesis={thesisCandidates} anomaly={anomalyCandidates} onEvidence={() => select("evidence")} onRecord={() => select("record")} /> : null}
        {view === "evidence" ? <EvidenceWorkspace embedded /> : null}
        {view === "record" ? <JournalDesk
          brief={brief}
          journal={journal}
          journalCount={journalCount}
          shadow={shadow}
          shadowCount={shadowCount}
          legacyShadowCount={workspace?.tab_counts.legacy_shadow_observations ?? 0}
          learning={learning}
        /> : null}
      </div>
    </WorkspacePage>
  );
}

function DeskActions({ mode }: { mode?: string }) {
  return <StatusBadge tone={mode === "paper" ? "warn" : "info"}>{mode ?? "shadow"} only</StatusBadge>;
}

function ViewNav({ active, panelId, onSelect }: { active: View; panelId: string; onSelect: (view: View) => void }) {
  const specs: Array<[View, ReactNode, string, string]> = [
    ["desk", <Target className="size-4" />, "Trade desk", "Verdict, gates, and candidates"],
    ["evidence", <Microscope className="size-4" />, "Market evidence", "Chain, volatility, and history"],
    ["record", <BookOpenCheck className="size-4" />, "Learning log", "Forecasts, paper outcomes, and proof"],
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

function TradeDesk({ brief, workspace, thesis, anomaly, onEvidence, onRecord }: { brief: OptionsDecisionBrief | null; workspace: OptionsWorkspacePayload | null; thesis: OptionsDecisionCandidate[]; anomaly: OptionsDecisionCandidate[]; onEvidence: () => void; onRecord: () => void }) {
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

    <section className="grid gap-4 xl:grid-cols-[minmax(320px,0.82fr)_minmax(0,1.18fr)]">
      <GateStack brief={brief} />
      <BlockerBoard brief={brief} />
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
  const rows = brief.readiness.top_blockers.slice(0, 3);
  const max = Math.max(...rows.map((row) => row.count), 1);
  return <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
    <SectionTitle icon={<X className="size-4" />} title="Why the chain produced no trade" detail="The three largest evidence-quality failures. Counts overlap." />
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

function SectionTitle({ icon, title, detail }: { icon: ReactNode; title: string; detail: string }) { return <div className="flex items-start gap-3"><span className="mt-0.5 text-primary">{icon}</span><span><h2 className="font-semibold">{title}</h2><p className="mt-0.5 text-xs leading-5 text-muted-foreground">{detail}</p></span></div>; }
function Pill({ icon, children }: { icon: ReactNode; children: ReactNode }) { return <span className="inline-flex items-center gap-1.5 rounded-full border border-current/15 bg-background/40 px-2.5 py-1">{icon}{children}</span>; }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt><dd className="mt-0.5 font-medium tabular-nums">{value}</dd></div>; }
function Empty({ text }: { text: string }) { return <p className="mt-4 rounded-lg border border-dashed border-border p-5 text-sm leading-6 text-muted-foreground">{text}</p>; }
function verdictMetric(brief: OptionsDecisionBrief) { return decisionPresentation(brief).title.replace(/^No trade — /, "No trade · ").replace(/^Wait — /, "Wait · "); }
function verdictClass(toneValue: "good" | "warn" | "info" | "muted") { return toneValue === "good" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-950 dark:text-emerald-50" : toneValue === "warn" ? "border-amber-500/30 bg-amber-500/10 text-amber-950 dark:text-amber-50" : toneValue === "info" ? "border-sky-500/30 bg-sky-500/10 text-sky-950 dark:text-sky-50" : "border-border bg-card text-foreground"; }
function tone(state?: string): "good" | "warn" | "info" | "muted" { return state === "PAPER_READY" ? "good" : state === "REJECT" ? "warn" : state === "WATCH" ? "info" : "muted"; }
function regularSessionNow() { const now = new Date(); const day = now.getDay(); const minutes = now.getHours() * 60 + now.getMinutes(); return day >= 1 && day <= 5 && minutes >= 9 * 60 + 30 && minutes <= 16 * 60; }
function formatDateTime(value: string | null) { return value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "pending"; }
function money(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "currency", currency: "USD" }); }
function percent(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }); }
function number(value: number | null | undefined, digits = 0) { return value === null || value === undefined ? "—" : value.toFixed(digits); }
function normalizeView(value: string | null): View { return value === "evidence" ? "evidence" : value === "record" || value === "journal" || value === "learn" ? "record" : "desk"; }
export function optionsViewSearch(search: URLSearchParams, view: View): URLSearchParams {
  const values = new URLSearchParams(search);
  values.set("symbol", "QQQ");
  values.set("tab", view);
  values.delete("lane");
  return values;
}
function ignoreAbort(cause: unknown, setError: (value: string | null) => void) { if ((cause as { name?: string }).name !== "AbortError") setError(cause instanceof Error ? cause.message : "Unable to load options decision data"); }
function viewKey(event: KeyboardEvent<HTMLButtonElement>, current: View, select: (view: View) => void) { if (!(["ArrowLeft", "ArrowRight", "Home", "End"] as string[]).includes(event.key)) return; event.preventDefault(); const index = VIEWS.indexOf(current); const next = event.key === "Home" ? 0 : event.key === "End" ? VIEWS.length - 1 : (index + (event.key === "ArrowRight" ? 1 : VIEWS.length - 1)) % VIEWS.length; select(VIEWS[next]); document.getElementById((event.currentTarget.parentElement?.querySelectorAll("[role=tab]")[next] as HTMLElement | undefined)?.id ?? "")?.focus(); }
