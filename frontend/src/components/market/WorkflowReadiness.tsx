import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { loadWorkstationStatus, type WorkstationStatus } from "@/api/workstation";
import { startRefreshJob } from "@/api/panel";
import { Button } from "@/components/ui/button";
import { humanize, dateTime } from "@/presentation/labels";

export type ReadinessView = "today" | "market" | "paper" | "research" | "health";
const WORKER_NAMES: Record<string, string> = {
  refresh_assessment_inputs: "Assessment quotes", refresh_symbol_features: "Monitored trend features", run_continuous_advisor: "Forecast issuance",
  update_broker_account: "Account facts", update_market_data: "Market data", update_market_valuations: "Valuations", refresh_market_publication: "Market publication",
  refresh_decision_models: "Decision publication", process_options_paper_orders: "Paper manager",
  refresh_paper_quotes: "Active-contract quotes", run_option_paper_experiments: "Candidate research",
  run_stock_alpha_walk_forward: "Historical validation", refresh_symbol_decision_outcomes: "Outcome resolution",
  run_continuous_advisor_replay: "Forecast resolution",
};
const JOBS: Record<ReadinessView, string[]> = {
  today: ["process_options_paper_orders", "refresh_decision_models"],
  market: ["update_market_data", "update_market_valuations", "refresh_market_publication"],
  paper: ["process_options_paper_orders", "refresh_paper_quotes", "run_option_paper_experiments"],
  research: ["run_option_paper_experiments", "run_stock_alpha_walk_forward", "refresh_symbol_decision_outcomes", "run_continuous_advisor_replay"],
  health: Object.keys(WORKER_NAMES),
};
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const array = (value: unknown): Array<Record<string, unknown>> => Array.isArray(value) ? value.map(object) : [];
function count(value: unknown): string { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value.toLocaleString() : "Not available"; }
export function operationCount(population: unknown, statuses: string[]): string {
  const record = object(population);
  if (record.status !== "available") return "Not available";
  const counts = object(record.counts);
  const values = statuses.map(key => counts[key] ?? 0);
  if (values.some(value => typeof value !== "number" || !Number.isFinite(value) || value < 0)) return "Not available";
  return count((values as number[]).reduce((a, b) => a + b, 0));
}
export function entrySwitchLabel(value: unknown): string {
  return value === true ? "Options entry master switch enabled; lane, qualification and risk gates still apply" :
    value === false ? "Options entry master switch disabled; existing risk is still managed" :
    "Options entry permission could not be read; no permission is assumed";
}
function stateClass(state: unknown): string {
  return ["failed", "overdue", "unavailable"].includes(String(state)) ? "text-destructive" :
    ["available", "succeeded", "running"].includes(String(state)) ? "text-primary" : "text-amber-700 dark:text-amber-300";
}

