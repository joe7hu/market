import { RefreshCw } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Button } from "@/components/ui/button";
import { rows } from "@/utils";
import { buildFlowStages, DataFlowDiagram } from "@/views/health/dataFlow";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";
import {
  agentSchedulerSeconds,
  buildAgentPipelines,
  buildCategories,
  buildFamilyHealth,
  collectTopErrors,
} from "@/views/health/aggregate";
import { catalogToneCounts, groupCatalogByFamily, parseSourceCatalog } from "@/views/health/catalog";
import { useRefreshJobs } from "@/views/health/useRefreshJobs";
import { AgentUsagePanel } from "@/views/health/agentPanels";
import { TriggerPanel } from "@/views/health/triggerPanels";
import { CatalogControlPlane } from "@/views/health/catalogPanels";
import { TopErrorsPanel } from "@/views/health/categoryPanels";
import { CollapsibleSection, RefreshHistoryTable, RunStatusTable } from "@/views/health/tables";

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

  // Authoritative catalog (primary/fallback/cadence) drives the top-level view.
  const catalogCategories = useMemo(() => parseSourceCatalog(data), [data]);
  const catalogFamilies = useMemo(() => groupCatalogByFamily(catalogCategories), [catalogCategories]);

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

      <CatalogControlPlane
        families={catalogFamilies}
        data={data}
        expanded={expanded}
        onToggle={(id) => setExpanded((current) => (current === id ? null : id))}
        jobs={jobs}
      />

      <AgentUsagePanel pipelines={agentPipelines} jobs={jobs} schedulerSeconds={schedulerAgentSeconds} />

      <TriggerPanel jobs={jobs} />

      <TopErrorsPanel errors={topErrors} />

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
