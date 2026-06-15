import { Loader2, Play } from "lucide-react";

import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { Tone } from "@/ui/tone";
import { toneFromText } from "@/views/rowFormat";
import { JOB_CATALOG, jobDef, type JobGroup } from "@/views/health/dataFlow";
import { formatDateTime } from "@/views/health/format";
import type { UseRefreshJobs } from "@/views/health/useRefreshJobs";

export function TriggerPanel({ jobs, excludeJobs }: { jobs: UseRefreshJobs; excludeJobs?: Set<string> }) {
  // Per-category ingestion jobs are launched from the Data Sources catalog, so
  // hide those here and keep only orchestration jobs (full pipeline, synthesis,
  // agent) plus any ingestion job not owned by a catalog category.
  const exclude = excludeJobs ?? new Set<string>();
  const available = (jobs.allowlist.length ? jobs.allowlist : Object.keys(JOB_CATALOG)).filter((name: string) => !exclude.has(name));
  const groups: Array<{ id: JobGroup; label: string }> = [
    { id: "full", label: "Full Pipeline" },
    { id: "ingestion", label: "Ingestion (not in catalog)" },
    { id: "synthesis", label: "Synthesis (recompute signals)" },
  ];

  return (
    <DataTableFrame
      title="Manual Triggers"
      action={
        jobs.startError ? (
          <span className="text-sm text-red-600 dark:text-red-400">{jobs.startError}</span>
        ) : jobs.anyRunning ? (
          <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Background refresh active
          </span>
        ) : null
      }
    >
      <div className="space-y-4 p-4">
        {groups.map((group) => {
          const groupJobs = available.filter((name: string) => jobDef(name).group === group.id);
          if (!groupJobs.length) return null;
          return (
            <div key={group.id}>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{group.label}</h3>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                {groupJobs.map((name: string) => (
                  <JobCard key={name} jobName={name} jobs={jobs} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </DataTableFrame>
  );
}

function JobCard({ jobName, jobs }: { jobName: string; jobs: UseRefreshJobs }) {
  const def = jobDef(jobName);
  const state = jobs.jobStates[jobName];
  const pending = jobs.pendingJobs.has(jobName);
  const running = pending || state?.status === "running";
  const tone: Tone = running ? "info" : state ? toneFromText(state.status) : "muted";

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-background p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{def.label}</div>
          <div className="line-clamp-2 text-xs leading-4 text-muted-foreground">{def.description}</div>
        </div>
        <StatusBadge tone={tone}>{running ? "running" : state ? state.status : "idle"}</StatusBadge>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] text-muted-foreground">
          {state?.finishedAt ? `Last: ${formatDateTime(state.finishedAt)}` : state?.startedAt ? `Started: ${formatDateTime(state.startedAt)}` : "Never run here"}
        </span>
        <Button type="button" size="sm" variant={running ? "outline" : "default"} disabled={running} onClick={() => void jobs.start(jobName)}>
          {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          {running ? "Running" : "Run"}
        </Button>
      </div>
    </div>
  );
}
