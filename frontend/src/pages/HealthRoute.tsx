import { AlertTriangle, BrainCircuit, ChevronDown, ChevronRight, Clock3, Loader2, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { loadRefreshJobs, startRefreshJob, type RefreshJob, type RefreshJobsPayload } from "../api";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { PanelData, RowRecord } from "@/types";
import { toneFromOperationalStatus, type Tone } from "@/ui/tone";
import { rows } from "@/utils";
import {
  buildFlowStages,
  DataFlowDiagram,
  jobDef,
  JOB_CATALOG,
  type JobGroup,
  sourceFamilyDef,
  type SourceFamilyId,
  StatusDot,
  toneRank,
  worstTone,
  type FamilyHealth,
} from "@/views/health/dataFlow";
import { displayField, numberField, titleLabel, toneFromText } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";

// --- Types -----------------------------------------------------------------

type ProviderStat = {
  provider: string;
  tone: Tone;
  status: string;
  checks: number;
  fresh: number;
  stale: number;
  failed: number;
  latestAt: string;
  detail: string;
  items: number;
  signals: number;
  tickers: number;
};

type Category = {
  id: string;
  label: string;
  family: SourceFamilyId;
  tone: Tone;
  total: number; // distinct providers
  fresh: number;
  stale: number;
  failed: number;
  checks: number;
  items: number;
  signals: number;
  tickers: number;
  latestAt: string;
  origins: string[];
  providers: ProviderStat[];
};

type JobState = { status: string; startedAt?: string; finishedAt?: string | null; error?: string | null };
type AgentRuntime = { active: boolean; configured: boolean; limit: number; timeoutSeconds: number; requestCap?: number; cadence: string };
type AgentPipeline = {
  id: string;
  label: string;
  caption: string;
  tone: Tone;
  active: boolean;
  open: number;
  fulfilled: number;
  failed: number;
  superseded: number;
  limit: number;
  timeoutSeconds: number;
  latestAt: string;
};

// --- Category map ----------------------------------------------------------

type CategoryDef = { id: string; label: string; family: SourceFamilyId };

const CATEGORY_BY_TYPE: Record<string, CategoryDef> = {
  provider_health: { id: "provider_health", label: "Provider Health", family: "market_data" },
  closing_quote: { id: "quotes", label: "Quotes", family: "market_data" },
  intraday_quote: { id: "quotes", label: "Quotes", family: "market_data" },
  crypto_quote: { id: "quotes", label: "Quotes", family: "market_data" },
  options: { id: "options", label: "Options Chains", family: "market_data" },
  fundamental: { id: "fundamentals", label: "Fundamentals", family: "market_data" },
  filing: { id: "filings", label: "Filings & Ownership", family: "filing" },
  news: { id: "news", label: "News Wires", family: "blog" },
  provider_run: { id: "ingestion_runs", label: "Source Ingestion Runs", family: "other" },
  daily: { id: "daily", label: "Daily Analyses", family: "market_data" },
  documentation: { id: "documentation", label: "Documentation", family: "other" },
  arco_thesis: { id: "social", label: "Social Graph", family: "social" },
};

const OTHER_CATEGORY: CategoryDef = { id: "other", label: "Other", family: "other" };
const INGESTION_RUNS_CATEGORY: CategoryDef = { id: "ingestion_runs", label: "Source Ingestion Runs", family: "other" };

// Freshness types whose providers are followed content sources (not raw data
// feeds), so they should be filed under their directory family rather than the
// generic "Source Ingestion Runs" bucket.
const CONTENT_TYPES = new Set(["provider_run", "news", "filing", "arco_thesis"]);

// Directory source_family -> category. Ids are shared with CATEGORY_BY_TYPE so a
// followed source and its freshness rows collapse into ONE category (no
// "News Wires" vs "Followed News" duplication).
const DIR_FAMILY_CATEGORY: Record<string, CategoryDef> = {
  news: { id: "news", label: "News Wires", family: "blog" },
  podcast: { id: "podcasts", label: "Podcasts", family: "podcast" },
  blog: { id: "blogs", label: "Blogs & Memos", family: "blog" },
  private_graph: { id: "social", label: "Social Graph", family: "social" },
  social: { id: "social", label: "Social Graph", family: "social" },
  filing: { id: "filings", label: "Filings & Ownership", family: "filing" },
  transcript: { id: "transcripts", label: "Transcripts", family: "transcript" },
  market_data: { id: "market_data", label: "Market Data Sources", family: "market_data" },
  provider: { id: "provider_health", label: "Provider Health", family: "market_data" },
  estimates: { id: "fundamentals", label: "Fundamentals", family: "market_data" },
};

type DirEntry = { name: string; def: CategoryDef; items: number; signals: number; tickers: number; status: string; latestAt: string; detail: string };

// --- Route -----------------------------------------------------------------

export function HealthRoute() {
  const { data, model, loadScope } = useMarketData();
  usePanelScope("health");

  const jobs = useRefreshJobs();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);

  const categories = useMemo(() => buildCategories(data), [data]);
  const families = useMemo(() => buildFamilyHealth(categories), [categories]);
  const flowStages = useMemo(() => buildFlowStages(families), [families]);
  const agentPipelines = useMemo(() => buildAgentPipelines(data), [data]);
  const schedulerAgentSeconds = useMemo(() => agentSchedulerSeconds(data), [data]);
  const topErrors = useMemo(() => collectTopErrors(categories, data, jobs.rows), [categories, data, jobs.rows]);

  const totalProviders = categories.reduce((sum, category) => sum + category.total, 0);
  const failed = categories.reduce((sum, category) => sum + category.failed, 0);
  const stale = categories.reduce((sum, category) => sum + category.stale, 0);
  const fresh = categories.reduce((sum, category) => sum + category.fresh, 0);

  const metrics: MetricSpec[] = [
    ["Sources", totalProviders.toLocaleString(), `across ${categories.length} categories`, totalProviders ? "info" : "muted"],
    ["Fresh", fresh.toLocaleString(), "reporting on contract", fresh ? "good" : "muted"],
    ["Stale", stale.toLocaleString(), "past freshness window", stale ? "warn" : "good"],
    ["Failed", failed.toLocaleString(), "errored or unreachable", failed ? "bad" : "good"],
    ["Last Check", model.latestHealthCheck, "freshest health timestamp", model.sources.health === "live" ? "info" : "muted"],
  ];

  // Note: a background scheduler job is almost always "running", so the Reload
  // button tracks its own local state instead of jobs.anyRunning.
  const reload = useCallback(async () => {
    setReloading(true);
    try {
      await Promise.all([loadScope("health").catch(() => undefined), jobs.refresh()]);
    } finally {
      setReloading(false);
    }
  }, [jobs, loadScope]);

  return (
    <WorkspacePage
      eyebrow="Control plane"
      title="Source Health"
      subtitle="Per-source status, top errors, manual refresh triggers, and how every source feeds each ticker's signals."
      metrics={metrics}
      actions={
        <Button type="button" variant="outline" size="sm" onClick={() => void reload()} disabled={reloading}>
          <RefreshCw className={reloading ? "animate-spin" : undefined} />
          Reload
        </Button>
      }
    >
      <DataFlowDiagram stages={flowStages} />

      <AgentUsagePanel pipelines={agentPipelines} jobs={jobs} schedulerSeconds={schedulerAgentSeconds} />

      <TriggerPanel jobs={jobs} />

      <TopErrorsPanel errors={topErrors} />

      <CategoryControlPlane
        categories={categories}
        expanded={expanded}
        onToggle={(id) => setExpanded((current) => (current === id ? null : id))}
        jobs={jobs}
      />

      <CollapsibleSection title="Provider Runs" count={rows(data.providerRuns).length}>
        <RunStatusTable rows={rows(data.providerRuns).slice(0, 80)} />
      </CollapsibleSection>
      <CollapsibleSection title="Refresh Job History" count={jobs.rows.length}>
        <RefreshHistoryTable rows={jobs.rows.slice(0, 60)} />
      </CollapsibleSection>
      <CollapsibleSection title="Broker Status" count={rows(data.brokerStatus).length}>
        <RunStatusTable rows={rows(data.brokerStatus).slice(0, 60)} />
      </CollapsibleSection>
    </WorkspacePage>
  );
}

