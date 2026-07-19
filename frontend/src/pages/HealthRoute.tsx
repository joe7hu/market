import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Button } from "@/components/ui/button";
import { buildFlowStages, DataFlowDiagram } from "@/views/health/dataFlow";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";
import { collectSourceErrors, parseSourceCatalog, sourceFamilyHealth, summarizeSourceHealth } from "@/views/health/catalog";
import { Link } from "react-router-dom";

import { useRefreshJobs } from "@/views/health/useRefreshJobs";
import { TriggerPanel } from "@/views/health/triggerPanels";
import { SourceHealthControlPlane } from "@/views/health/catalogPanels";
import { TopErrorsPanel } from "@/views/health/categoryPanels";
import { RefreshHistoryTable } from "@/views/health/tables";
import { formatDateTime } from "@/views/health/format";
import { loadOptionHistoryHealth, type OptionHistoryHealth } from "@/api";

export function HealthRoute() {
  const { data, loadScope } = useMarketData();
  usePanelScope("health");

  const jobs = useRefreshJobs();
  const [reloading, setReloading] = useState(false);
  const [optionHistory, setOptionHistory] = useState<OptionHistoryHealth | null>(null);

  useEffect(() => { void loadOptionHistoryHealth().then(setOptionHistory).catch(() => setOptionHistory(null)); }, []);

  const sourceRows = useMemo(() => parseSourceCatalog(data), [data]);
  const summary = useMemo(() => summarizeSourceHealth(sourceRows), [sourceRows]);
  const families = useMemo(() => sourceFamilyHealth(sourceRows), [sourceRows]);
  const flowStages = useMemo(() => buildFlowStages(families), [families]);
  const topErrors = useMemo(() => collectSourceErrors(sourceRows, jobs.rows), [jobs.rows, sourceRows]);
  const catalogJobs = useMemo(
    () => new Set(sourceRows.flatMap((row) => row.refresh_jobs.length ? row.refresh_jobs : [row.refresh_job]).filter(Boolean)),
    [sourceRows],
  );

  const metrics: MetricSpec[] = [
    ["Enabled", summary.enabled.toLocaleString(), `${summary.total.toLocaleString()} registered sources`, summary.enabled ? "info" : "muted"],
    ["Healthy", summary.healthy.toLocaleString(), "current and reporting", summary.healthy === summary.enabled ? "good" : "info"],
    ["Needs Attention", summary.attention.toLocaleString(), "degraded, missing, or stale", summary.attention ? "warn" : "good"],
    ["Failed", summary.failed.toLocaleString(), "latest attempt failed", summary.failed ? "bad" : "good"],
    ["Disabled", summary.disabled.toLocaleString(), "excluded from health alerts", "muted"],
    ["Last Success", formatDateTime(summary.lastSuccessAt), "freshest successful source check", summary.lastSuccessAt ? "info" : "muted"],
    ["Option history", optionHistory ? `${optionHistory.complete_snapshots.toLocaleString()}/${optionHistory.snapshots.toLocaleString()} complete` : "Unavailable", optionHistory ? `${formatBytes(optionHistory.storage_bytes)} retained for ${optionHistory.retention_days} days` : "Waiting for database status", optionHistory ? "info" : "muted"],
    ["History coverage", optionHistory?.average_completeness !== null && optionHistory?.average_completeness !== undefined ? optionHistory.average_completeness.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "Collecting", optionHistory?.latest_complete_slot ? `Latest complete ${formatDateTime(optionHistory.latest_complete_slot)}` : "No complete full-chain slot yet", optionHistory?.complete_snapshots ? "good" : "warn"],
  ];

  // Note: a background scheduler job is almost always "running", so the Reload
  // button tracks its own local state instead of jobs.anyRunning.
  const reload = useCallback(async () => {
    setReloading(true);
    try {
      await Promise.all([loadScope("health").catch(() => undefined), jobs.refresh(), loadOptionHistoryHealth().then(setOptionHistory).catch(() => setOptionHistory(null))]);
    } finally {
      setReloading(false);
    }
  }, [jobs, loadScope]);

  return (
    <WorkspacePage
      eyebrow="Control plane"
      title="Source Health"
      subtitle="Operational source checks, data recency, coverage, and the exact jobs that own each refresh path."
      metrics={metrics}
      actions={
        <Button type="button" variant="outline" size="sm" onClick={() => void reload()} disabled={reloading}>
          <RefreshCw className={reloading ? "animate-spin" : undefined} />
          Reload
        </Button>
      }
    >
      <DataFlowDiagram stages={flowStages} />

      <SourceHealthControlPlane sourceRows={sourceRows} jobs={jobs} />

      <div className="flex items-center justify-between rounded-xl border border-border bg-card px-4 py-3">
        <span className="text-sm text-muted-foreground">The option agent has its own control plane — config, on-demand runs, context, and cost.</span>
        <Link to="/agent" className="text-sm font-medium text-primary hover:underline">Manage the option agent →</Link>
      </div>

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

function formatBytes(value: number): string { return value >= 1024 * 1024 ? `${(value / (1024 * 1024)).toFixed(1)} MB` : `${(value / 1024).toFixed(1)} KB`; }
