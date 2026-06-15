import { RefreshCw } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Button } from "@/components/ui/button";
import { buildFlowStages, DataFlowDiagram } from "@/views/health/dataFlow";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";
import {
  agentSchedulerSeconds,
  buildCategories,
  buildFamilyHealth,
  collectTopErrors,
} from "@/views/health/aggregate";
import { catalogToneCounts, groupCatalogByFamily, parseSourceCatalog } from "@/views/health/catalog";
import { useRefreshJobs } from "@/views/health/useRefreshJobs";
import { AgentControlPanel } from "@/views/health/agentPanels";
import { TriggerPanel } from "@/views/health/triggerPanels";
import { CatalogControlPlane } from "@/views/health/catalogPanels";
import { TopErrorsPanel } from "@/views/health/categoryPanels";
import { RefreshHistoryTable } from "@/views/health/tables";

export function HealthRoute() {
  const { data, model, loadScope } = useMarketData();
  usePanelScope("health");

  const jobs = useRefreshJobs();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);

  const categories = useMemo(() => buildCategories(data), [data]);
  const families = useMemo(() => buildFamilyHealth(categories), [categories]);
  const flowStages = useMemo(() => buildFlowStages(families), [families]);
  const schedulerAgentSeconds = useMemo(() => agentSchedulerSeconds(data), [data]);
  const topErrors = useMemo(() => collectTopErrors(categories, data, jobs.rows), [categories, data, jobs.rows]);

  // Authoritative catalog (primary/fallback/cadence) drives the top-level view.
  const catalogCategories = useMemo(() => parseSourceCatalog(data), [data]);
  const catalogFamilies = useMemo(() => groupCatalogByFamily(catalogCategories), [catalogCategories]);
  // Per-category refresh jobs are launched from the catalog, so the Operations
  // trigger panel hides them and shows only orchestration/uncovered jobs.
  const catalogJobs = useMemo(
    () => new Set(catalogCategories.map((category) => category.refresh_job).filter(Boolean)),
    [catalogCategories],
  );

  // Prefer the catalog's category tones for top metrics; fall back to the live
  // joiner's provider counts when the catalog has not arrived yet.
  const catalogCounts = useMemo(() => catalogToneCounts(catalogCategories), [catalogCategories]);
  const totalProviders = categories.reduce((sum, category) => sum + category.total, 0);
  const failed = catalogCategories.length ? catalogCounts.failed : categories.reduce((sum, category) => sum + category.failed, 0);
  const stale = catalogCategories.length ? catalogCounts.stale : categories.reduce((sum, category) => sum + category.stale, 0);
  const fresh = catalogCategories.length ? catalogCounts.fresh : categories.reduce((sum, category) => sum + category.fresh, 0);
  const categoryCount = catalogCategories.length || categories.length;

  const metrics: MetricSpec[] = [
    ["Categories", categoryCount.toLocaleString(), `${totalProviders.toLocaleString()} live providers`, categoryCount ? "info" : "muted"],
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
      subtitle="Data sources by category with primary/fallback status, the option-agent control plane, and operations triggers."
      metrics={metrics}
      actions={
        <Button type="button" variant="outline" size="sm" onClick={() => void reload()} disabled={reloading}>
          <RefreshCw className={reloading ? "animate-spin" : undefined} />
          Reload
        </Button>
      }
    >
      <DataFlowDiagram stages={flowStages} />

      <CatalogControlPlane
        families={catalogFamilies}
        data={data}
        expanded={expanded}
        onToggle={(id) => setExpanded((current) => (current === id ? null : id))}
        jobs={jobs}
      />

      <AgentControlPanel data={data} jobs={jobs} schedulerSeconds={schedulerAgentSeconds} onChanged={() => void reload()} />

      <details className="overflow-hidden rounded-xl border border-border bg-card">
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-lg font-semibold">
          <span>Operations</span>
          <span className="text-sm font-normal text-muted-foreground">triggers · top errors · run history</span>
        </summary>
        <div className="space-y-4 border-t border-border p-4">
          <TriggerPanel jobs={jobs} excludeJobs={catalogJobs} />
          <TopErrorsPanel errors={topErrors} />
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex items-center justify-between px-4 py-3 text-sm font-semibold">
              <span>Refresh Job History</span>
              <span className="font-normal text-muted-foreground">{jobs.rows.length.toLocaleString()} rows</span>
            </div>
            <div className="overflow-x-auto border-t border-border">
              <RefreshHistoryTable rows={jobs.rows.slice(0, 60)} />
            </div>
          </div>
        </div>
      </details>
    </WorkspacePage>
  );
}