// --- Agent usage dashboard -------------------------------------------------

function AgentUsagePanel({ pipelines, jobs, schedulerSeconds }: { pipelines: AgentPipeline[]; jobs: UseRefreshJobs; schedulerSeconds: number }) {
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
              <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>Run cap <span className="text-foreground tabular-nums">{pipeline.limit}</span></span>
                <span>Timeout <span className="text-foreground tabular-nums">{pipeline.timeoutSeconds}s</span></span>
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

// --- Manual trigger panel --------------------------------------------------

type UseRefreshJobs = ReturnType<typeof useRefreshJobs>;

function TriggerPanel({ jobs }: { jobs: UseRefreshJobs }) {
  const available = jobs.allowlist.length ? jobs.allowlist : Object.keys(JOB_CATALOG);
  const groups: Array<{ id: JobGroup; label: string }> = [
    { id: "full", label: "Full Pipeline" },
    { id: "ingestion", label: "Ingestion (pull sources)" },
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
          const groupJobs = available.filter((name) => jobDef(name).group === group.id);
          if (!groupJobs.length) return null;
          return (
            <div key={group.id}>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{group.label}</h3>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                {groupJobs.map((name) => (
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

// --- Top errors ------------------------------------------------------------

type ErrorAgg = { message: string; tone: Tone; count: number; latestAt: string; sources: string[] };

function TopErrorsPanel({ errors }: { errors: ErrorAgg[] }) {
  return (
    <DataTableFrame title="Top Errors">
      {errors.length ? (
        <table className="w-full min-w-[820px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-3">Count</th>
              <th className="px-3 py-3">Error</th>
              <th className="px-3 py-3">Affected Sources</th>
              <th className="px-3 py-3">Latest</th>
            </tr>
          </thead>
          <tbody>
            {errors.map((error, index) => (
              <tr key={`${error.message}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
                <td className="px-3 py-3">
                  <StatusBadge tone={error.tone}>{error.count.toLocaleString()}×</StatusBadge>
                </td>
                <td className="max-w-[520px] px-3 py-3">
                  <div className="flex items-start gap-2">
                    <AlertTriangle className={`mt-0.5 size-4 shrink-0 ${error.tone === "bad" ? "text-red-500" : "text-amber-500"}`} />
                    <span className="leading-5">{error.message}</span>
                  </div>
                </td>
                <td className="max-w-[260px] px-3 py-3 text-muted-foreground">
                  {error.sources.slice(0, 6).join(", ")}
                  {error.sources.length > 6 ? ` +${error.sources.length - 6}` : ""}
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(error.latestAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="flex items-center gap-2 px-4 py-6 text-sm text-muted-foreground">
          <StatusDot tone="good" /> No source errors in the current snapshot.
        </div>
      )}
    </DataTableFrame>
  );
}

// --- Category control plane (summary + provider drill-down) ----------------

function CategoryControlPlane({
  categories,
  expanded,
  onToggle,
  jobs,
}: {
  categories: Category[];
  expanded: string | null;
  onToggle: (id: string) => void;
  jobs: UseRefreshJobs;
}) {
  return (
    <DataTableFrame title="Source Status">
      <table className="w-full min-w-[1040px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="w-8 px-2 py-3" />
            <th className="px-3 py-3">Category</th>
            <th className="px-3 py-3">Status</th>
            <th className="px-3 py-3">Sources</th>
            <th className="px-3 py-3">Fresh</th>
            <th className="px-3 py-3">Stale</th>
            <th className="px-3 py-3">Failed</th>
            <th className="px-3 py-3">Latest</th>
            <th className="px-3 py-3">Action</th>
          </tr>
        </thead>
        <tbody>
          {categories.map((category) => {
            const isOpen = expanded === category.id;
            const family = sourceFamilyDef(category.family);
            const Icon = family.icon;
            const job = family.job;
            const running = jobs.pendingJobs.has(job) || jobs.jobStates[job]?.status === "running";
            return (
              <FragmentRow key={category.id}>
                <tr className="border-b border-border align-top hover:bg-accent/40">
                  <td className="px-2 py-3">
                    <button type="button" onClick={() => onToggle(category.id)} className="text-muted-foreground hover:text-foreground" aria-label={isOpen ? "Collapse" : "Expand"}>
                      {isOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                    </button>
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex items-center gap-2">
                      <Icon className="size-4 text-muted-foreground" />
                      <span className="font-medium">{category.label}</span>
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">{family.label}</div>
                  </td>
                  <td className="px-3 py-3"><StatusBadge tone={category.tone}>{statusLabel(category.tone)}</StatusBadge></td>
                  <td className="px-3 py-3 tabular-nums">{category.total.toLocaleString()}</td>
                  <td className="px-3 py-3 tabular-nums text-emerald-600 dark:text-emerald-400">{category.fresh.toLocaleString()}</td>
                  <td className="px-3 py-3 tabular-nums">{category.stale ? <span className="text-amber-600 dark:text-amber-400">{category.stale.toLocaleString()}</span> : <span className="text-muted-foreground">0</span>}</td>
                  <td className="px-3 py-3 tabular-nums">{category.failed ? <span className="text-red-600 dark:text-red-400">{category.failed.toLocaleString()}</span> : <span className="text-muted-foreground">0</span>}</td>
                  <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(category.latestAt)}</td>
                  <td className="px-3 py-3">
                    <Button type="button" size="sm" variant="outline" disabled={running} onClick={() => void jobs.start(job)} title={`Run ${jobDef(job).label}`}>
                      {running ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                      Refresh
                    </Button>
                  </td>
                </tr>
                {isOpen ? (
                  <tr className="border-b border-border bg-muted/30">
                    <td />
                    <td colSpan={8} className="px-3 py-3">
                      <CategoryDrillDown category={category} />
                    </td>
                  </tr>
                ) : null}
              </FragmentRow>
            );
          })}
          {!categories.length ? (
            <tr>
              <td colSpan={9} className="px-4 py-6 text-sm text-muted-foreground">No source status rows available.</td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function CategoryDrillDown({ category }: { category: Category }) {
  const hasContribution = category.items > 0 || category.signals > 0 || category.tickers > 0;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <span>Checks: <span className="text-foreground tabular-nums">{category.checks.toLocaleString()}</span></span>
        <span>Tables: <span className="text-foreground">{category.origins.join(", ") || "-"}</span></span>
        <span>Refresh job: <span className="text-foreground">{jobDef(sourceFamilyDef(category.family).job).label}</span></span>
        {hasContribution ? (
          <span>
            Contributes: <span className="text-foreground tabular-nums">{category.items.toLocaleString()}</span> items ·{" "}
            <span className="text-foreground tabular-nums">{category.signals.toLocaleString()}</span> signals ·{" "}
            <span className="text-foreground tabular-nums">{category.tickers.toLocaleString()}</span> tickers
          </span>
        ) : null}
      </div>
      <table className="w-full min-w-[640px] text-xs">
        <thead className="text-left text-muted-foreground">
          <tr>
            <th className="py-1 pr-3">Source</th>
            <th className="py-1 pr-3">Status</th>
            <th className="py-1 pr-3">Checks</th>
            <th className="py-1 pr-3">Stale</th>
            <th className="py-1 pr-3">Failed</th>
            <th className="py-1 pr-3">Latest</th>
            <th className="py-1 pr-3">Detail</th>
          </tr>
        </thead>
        <tbody>
          {category.providers.slice(0, 40).map((provider) => (
            <tr key={provider.provider} className="border-t border-border/60 align-top">
              <td className="py-1 pr-3 font-medium">{provider.provider}</td>
              <td className="py-1 pr-3"><StatusBadge tone={provider.tone}>{titleLabel(provider.status)}</StatusBadge></td>
              <td className="py-1 pr-3 tabular-nums">{provider.checks.toLocaleString()}</td>
              <td className="py-1 pr-3 tabular-nums">{provider.stale || "-"}</td>
              <td className="py-1 pr-3 tabular-nums">{provider.failed || "-"}</td>
              <td className="whitespace-nowrap py-1 pr-3 text-muted-foreground">{formatDateTime(provider.latestAt)}</td>
              <td className="max-w-[360px] truncate py-1 pr-3 text-muted-foreground" title={provider.detail}>{provider.detail || "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {category.providers.length > 40 ? (
        <div className="text-xs text-muted-foreground">Showing 40 of {category.providers.length} sources (worst first).</div>
      ) : null}
    </div>
  );
}

// --- Secondary tables ------------------------------------------------------

function RunStatusTable({ rows: runRows }: { rows: RowRecord[] }) {
  return (
    <table className="w-full min-w-[920px] text-sm">
      <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
        <tr>
          <th className="px-3 py-3">Name</th>
          <th className="px-3 py-3">Capability</th>
          <th className="px-3 py-3">Status</th>
          <th className="px-3 py-3">Started</th>
          <th className="px-3 py-3">Finished</th>
          <th className="px-3 py-3">Detail</th>
        </tr>
      </thead>
      <tbody>
        {runRows.map((row, index) => {
          const status = displayField(row, ["status", "health", "provider_status", "latest_status"], "not_loaded");
          return (
            <tr key={`${displayField(row, ["id", "run_id", "job_name", "provider"], "run")}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
              <td className="max-w-[260px] px-3 py-3 font-medium">{displayField(row, ["job_name", "provider", "source", "id", "run_id"], "run")}</td>
              <td className="px-3 py-3 text-muted-foreground">{displayField(row, ["capability", "source_url", "account_type"], "-")}</td>
              <td className="px-3 py-3"><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></td>
              <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(displayField(row, ["started_at", "checked_at", "timestamp"], ""))}</td>
              <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(displayField(row, ["finished_at", "last_data_at", "updated_at"], ""))}</td>
              <td className="max-w-[460px] px-3 py-3 text-muted-foreground">{displayField(row, ["error", "detail", "message", "failedStep"], "-")}</td>
            </tr>
          );
        })}
        {!runRows.length ? (
          <tr>
            <td colSpan={6} className="px-4 py-6 text-sm text-muted-foreground">No rows available.</td>
          </tr>
        ) : null}
      </tbody>
    </table>
  );
}

function RefreshHistoryTable({ rows: jobRows }: { rows: RefreshJob[] }) {
  return (
    <table className="w-full min-w-[820px] text-sm">
      <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
        <tr>
          <th className="px-3 py-3">Job</th>
          <th className="px-3 py-3">Status</th>
          <th className="px-3 py-3">Started</th>
          <th className="px-3 py-3">Finished</th>
          <th className="px-3 py-3">Error</th>
        </tr>
      </thead>
      <tbody>
        {jobRows.map((row, index) => (
          <tr key={`${row.id ?? row.job_name}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
            <td className="max-w-[260px] px-3 py-3 font-medium">{jobDef(row.job_name ?? "").label}</td>
            <td className="px-3 py-3"><StatusBadge tone={toneFromText(row.status ?? "")}>{titleLabel(row.status ?? "unknown")}</StatusBadge></td>
            <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(row.started_at ?? "")}</td>
            <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(row.finished_at ?? "")}</td>
            <td className="max-w-[460px] px-3 py-3 text-muted-foreground">{row.error || "-"}</td>
          </tr>
        ))}
        {!jobRows.length ? (
          <tr>
            <td colSpan={5} className="px-4 py-6 text-sm text-muted-foreground">No refresh jobs recorded.</td>
          </tr>
        ) : null}
      </tbody>
    </table>
  );
}

function CollapsibleSection({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <details className="overflow-hidden rounded-xl border border-border bg-card">
      <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-lg font-semibold">
        <span>{title}</span>
        <span className="text-sm font-normal text-muted-foreground">{count.toLocaleString()} rows</span>
      </summary>
      <div className="overflow-x-auto border-t border-border">{children}</div>
    </details>
  );
}

function FragmentRow({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

// --- Refresh-jobs hook -----------------------------------------------------

function useRefreshJobs() {
  const [payload, setPayload] = useState<RefreshJobsPayload | null>(null);
  const [pendingJobs, setPendingJobs] = useState<Set<string>>(() => new Set());
  const [startError, setStartError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setPayload(await loadRefreshJobs());
    } catch {
      // Keep the last good payload; the reload button can retry.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const jobRows = payload?.rows ?? [];
  const jobStates = useMemo(() => latestByJob(jobRows), [jobRows]);
  const anyRunning = useMemo(
    () => pendingJobs.size > 0 || Object.values(jobStates).some((state) => state.status === "running"),
    [jobStates, pendingJobs],
  );

  useEffect(() => {
    if (!anyRunning) return;
    const id = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(id);
  }, [anyRunning, refresh]);

  const start = useCallback(
    async (jobName: string) => {
      setStartError(null);
      setPendingJobs((prev) => new Set(prev).add(jobName));
      try {
        await startRefreshJob(jobName);
        await refresh();
      } catch (error) {
        setStartError(error instanceof Error ? error.message : "Failed to start refresh job");
      } finally {
        setPendingJobs((prev) => {
          const next = new Set(prev);
          next.delete(jobName);
          return next;
        });
      }
    },
    [refresh],
  );

  return {
    allowlist: payload?.allowlist ?? [],
    latestStatus: payload?.latest_status ?? null,
    rows: jobRows,
    jobStates,
    pendingJobs,
    anyRunning,
    start,
    startError,
    refresh,
  };
}

function latestByJob(jobRows: RefreshJob[]): Record<string, JobState> {
  const out: Record<string, JobState> = {};
  for (const row of jobRows) {
    const name = row.job_name;
    if (!name) continue;
    const existing = out[name];
    if (!existing || dateMs(row.started_at ?? "") > dateMs(existing.startedAt ?? "")) {
      out[name] = { status: row.status ?? "", startedAt: row.started_at, finishedAt: row.finished_at, error: row.error };
    }
  }
  return out;
}

// --- Aggregation -----------------------------------------------------------

function buildCategories(data: PanelData): Category[] {
  // Accumulate per category -> per provider.
  const cats = new Map<string, { def: CategoryDef; providers: Map<string, ProviderStat>; origins: Set<string> }>();

  const record = (
    def: CategoryDef,
    provider: string,
    tone: Tone,
    status: string,
    latestAt: string,
    detail: string,
    origin: string,
    counts: { items?: number; signals?: number; tickers?: number } = {},
  ) => {
    let cat = cats.get(def.id);
    if (!cat) {
      cat = { def, providers: new Map(), origins: new Set() };
      cats.set(def.id, cat);
    }
    cat.origins.add(origin);
    const key = baseProvider(provider);
    const stat =
      cat.providers.get(key) ??
      ({ provider: key, tone: "good", status, checks: 0, fresh: 0, stale: 0, failed: 0, latestAt: "", detail: "", items: 0, signals: 0, tickers: 0 } satisfies ProviderStat);
    stat.checks += 1;
    if (tone === "bad") stat.failed += 1;
    else if (tone === "warn") stat.stale += 1;
    else if (tone === "good" || tone === "info") stat.fresh += 1;
    if (toneRank(tone) < toneRank(stat.tone)) {
      stat.tone = tone;
      stat.status = status;
      if (detail) stat.detail = detail;
    } else if (!stat.detail && detail) {
      stat.detail = detail;
    }
    if (dateMs(latestAt) > dateMs(stat.latestAt)) stat.latestAt = latestAt;
    stat.items = Math.max(stat.items, counts.items ?? 0);
    stat.signals = Math.max(stat.signals, counts.signals ?? 0);
    stat.tickers = Math.max(stat.tickers, counts.tickers ?? 0);
    cat.providers.set(key, stat);
  };

  // Index the followed-source directory up front so freshness rows for the same
  // source land in one content category and pick up contribution stats.
  const dirIndex = new Map<string, DirEntry>();
  for (const row of rows(data.sources)) {
    const name = displayField(row, ["source_name", "source"], "");
    if (!name) continue;
    const family = displayField(row, ["source_family", "content_type"], "");
    const enabled = booleanValue(row.enabled) || booleanValue(row.is_followed);
    dirIndex.set(normKey(name), {
      name,
      def: DIR_FAMILY_CATEGORY[family] ?? INGESTION_RUNS_CATEGORY,
      items: numberField(row, ["items_count", "item_count"], 0),
      signals: numberField(row, ["signals_count", "signal_count"], 0),
      tickers: numberField(row, ["tickers_count", "ticker_count"], 0),
      status: displayField(row, ["latest_run_status", "freshness", "health", "status"], enabled ? "not_loaded" : "disabled"),
      latestAt: displayField(row, ["latest_run_at", "latest_at", "checked_at"], ""),
      detail: displayField(row, ["latest_failure_detail", "notes"], ""),
    });
  }
  const matchedDir = new Set<string>();

  // Source freshness — the bulk of the pipeline health.
  for (const row of rows(data.sourceFreshness)) {
    const type = displayField(row, ["source_type"], "");
    const provider = displayField(row, ["provider", "source_key", "source"], "unknown");
    // Only content-source freshness rows fold into the directory category.
    const dir = CONTENT_TYPES.has(type) ? dirIndex.get(normKey(baseProvider(provider))) : undefined;
    if (dir) matchedDir.add(normKey(baseProvider(provider)));
    const status = displayField(row, ["freshness_status", "status"], "not_loaded");
    record(
      dir ? dir.def : CATEGORY_BY_TYPE[type] ?? OTHER_CATEGORY,
      provider,
      freshnessTone(displayField(row, ["freshness_status"], ""), displayField(row, ["status"], "")),
      status,
      displayField(row, ["last_observed_at", "checked_at"], ""),
      displayField(row, ["detail"], ""),
      "freshness",
      dir ? { items: dir.items, signals: dir.signals, tickers: dir.tickers } : {},
    );
  }

  // Source health — upstream provider reachability. Verified-docs markers go to
  // Documentation; everything else is provider reachability. Per-symbol probes
  // (e.g. "yfinance:ZENA") collapse to their base provider inside record().
  for (const row of rows(data.sourceHealth)) {
    const provider = displayField(row, ["source"], "unknown");
    const status = displayField(row, ["status"], "not_loaded");
    const def = status === "verified_docs" ? CATEGORY_BY_TYPE.documentation : CATEGORY_BY_TYPE.provider_health;
    record(
      def,
      provider,
      toneFromText(status),
      status,
      displayField(row, ["checked_at"], ""),
      displayField(row, ["detail"], ""),
      "health",
    );
  }

  // Followed sources with no matching freshness row yet — add them once, into the
  // same shared category, so the directory never duplicates a freshness entry.
  for (const [key, entry] of dirIndex) {
    if (matchedDir.has(key)) continue;
    record(
      entry.def,
      entry.name,
      toneFromText(entry.status),
      entry.status,
      entry.latestAt,
      entry.detail,
      "directory",
      { items: entry.items, signals: entry.signals, tickers: entry.tickers },
    );
  }

  // Broker connectivity.
  for (const row of rows(data.brokerStatus)) {
    const provider = displayField(row, ["provider", "source"], "broker");
    const status = displayField(row, ["status", "health"], "not_loaded");
    record(
      { id: "broker", label: "Broker", family: "broker" },
      provider,
      toneFromText(status),
      status,
      displayField(row, ["checked_at", "last_data_at"], ""),
      displayField(row, ["detail"], ""),
      "broker",
    );
  }

  // Finalize: roll provider stats up to category summaries.
  const categories: Category[] = [];
  for (const { def, providers, origins } of cats.values()) {
    const list = [...providers.values()].sort(
      (a, b) => toneRank(a.tone) - toneRank(b.tone) || dateMs(b.latestAt) - dateMs(a.latestAt) || a.provider.localeCompare(b.provider),
    );
    const category: Category = {
      id: def.id,
      label: def.label,
      family: def.family,
      tone: worstTone(list.map((stat) => stat.tone)),
      total: list.length,
      fresh: list.filter((stat) => stat.tone === "good" || stat.tone === "info").length,
      stale: list.filter((stat) => stat.tone === "warn").length,
      failed: list.filter((stat) => stat.tone === "bad").length,
      checks: list.reduce((sum, stat) => sum + stat.checks, 0),
      items: list.reduce((sum, stat) => sum + stat.items, 0),
      signals: list.reduce((sum, stat) => sum + stat.signals, 0),
      tickers: list.reduce((sum, stat) => sum + stat.tickers, 0),
      latestAt: list.reduce((latest, stat) => (dateMs(stat.latestAt) > dateMs(latest) ? stat.latestAt : latest), ""),
      origins: [...origins].sort(),
      providers: list,
    };
    categories.push(category);
  }

  return categories.sort(
    (a, b) => toneRank(a.tone) - toneRank(b.tone) || b.failed - a.failed || b.total - a.total || a.label.localeCompare(b.label),
  );
}

function buildFamilyHealth(categories: Category[]): FamilyHealth[] {
  const byFamily = new Map<SourceFamilyId, { tones: Tone[]; total: number; healthy: number }>();
  for (const category of categories) {
    const entry = byFamily.get(category.family) ?? { tones: [], total: 0, healthy: 0 };
    entry.tones.push(category.tone);
    entry.total += category.total;
    entry.healthy += category.fresh;
    byFamily.set(category.family, entry);
  }
  return [...byFamily.entries()].map(([id, entry]) => ({
    id,
    label: sourceFamilyDef(id).label,
    tone: worstTone(entry.tones),
    total: entry.total,
    healthy: entry.healthy,
  }));
}

function buildAgentPipelines(data: PanelData): AgentPipeline[] {
  const metadata = jsonRecord(data.dashboard.status?.metadata);
  const agents = jsonRecord(metadata.agents);
  const thesisRuntime = agentRuntime(jsonRecord(agents.option_thesis), 8);
  const postmortemRuntime = agentRuntime(jsonRecord(agents.option_postmortem), 4);
  const thesisRequests = rows(data.agentThesisRequest);
  const postmortemRequests = rows(data.agentPostmortemRequest);
  return [
    buildAgentPipeline({
      id: "option_thesis",
      label: "Option Thesis Agent",
      caption: "Product, technology, catalyst, invalidation, and red-team synthesis for top-ranked options candidates.",
      runtime: thesisRuntime,
      requestRows: thesisRequests,
      resultRows: rows(data.agentThesis),
    }),
    buildAgentPipeline({
      id: "option_postmortem",
      label: "Option Postmortem Agent",
      caption: "Structured explanations and strategy-mutation proposals for missed winners, losers, and invalidations.",
      runtime: postmortemRuntime,
      requestRows: postmortemRequests,
      resultRows: rows(data.agentPostmortem),
    }),
  ];
}

function agentSchedulerSeconds(data: PanelData): number {
  const metadata = jsonRecord(data.dashboard.status?.metadata);
  const scheduler = jsonRecord(metadata.scheduler);
  return numberFromJson(scheduler.agent_refresh_seconds, 0);
}

function buildAgentPipeline({
  id,
  label,
  caption,
  runtime,
  requestRows,
  resultRows,
}: {
  id: string;
  label: string;
  caption: string;
  runtime: AgentRuntime;
  requestRows: RowRecord[];
  resultRows: RowRecord[];
}): AgentPipeline {
  const statusCounts = countByStatus(requestRows);
  const failed = (statusCounts.agent_failed ?? 0) + (statusCounts.failed ?? 0);
  const open = statusCounts.open ?? 0;
  const latestRequest = latestRowDate(requestRows, ["created_at", "updated_at"]);
  const latestResult = latestRowDate(resultRows, ["created_at", "updated_at"]);
  const latestAt = dateMs(latestResult) > dateMs(latestRequest) ? latestResult : latestRequest;
  const tone: Tone = failed ? "bad" : open && !runtime.active ? "warn" : runtime.active ? "good" : "muted";
  return {
    id,
    label,
    caption,
    tone,
    active: runtime.active,
    open,
    fulfilled: statusCounts.fulfilled ?? resultRows.length,
    failed,
    superseded: statusCounts.superseded ?? 0,
    limit: runtime.limit,
    timeoutSeconds: runtime.timeoutSeconds,
    latestAt,
  };
}

function agentRuntime(raw: Record<string, unknown>, fallbackLimit: number): AgentRuntime {
  return {
    active: booleanFromJson(raw.active ?? raw.enabled, false) && booleanFromJson(raw.configured, Boolean(raw.command)),
    configured: booleanFromJson(raw.configured, Boolean(raw.command)),
    limit: numberFromJson(raw.limit, fallbackLimit),
    timeoutSeconds: numberFromJson(raw.timeout_seconds, 120),
    requestCap: raw.request_cap === undefined ? undefined : numberFromJson(raw.request_cap, fallbackLimit),
    cadence: stringFromJson(raw.cadence, "daily_premarket"),
  };
}

function countByStatus(requestRows: RowRecord[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const row of requestRows) {
    const status = displayField(row, ["status"], "unknown").toLowerCase();
    counts[status] = (counts[status] ?? 0) + 1;
  }
  return counts;
}

function latestRowDate(requestRows: RowRecord[], fields: string[]): string {
  return requestRows.reduce((latest, row) => {
    const value = displayField(row, fields, "");
    return dateMs(value) > dateMs(latest) ? value : latest;
  }, "");
}

function latestAgentStep(status: unknown): { attempted: number; accepted: number; failed: number; status: string } {
  const full = jsonRecord(status);
  const steps = Array.isArray(full.steps) ? full.steps : [];
  const optionStep = steps.map(jsonRecord).find((step) => stringFromJson(step.name, "") === "option_agents");
  const result = jsonRecord(optionStep?.result);
  const thesis = jsonRecord(result.agent_thesis_runner);
  const postmortem = jsonRecord(result.agent_postmortem_runner);
  return {
    attempted: numberFromJson(thesis.attempted, 0) + numberFromJson(postmortem.attempted, 0),
    accepted: numberFromJson(thesis.accepted, 0) + numberFromJson(postmortem.accepted, 0),
    failed: numberFromJson(thesis.failed, 0) + numberFromJson(postmortem.failed, 0),
    status: optionStep ? (optionStep.ok === false ? "failed" : "succeeded") : "unknown",
  };
}

function latestRunnerCount(jobRows: RefreshJob[], jobName: string, field: "attempted" | "accepted" | "failed"): number {
  const row = jobRows.find((item) => item.job_name === jobName);
  if (!row) return 0;
  const summary = jsonRecord(row.summary);
  const nestedAgents = jsonRecord(summary.agents);
  const source = Object.keys(nestedAgents).length ? nestedAgents : summary;
  const thesis = jsonRecord(source.agent_thesis_runner);
  const postmortem = jsonRecord(source.agent_postmortem_runner);
  return numberFromJson(thesis[field], 0) + numberFromJson(postmortem[field], 0);
}

function collectTopErrors(categories: Category[], data: PanelData, jobRows: RefreshJob[]): ErrorAgg[] {
  const byMessage = new Map<string, ErrorAgg>();

  const add = (message: string, tone: Tone, at: string, source: string) => {
    const clean = message.trim();
    if (!clean) return;
    const dedupeKey = clean.slice(0, 160).toLowerCase();
    const existing = byMessage.get(dedupeKey);
    if (existing) {
      existing.count += 1;
      if (toneRank(tone) < toneRank(existing.tone)) existing.tone = tone;
      if (dateMs(at) > dateMs(existing.latestAt)) existing.latestAt = at;
      if (!existing.sources.includes(source)) existing.sources.push(source);
    } else {
      byMessage.set(dedupeKey, { message: clean.slice(0, 200), tone, count: 1, latestAt: at, sources: [source] });
    }
  };

  for (const category of categories) {
    for (const provider of category.providers) {
      if (provider.tone !== "bad" && provider.tone !== "warn") continue;
      const message = provider.detail ? truncate(provider.detail) : `${category.label}: ${provider.status}`;
      add(message, provider.tone, provider.latestAt, provider.provider);
    }
  }
  for (const row of rows(data.providerRuns)) {
    const status = displayField(row, ["status"], "");
    const tone = toneFromText(status);
    if (tone === "bad" || tone === "warn") {
      const detail = displayField(row, ["detail", "error", "message"], titleLabel(status));
      add(truncate(detail), tone, displayField(row, ["finished_at", "started_at"], ""), `${displayField(row, ["provider"], "provider")}:${displayField(row, ["capability"], "")}`);
    }
  }
  for (const row of jobRows) {
    if ((row.status ?? "") === "failed" && row.error) {
      add(truncate(row.error), "bad", row.finished_at ?? row.started_at ?? "", jobDef(row.job_name ?? "").label);
    }
  }

  return [...byMessage.values()]
    .sort((a, b) => toneRank(a.tone) - toneRank(b.tone) || b.count - a.count || dateMs(b.latestAt) - dateMs(a.latestAt))
    .slice(0, 12);
}

// --- Small helpers ---------------------------------------------------------

function freshnessTone(freshnessStatus: string, status: string): Tone {
  const fresh = freshnessStatus.toLowerCase();
  if (fresh === "failed") return "bad";
  if (fresh === "stale") return "warn";
  if (fresh === "documentation" || fresh === "not_applicable") return "muted";
  const statusTone = toneFromOperationalStatus(status);
  if (statusTone) return statusTone;
  if (fresh === "fresh") return "good";
  return toneFromText(freshnessStatus || status);
}

/** Collapse per-symbol probes ("yahoo-chart:KNOX.V", "yfinance:ZENA") to their base provider. */
function baseProvider(provider: string): string {
  const trimmed = provider.trim();
  if (!trimmed) return "unknown";
  const colon = trimmed.indexOf(":");
  return colon > 0 ? trimmed.slice(0, colon) : trimmed;
}

/** Normalized key for matching directory names to freshness providers (space/underscore/case-insensitive). */
function normKey(value: string): string {
  return value.toLowerCase().replace(/[\s_-]+/g, "");
}

function statusLabel(tone: Tone): string {
  return tone === "good" ? "Healthy" : tone === "warn" ? "Degraded" : tone === "bad" ? "Failed" : tone === "info" ? "Active" : "Idle";
}

function booleanValue(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["true", "yes", "1", "followed", "enabled"].includes(value.trim().toLowerCase());
  return false;
}

function truncate(value: string, max = 180): string {
  const clean = value.trim().replace(/\s+/g, " ");
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

function dateMs(value: string | undefined | null): number {
  if (!value) return 0;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function numberFromJson(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : fallback;
}

function booleanFromJson(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["1", "true", "yes", "on", "enabled", "active"].includes(value.trim().toLowerCase());
  return fallback;
}

function stringFromJson(value: unknown, fallback: string): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function formatDateTime(value: string | undefined | null): string {
  if (!value || value === "-") return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
