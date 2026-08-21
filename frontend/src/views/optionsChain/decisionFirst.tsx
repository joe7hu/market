import {
  Activity, ArrowRight, BookOpenCheck, FlaskConical,
  Microscope, ShieldCheck, Target, X,
} from "lucide-react";
import { useEffect, useId, useState, type ComponentType, type KeyboardEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  loadOptionsCandidates, loadOptionsLearningProgress, loadOptionsPaperJournal,
  loadOptionsShadowObservations,
  loadOptionsWorkspace, type OptionsDecisionBrief, type OptionsDecisionCandidate,
  type MarketRegime, type OptionsLearningProgress, type OptionsPaperJournalRow, type OptionsWorkspacePayload, type StrategyRoute,
} from "@/api/options";
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
      subtitle="QQQ-only paper underwriting: is there a QQQ options setup worth taking on paper right now?"
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

    <DecisionGate brief={brief} />

    {candidates.length ? <CandidateBoard thesis={thesis} anomaly={anomaly} state={brief.state} /> : null}
  </div>;
}

function DecisionGate({ brief }: { brief: OptionsDecisionBrief }) {
  const ticket = brief.strongest_candidate?.ticket;
  const ticketBlocker = ticket?.blockers?.[0];
  const failed = brief.readiness.top_blockers[0];
  const truth = brief.decision_truth;
  const blocker = ticketBlocker ?? failed?.blocker ?? truth?.primary_blocker ?? truth?.blockers?.[0];
  const incomplete = brief.state !== "PAPER_READY"
    || brief.mode === "disabled"
    || brief.readiness.capture.capture_state !== "complete"
    || brief.readiness.analysis.eligible_groups <= 0
    || !brief.readiness.thesis.eligible
    || brief.readiness.canary.qualified_regular_sessions < brief.readiness.canary.required_regular_sessions
    || !ticket
    || Boolean(ticket.blockers?.length)
    || truth?.route_verdict === "NO_TRADE"
    || truth?.readiness_state === "incomplete"
    || truth?.execution_state === "DISABLED";
  const copy = blocker ? blockerCopy(blocker) : null;
  return <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
    <SectionTitle
      icon={blocker || incomplete ? <X className="size-4" /> : <ShieldCheck className="size-4" />}
      title={copy?.label ?? (incomplete ? "Decision gates incomplete" : "Decision gates are clear")}
      detail={copy?.detail ?? (incomplete ? "QQQ-only paper underwriting remains blocked until data, thesis, route, and execution gates are complete." : "All current QQQ paper-underwriting gates are complete.")}
    />
    <div className="mt-4 rounded-lg border border-border bg-background p-3">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Required next action</p>
      <p className="mt-1 text-sm font-medium">{ticket?.required_next_action ?? brief.readiness.next_required_action}</p>
    </div>
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
  const ticket = candidate.ticket;
  return <article className="py-3 first:pt-1 last:pb-1">
    <div className="flex items-start justify-between gap-3">
      <div><strong className="text-sm">{sentence(candidate.structure)}</strong><p className="text-xs text-muted-foreground">{candidate.expiration} · {candidate.option_type.toUpperCase()} {money(candidate.strike)}</p></div>
      <StatusBadge tone={tone(state)}>{candidate.paper_state}</StatusBadge>
    </div>
    <dl className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
      <Metric label="Entry" value={money(candidate.conservative_entry.price)} />
      <Metric label="Quantity" value={ticket ? String(ticket.risk.recommended_quantity) : "0"} />
      <Metric label="Total risk" value={money(ticket?.risk.total_risk)} />
      <Metric label="Target / stop" value={ticket ? `${money(ticket.exits.profit_price)} / ${money(ticket.exits.loss_price)}` : "—"} />
    </dl>
    <RouteContext route={candidate.strategy_route} regime={candidate.market_regime} />
    {ticket ? <div className="mt-3 space-y-1 text-xs text-muted-foreground">
      {ticket.legs.map((leg) => <p key={`${leg.contract_id}-${leg.side}`}>{leg.side.toUpperCase()} {money(leg.strike)} {leg.option_type.toUpperCase()} · {money(leg.bid)} × {money(leg.ask)} · {leg.bid_size ?? "—"} / {leg.ask_size ?? "—"} · age {number(leg.quote_age_seconds, 0)}s</p>)}
      <p>Time exit {ticket.exits.time_exit_dte} DTE · Invalidation: {ticket.exits.thesis_invalidation ?? "required before READY"}</p>
      {ticket.blockers.length ? <p className="font-medium text-amber-700 dark:text-amber-300">NO TRADE — research only · {ticket.required_next_action}</p> : null}
    </div> : null}
  </article>;
}