export function WorkflowReadiness({ view = "today", onDataChanged }: { view?: ReadinessView; onDataChanged?: () => void }) {
  const [snapshot, setSnapshot] = useState<WorkstationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [queued, setQueued] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const request = useRef<AbortController | null>(null);
  const acceptedVersion = useRef<string | null>(null);
  const mounted = useRef(false);
  const reload = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    void loadWorkstationStatus(controller.signal).then(value => {
      if (!controller.signal.aborted) { setSnapshot(value); setError(null); }
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Workflow state could not be read.");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
  }, []);
  useEffect(() => {
    mounted.current = true;
    reload();
    const timer = setInterval(() => { if (document.visibilityState === "visible") reload(); }, 30000);
    return () => { mounted.current = false; clearInterval(timer); request.current?.abort(); };
  }, [reload]);
  async function run(job: string) {
    setStarting(true); setQueued(null);
    try {
      await startRefreshJob(job);
      if (mounted.current) { setQueued(`${WORKER_NAMES[job] ?? humanize(job)} requested. Completion is not yet verified; watch its next recorded run below.`); reload(); }
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "The operation could not be queued."); }
    finally { if (mounted.current) setStarting(false); }
  }
  const paper = object(snapshot?.paper), observations = object(snapshot?.observations), market = object(snapshot?.market);
  const workers = snapshot?.workers?.filter(worker => view === "health" || JOBS[view].includes(worker.job)) ?? [];
  const decisionService = object(snapshot?.decision_service), forecasts = object(snapshot?.forecasts);
  const blockers = snapshot?.blockers?.filter(item => view === "today" || view === "health" || item.capability === "Trading signals" || item.capability === "Forecast learning" ||
    view === "market" && item.capability === "Market" || view === "paper" && item.capability === "Paper execution" ||
    view === "research" && item.capability === "Strategy learning") ?? [];
  useEffect(() => {
    if (!snapshot) return;
    const version = view === "market" ? String(snapshot.market?.publication_id ?? "") :
      view === "research" ? JSON.stringify((snapshot.evaluations ?? []).map(row => [row.strategy_revision_id, row.evaluation_type, row.verdict, row.evaluated_at])) :
      String((snapshot.workers ?? []).find(worker => worker.job === "refresh_decision_models")?.last_success_at ?? "");
    if (acceptedVersion.current !== null && acceptedVersion.current !== version) onDataChanged?.();
    acceptedVersion.current = version;
  }, [snapshot, view, onDataChanged]);
  const reasons = array(observations.reason_counts);
  const compact = view === "today" || view === "market" || view === "research";
  return <section className="rounded-xl border border-border bg-card p-4" aria-label="Workflow readiness">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold">{view === "paper" ? "Paper execution — what is happening now" : view === "research" ? "Evidence collection health" : "Workflow readiness"}</h2>
      <p className="mt-1 text-xs text-muted-foreground">{snapshot ? `Recorded ${dateTime(snapshot.as_of)} · ${snapshot.market_session === "regular" ? "US regular session · crypto 24/7" : `US market closed · crypto 24/7 · next US session ${dateTime(snapshot.next_session_at)}`}` : "Loading recorded workflow state…"}</p></div>
      <Button variant="outline" size="sm" disabled={loading} onClick={reload}>{loading ? "Checking…" : "Check status"}</Button></div>
    {error ? <p role="alert" className="mt-3 text-sm text-destructive">{snapshot ? "Retained status may be stale. " : ""}{error}</p> : null}
    {snapshot?.failed_reads?.length ? <p role="alert" className="mt-3 text-sm text-destructive">Some workflow evidence could not be read: {snapshot.failed_reads.map(value => humanize(value)).join(", ")}. Unread populations are not zero.</p> : null}
    {snapshot ? <>
      <div className={`mt-3 rounded-lg border p-3 ${snapshot.status === "available" ? "border-border" : "border-destructive/40"}`} role={snapshot.status === "available" ? "status" : "alert"}>
        <strong>{snapshot.status === "available" ? "Decision service operational" : "SERVICE DEGRADED — required workflow checks failed"}</strong>
        <p className="mt-1 text-sm">Instrument services ready: {count(decisionService.ready_count)} / {count(decisionService.monitored_count)} monitored instruments. Database connectivity alone is not health.</p>
      </div>
      {view === "research" || view === "health" ? <div className="mt-3 rounded-lg border p-3 text-sm"><strong>Forecast lifecycle</strong><p className="mt-1">{count(forecasts.issued)} issued · {count(forecasts.resolved)} resolved · {count(forecasts.pending)} pending · {count(forecasts.overdue)} overdue</p>{forecasts.next_maturity_at ? <p className="mt-1 text-xs">Next outcome window ends {dateTime(String(forecasts.next_maturity_at))}.</p> : null}<p className="mt-1 text-xs text-muted-foreground">{String(forecasts.basis ?? "Forecast producer/resolver state could not be read.")}</p></div> : null}
      {view === "health" && array(decisionService.instruments).length ? <details className="mt-3" open><summary className="cursor-pointer text-sm font-medium">Instrument-by-instrument service checks</summary><div className="mt-2 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">Instrument</th><th className="p-2">Service</th><th className="p-2">Quote clock</th><th className="p-2">Completed feature session</th><th className="p-2">Failing producer</th></tr></thead><tbody>{array(decisionService.instruments).map(item => <tr key={String(item.symbol)} className="border-t"><td className="p-2 font-medium">{String(item.symbol)}</td><td className="p-2">{humanize(item.status)}</td><td className="p-2">{humanize(item.quote_state)} · {dateTime(String(item.quote_observed_at ?? ""))}</td><td className="p-2">{String(item.feature_session ?? "Producer failed")}</td><td className="p-2">{Array.isArray(item.owner_jobs) && item.owner_jobs.length ? item.owner_jobs.map(String).join(", ") : "All checks passed"}</td></tr>)}</tbody></table></div></details> : null}
      {view !== "market" && view !== "research" ? <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <div className="rounded-lg bg-muted/40 p-3"><p className="text-xs text-muted-foreground">Market publication</p><p className={`mt-1 font-semibold ${stateClass(market.status)}`}>{humanize(market.status, "Not published")}</p>{market.published_at ? <p className="mt-1 text-xs text-muted-foreground">{dateTime(String(market.published_at))}</p> : null}</div>
        <div className="rounded-lg bg-muted/40 p-3"><p className="text-xs text-muted-foreground">Funded paper book · open / exited orders</p><p className="mt-1 font-semibold">{operationCount(paper, ["entered", "partial_exited"])} / {operationCount(paper, ["exited"])}</p><p className="mt-1 text-xs text-muted-foreground">{entrySwitchLabel(paper.entries_enabled)}</p></div>
        <div className="rounded-lg bg-muted/40 p-3"><p className="text-xs text-muted-foreground">Research observations · open / closed</p><p className="mt-1 font-semibold">{operationCount(observations, ["entered"])} / {operationCount(observations, ["closed"])}</p><p className="mt-1 text-xs text-muted-foreground">Separate from funded paper-account P&amp;L</p></div>
      </div> : <p className={`mt-3 text-sm ${stateClass(market.status)}`}>Market publication: {humanize(market.status)} · {market.published_at ? dateTime(String(market.published_at)) : "No recorded publication"}</p>}
      {view === "market" || view === "health" ? <div className="mt-3 text-xs text-muted-foreground"><p>{String(market.reason ?? "")}</p><p className="mt-1">Input cutoff: {market.input_cutoff ? dateTime(String(market.input_cutoff)) : "Not recorded"} · Valuation context: {humanize(market.valuation_status, "Not available")}</p>{array(market.dimensions).map(dimension => <p className="mt-1" key={String(dimension.name)}>{String(dimension.name)}: {humanize(dimension.status)} · members {count(dimension.available_members)} / {count(dimension.eligible_members)}{Array.isArray(dimension.blockers) && dimension.blockers.length ? " · inspect coverage" : ""}</p>)}</div> : null}
      {blockers.length ? <div className="mt-3 space-y-2">{(view === "health" ? blockers : blockers.slice(0, 3)).map((item, i) => <div key={`${item.capability}-${i}`} className="rounded-md border border-amber-300/60 p-3 text-sm"><strong>{item.capability}: </strong>{humanize(item.reason)}<p className="mt-1 text-xs text-muted-foreground">{item.action} {view === "health" && item.job ? <button className="mr-2 underline" disabled={starting} onClick={() => void run(item.job!)}>Retry {WORKER_NAMES[item.job] ?? item.job}</button> : null}<Link className="text-primary underline" to={item.href}>Review →</Link></p></div>)}</div> : null}
      <details className="mt-3" open={!compact}><summary className="cursor-pointer text-xs text-muted-foreground">Worker timing and source details · {workers.length} stages</summary>
      <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-muted-foreground"><tr><th className="p-2">Stage</th><th className="p-2">State</th><th className="p-2">Source / downstream</th><th className="p-2">Last success</th><th className="p-2">Next expected*</th></tr></thead><tbody>{workers.map(worker => <tr key={worker.job} className="border-t border-border"><td className="p-2 font-medium">{WORKER_NAMES[worker.job] ?? humanize(worker.job)}</td><td className={`p-2 ${stateClass(worker.status)}`} title={worker.reason}>{humanize(worker.status)}</td><td className="p-2">{worker.source_status ? humanize(worker.source_status) : "Not recorded"} / {worker.downstream_status ? humanize(worker.downstream_status) : "Not recorded"}</td><td className="p-2">{worker.last_success_at ? dateTime(worker.last_success_at) : "No recorded success"}</td><td className="p-2">{worker.next_expected_at ? dateTime(worker.next_expected_at) : worker.status === "disabled" ? "Not scheduled" : "After scheduler dispatch"}</td></tr>)}</tbody></table></div>
      <p className="mt-2 text-[11px] text-muted-foreground">*Expected from cadence, not a promise of dispatch. Only venue-bound equity work pauses when closed; crypto quotes and due outcome resolution do not. Collector success does not establish a tradable decision.</p></details>
      {view === "paper" || view === "health" ? <details className="mt-4" open={view === "paper"}><summary className="cursor-pointer text-sm font-medium">Execution funnel and unresolved reasons</summary><div className="mt-3 grid gap-4 lg:grid-cols-2">
        <div><p className="mb-2 text-xs text-muted-foreground">Recorded research observations, full population</p>{observations.status !== "available" ? <p>Observation counts could not be read.</p> : reasons.length ? reasons.slice(0, 8).map((item, i) => <div key={i} className="flex items-start justify-between gap-3 border-b py-2 text-xs"><span><strong>{humanize(item.status)}</strong> · {humanize(item.reason, "No reason recorded")}</span><span className="tabular-nums">{count(item.count)}</span></div>) : <p className="text-sm">No prospective observations have been recorded.</p>}</div>
        <div><p className="mb-2 text-xs text-muted-foreground">Next pending/open funded orders · at most 20 shown</p>{paper.waiting_status === "unavailable" ? <p role="alert" className="text-sm">Pending-order evidence could not be read.</p> : array(paper.waiting).length ? array(paper.waiting).map(item => <Link key={String(item.paper_order_id)} className="block border-b py-2 text-xs hover:underline" to={`/portfolio/paper/trades/${encodeURIComponent(String(item.paper_order_id))}`}><strong>{String(item.symbol)}</strong> · {humanize(item.status)}<p className="mt-1">{humanize(item.reason, "Waiting for a later admissible quote or exit condition")}</p><p className="text-muted-foreground">Last checked {dateTime(String(object(item.management).last_checked_at ?? ""))}</p></Link>) : <p className="text-sm">{paper.status === "available" ? "No pending/open funded orders in this snapshot." : "Funded-order state could not be read."}</p>}</div>
      </div></details> : null}
      <div className="mt-4 flex flex-wrap items-center gap-3">{view === "market" || view === "health" ? <Button size="sm" variant="outline" disabled={starting} onClick={() => void run("refresh_market_publication")}>Rebuild Market snapshot</Button> : null}{view === "paper" ? <Button size="sm" variant="outline" disabled={starting} onClick={() => void run("process_options_paper_orders")}>Run paper management cycle</Button> : null}<Link to="/health" className="text-xs text-primary hover:underline">Inspect workflow details →</Link></div>
    </> : null}
    {queued ? <p role="status" className="mt-3 text-sm">{queued}</p> : null}
  </section>;
}
