import { AlertTriangle, CheckCircle2, Clock3, FlaskConical, MinusCircle } from "lucide-react";
import type { DecisionFunnel, RefreshJob } from "@/api/panel";
import { alphaEvaluation, blockerLabel, funnelStageState } from "./qualification";

const STATE = {
  passed: { label: "Passed", Icon: CheckCircle2, className: "text-emerald-600 dark:text-emerald-400" },
  qualifying: { label: "Qualifying", Icon: FlaskConical, className: "text-blue-600 dark:text-blue-400" },
  blocked: { label: "Blocked", Icon: AlertTriangle, className: "text-amber-600 dark:text-amber-400" },
  not_reached: { label: "Not reached", Icon: MinusCircle, className: "text-muted-foreground" },
};

export function DecisionFunnelPanel({ funnel, jobs = [], loading = false, error = null }: {
  funnel: DecisionFunnel | null; jobs?: RefreshJob[]; loading?: boolean; error?: string | null;
}) {
  const alpha = alphaEvaluation(jobs);
  return <section className="overflow-hidden rounded-xl border border-border bg-card" aria-labelledby="decision-funnel-title">
    <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-4">
      <div><h2 id="decision-funnel-title" className="font-semibold">Decision funnel</h2><p className="mt-1 text-xs text-muted-foreground">Stock signal qualification, separate from service health.</p></div>
      <div className="text-right"><span className="text-2xl font-semibold tabular-nums">{funnel ? `${funnel.actionable}/${funnel.total}` : "—"}</span><p className="text-xs text-muted-foreground">actionable stocks{loading ? " · refreshing" : ""}</p></div>
    </div>
    {error ? <p role="alert" className="mx-4 mb-4 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{funnel ? "Displayed funnel may be stale. " : ""}{error}</p> : null}
    {!funnel ? <p role="status" className="px-4 pb-4 text-sm text-muted-foreground">{loading ? "Loading decision stages…" : "Decision funnel is unavailable. Reload System to retry."}</p> : <>
      <ol className="grid gap-3 border-t border-border p-4 sm:grid-cols-2 xl:grid-cols-4">
        {(funnel.stages ?? []).map((stage, index) => {
          const state = funnelStageState(stage), presentation = STATE[state], Icon = presentation.Icon;
          const reached = stage.reached_count ?? 0, passed = stage.count ?? 0;
          const progress = reached > 0 ? Math.max(0, Math.min(100, passed / reached * 100)) : 0;
          return <li key={stage.stage} className="min-w-0 rounded-xl border border-border p-3">
            <div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-muted-foreground">{String(index + 1).padStart(2, "0")}</span><span className={`flex items-center gap-1 text-xs font-medium ${presentation.className}`}><Icon size={14} aria-hidden="true" />{presentation.label}</span></div>
            <h3 className="mt-3 text-sm font-semibold capitalize">{stage.stage.replaceAll("_", " ")}</h3>
            <p className="mt-2 text-xl font-semibold tabular-nums">{stage.count}<span className="text-sm font-normal text-muted-foreground"> / {stage.reached_count} passed</span></p>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label={`${stage.stage.replaceAll("_", " ")} pass rate`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress)}><div className="h-full rounded-full bg-primary" style={{ width: `${progress}%` }} /></div>
            <div className="mt-3 space-y-1 text-xs text-muted-foreground">{(stage.top_blockers ?? []).slice(0, 2).map(blocker => <p key={blocker.reason}>{blockerLabel(blocker.reason)} <span className="font-medium tabular-nums">· {blocker.count}</span></p>)}{state === "not_reached" ? <p>Waiting on an earlier stage—not another outage.</p> : null}{state === "blocked" && !stage.top_blockers?.length ? <p>Unresolved stage; inspect diagnostics.</p> : null}</div>
          </li>;
        })}
      </ol>
      {alpha ? <div className="mx-4 mb-4 rounded-xl border border-border bg-muted/20 p-4" aria-label="Stock alpha qualification">
        <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="flex items-center gap-2 text-sm font-semibold"><FlaskConical size={16} aria-hidden="true" />Stock alpha</h3><span className={`flex items-center gap-1 text-xs ${alpha.failed ? "text-destructive" : "text-muted-foreground"}`}><Clock3 size={13} aria-hidden="true" />{alpha.label}</span></div>
        <p className="mt-3 text-2xl font-semibold tabular-nums">{alpha.observations ?? "—"}<span className="ml-2 text-xs font-normal text-muted-foreground">independent point-in-time observations</span></p>
        <p className="mt-2 text-xs text-muted-foreground">{alpha.failed ? "Repair the evaluator error before expecting a new qualification result." : alpha.reason === "repeated_control_observations_unavailable" ? "Repeated controls have no usable results yet. No strategy promotion or stock forecast is authorized by this run." : alpha.complete ? "Validation passed. Promotion and a current forecast remain separate requirements." : "Only mature, independently eligible outcomes count. A resolved-outcome total is not a qualification score."}</p>
        {alpha.job.error ? <p role="alert" className="mt-2 text-sm text-destructive">{alpha.job.error}</p> : null}
        <div className="mt-3 flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>{alpha.job.finished_at ? `Last evaluator check ${new Date(alpha.job.finished_at).toLocaleString()}` : "Run has not finished"}</span><a className="font-medium text-primary hover:underline" href="/research">Inspect validation →</a></div>
      </div> : null}
      <details className="border-t border-border"><summary className="cursor-pointer px-4 py-3 text-sm text-muted-foreground">Diagnostics · owners, retries and evidence codes</summary><div className="space-y-4 px-4 pb-4 text-xs">
        <p className="text-muted-foreground">Policy {funnel.policy_version}. {funnel.scope === "monitored_stock_lane" ? "Watched and held stocks only; historical symbols and crypto are outside this stock-validation lane." : "Published stock-validation lane."} Each symbol stops at its first blocker.</p>
        {(funnel.stages ?? []).map(stage => <div key={stage.stage} className="rounded-lg border border-border p-3"><p className="font-semibold">{stage.stage.replaceAll("_", " ")} · {stage.reached_count} reached · {stage.not_reached_count} stopped upstream</p><p className="mt-1">Owner: {stage.owner} · Retry: {stage.retry}</p><p className="mt-1">Independent diagnostics · {stage.available_count}/{stage.total} available</p>{(stage.top_blockers ?? []).map(blocker => <p className="mt-1 break-words" key={`first-${blocker.reason}`}>First blocker: <code>{blocker.reason}</code> · {blocker.count}</p>)}{(stage.diagnostic_blockers ?? []).map(blocker => <p className="mt-1 break-words" key={blocker.reason}><code>{blocker.reason}</code> · {blocker.affected_symbols?.join(", ")} · {blocker.count}</p>)}</div>)}
      </div></details>
    </>}
  </section>;
}