export function routeContextFacts(route: StrategyRoute, regime: MarketRegime) {
  return {
    selected: route.selected_structure,
    shadow: route.shadow !== false,
    trend: route.trend_state || regime.trend_state,
    trendConfidence: route.trend_confidence ?? regime.trend_confidence,
    volatility: route.volatility_state || regime.volatility_state,
    breadth: regime.breadth_state,
    blockers: route.route_blockers ?? [],
  };
}

function RouteContext({ route, regime }: { route: StrategyRoute; regime: MarketRegime }) {
  const facts = routeContextFacts(route, regime);
  const context = [
    facts.trend && `Trend ${sentence(facts.trend)}`,
    facts.trendConfidence !== null && facts.trendConfidence !== undefined && `confidence ${percent(facts.trendConfidence)}`,
    facts.volatility && `vol ${sentence(facts.volatility)}`,
    facts.breadth && `breadth ${sentence(facts.breadth)}`,
  ].filter(Boolean);
  return <div className="mt-3 min-w-0 rounded-md border border-border/70 bg-muted/40 p-2.5 text-xs leading-5">
    <div className="flex flex-wrap items-center gap-1.5"><StatusBadge tone="info">{facts.shadow ? "Shadow route" : "Route context"}</StatusBadge><StatusBadge tone="warn">Paper-only · not authorized</StatusBadge></div>
    <p className="mt-1 break-words text-muted-foreground"><span className="font-medium text-foreground">{sentence(facts.selected)}</span>{context.length ? ` · ${context.join(" · ")}` : " · Market route data pending"}</p>
    {facts.blockers.length ? <p className="mt-1 break-words text-amber-700 dark:text-amber-300">Route blocker: {facts.blockers[0]}</p> : null}
  </div>;
}

function SectionTitle({ icon, title, detail }: { icon: ReactNode; title: string; detail: string }) { return <div className="flex items-start gap-3"><span className="mt-0.5 text-primary">{icon}</span><span><h2 className="font-semibold">{title}</h2><p className="mt-0.5 text-xs leading-5 text-muted-foreground">{detail}</p></span></div>; }
function Pill({ icon, children }: { icon: ReactNode; children: ReactNode }) { return <span className="inline-flex items-center gap-1.5 rounded-full border border-current/15 bg-background/40 px-2.5 py-1">{icon}{children}</span>; }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt><dd className="mt-0.5 font-medium tabular-nums">{value}</dd></div>; }
function Empty({ text }: { text: string }) { return <p className="mt-4 rounded-lg border border-dashed border-border p-5 text-sm leading-6 text-muted-foreground">{text}</p>; }
function verdictMetric(brief: OptionsDecisionBrief) { return decisionPresentation(brief).title.replace(/^No trade — /, "No trade · ").replace(/^Wait — /, "Wait · "); }
function verdictClass(toneValue: "good" | "warn" | "info" | "muted") { return toneValue === "good" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-950 dark:text-emerald-50" : toneValue === "warn" ? "border-amber-500/30 bg-amber-500/10 text-amber-950 dark:text-amber-50" : toneValue === "info" ? "border-sky-500/30 bg-sky-500/10 text-sky-950 dark:text-sky-50" : "border-border bg-card text-foreground"; }
function tone(state?: string): "good" | "warn" | "info" | "muted" { return state === "PAPER_READY" ? "good" : state === "REJECT" ? "warn" : state === "WATCH" ? "info" : "muted"; }
function regularSessionNow() { const now = new Date(); const day = now.getDay(); const minutes = now.getHours() * 60 + now.getMinutes(); return day >= 1 && day <= 5 && minutes >= 9 * 60 + 30 && minutes <= 16 * 60; }
function formatDateTime(value: string | null | undefined) { return value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "pending"; }
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
