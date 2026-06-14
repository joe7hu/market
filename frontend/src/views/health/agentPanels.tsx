import { BrainCircuit, Clock3 } from "lucide-react";

import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import type { Tone } from "@/ui/tone";
import { titleLabel, toneFromText } from "@/views/rowFormat";
import { StatusDot } from "@/views/health/dataFlow";
import { latestAgentStep, latestRunnerCount } from "@/views/health/aggregate";
import { formatDateTime } from "@/views/health/format";
import type { AgentPipeline } from "@/views/health/types";
import type { UseRefreshJobs } from "@/views/health/useRefreshJobs";

export function AgentUsagePanel({ pipelines, jobs, schedulerSeconds }: { pipelines: AgentPipeline[]; jobs: UseRefreshJobs; schedulerSeconds: number }) {
  const fullRefreshAgentStep = latestAgentStep(jobs.latestStatus);
  const totalOpen = pipelines.reduce((sum, item) => sum + item.open, 0);
  const totalFulfilled = pipelines.reduce((sum, item) => sum + item.fulfilled, 0);
  const totalFailed = pipelines.reduce((sum, item) => sum + item.failed, 0);

  return (
    <DataTableFrame
      title="Agent Usage"
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <StatusBadge tone={schedulerSeconds > 0 ? "warn" : "good"}>{schedulerSeconds > 0 ? "In-app agents enabled" : "In-app agents paused"}</StatusBadge>
          <span>{totalOpen.toLocaleString()} open · {totalFailed.toLocaleString()} failed</span>
        </div>
      }
    >
      <div className="space-y-4 p-4">
        <div className="grid gap-3 md:grid-cols-3">
          <AgentStatTile label="Open agent work" value={totalOpen} caption="queued thesis/postmortem requests" tone={totalOpen ? "warn" : "good"} />
          <AgentStatTile label="Completed calls" value={totalFulfilled} caption="accepted structured outputs stored" tone={totalFulfilled ? "info" : "muted"} />
          <AgentStatTile label="Failed calls" value={totalFailed} caption="calls that consumed a run but returned no usable output" tone={totalFailed ? "bad" : "good"} />
        </div>

        <div className="grid gap-3 xl:grid-cols-2">
          {pipelines.map((pipeline) => (
            <div key={pipeline.id} className="rounded-lg border border-border bg-background p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <BrainCircuit className="size-4 text-muted-foreground" />
                    <h3 className="font-semibold">{pipeline.label}</h3>
                  </div>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">{pipeline.caption}</p>
                </div>
                <StatusBadge tone={pipeline.tone}>{pipeline.active ? "Active" : "Paused"}</StatusBadge>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
                <AgentMiniStat label="Open" value={pipeline.open} tone={pipeline.open ? "warn" : "good"} />
                <AgentMiniStat label="Done" value={pipeline.fulfilled} tone="info" />
                <AgentMiniStat label="Failed" value={pipeline.failed} tone={pipeline.failed ? "bad" : "good"} />
                <AgentMiniStat label="Superseded" value={pipeline.superseded} tone="muted" />
              </div>
              {pipeline.subCounts?.length ? (
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {pipeline.subCounts.map((sub) => (
                    <div key={sub.label} className="rounded-md border border-border/80 bg-muted/30 p-2 text-xs">
                      <div className="font-medium text-foreground">{sub.label}</div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-muted-foreground">
                        <span>Open <span className="text-foreground tabular-nums">{sub.open.toLocaleString()}</span></span>
                        <span>Done <span className="text-foreground tabular-nums">{sub.fulfilled.toLocaleString()}</span></span>
                        <span>Failed <span className="text-foreground tabular-nums">{sub.failed.toLocaleString()}</span></span>
                        <span>Cap <span className="text-foreground tabular-nums">{sub.limit.toLocaleString()}</span></span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
              <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>Run cap <span className="text-foreground tabular-nums">{pipeline.limit}</span></span>
                <span>Timeout <span className="text-foreground tabular-nums">{pipeline.timeoutSeconds}s</span></span>
                {pipeline.mode ? <span>Mode <span className="text-foreground">{titleLabel(pipeline.mode)}</span></span> : null}
                <span>Latest <span className="text-foreground">{formatDateTime(pipeline.latestAt)}</span></span>
              </div>
            </div>
          ))}
        </div>

        <table className="w-full min-w-[840px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-3">Spend Route</th>
              <th className="px-3 py-3">Cadence</th>
              <th className="px-3 py-3">Recent Agent Attempts</th>
              <th className="px-3 py-3">Accepted</th>
              <th className="px-3 py-3">Failed</th>
              <th className="px-3 py-3">Status</th>
            </tr>
          </thead>
          <tbody>
            <AgentRouteRow
              route="Full refresh option_agents step"
              cadence="daily full-market refresh"
              attempted={fullRefreshAgentStep.attempted}
              accepted={fullRefreshAgentStep.accepted}
              failed={fullRefreshAgentStep.failed}
              status={fullRefreshAgentStep.status}
            />
            <AgentRouteRow
              route="Manual run_option_agents"
              cadence="manual / refresh job"
              attempted={latestRunnerCount(jobs.rows, "run_option_agents", "attempted")}
              accepted={latestRunnerCount(jobs.rows, "run_option_agents", "accepted")}
              failed={latestRunnerCount(jobs.rows, "run_option_agents", "failed")}
              status={jobs.jobStates.run_option_agents?.status ?? "idle"}
            />
            <AgentRouteRow
              route="Premarket options intelligence"
              cadence="weekday premarket"
              attempted={latestRunnerCount(jobs.rows, "premarket_options_intelligence", "attempted")}
              accepted={latestRunnerCount(jobs.rows, "premarket_options_intelligence", "accepted")}
              failed={latestRunnerCount(jobs.rows, "premarket_options_intelligence", "failed")}
              status={jobs.jobStates.premarket_options_intelligence?.status ?? "status file"}
            />
            <AgentRouteRow
              route="In-app scheduler agent pass"
              cadence={schedulerSeconds > 0 ? `${schedulerSeconds}s interval` : "disabled"}
              attempted={0}
              accepted={0}
              failed={0}
              status={schedulerSeconds > 0 ? "enabled" : "paused"}
            />
          </tbody>
        </table>
      </div>
    </DataTableFrame>
  );
}

function AgentStatTile({ label, value, caption, tone }: { label: string; value: number; caption: string; tone: Tone }) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <div className="flex items-center gap-2 text-xs font-medium uppercase text-muted-foreground">
        <Clock3 className="size-3.5" />
        {label}
      </div>
      <div className="mt-2 text-2xl font-semibold tabular-nums">{value.toLocaleString()}</div>
      <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground"><StatusDot tone={tone} /> {caption}</div>
    </div>
  );
}

function AgentMiniStat({ label, value, tone }: { label: string; value: number; tone: Tone }) {
  return (
    <div className="rounded-md border border-border/80 bg-muted/30 p-2">
      <div className="text-[11px] uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 flex items-center gap-1.5 font-semibold tabular-nums"><StatusDot tone={tone} /> {value.toLocaleString()}</div>
    </div>
  );
}

function AgentRouteRow({ route, cadence, attempted, accepted, failed, status }: { route: string; cadence: string; attempted: number; accepted: number; failed: number; status: string }) {
  return (
    <tr className="border-b border-border align-top hover:bg-accent/40">
      <td className="px-3 py-3 font-medium">{route}</td>
      <td className="px-3 py-3 text-muted-foreground">{cadence}</td>
      <td className="px-3 py-3 tabular-nums">{attempted.toLocaleString()}</td>
      <td className="px-3 py-3 tabular-nums text-emerald-600 dark:text-emerald-400">{accepted.toLocaleString()}</td>
      <td className="px-3 py-3 tabular-nums">{failed ? <span className="text-red-600 dark:text-red-400">{failed.toLocaleString()}</span> : "0"}</td>
      <td className="px-3 py-3"><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></td>
    </tr>
  );
}
