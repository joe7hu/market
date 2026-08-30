import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
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
import { loadOptionHistoryHealth, type OptionHistoryHealth } from "@/api/options";
import { loadDecisionFunnel, type DecisionFunnel } from "@/api/panel";
import { numberFromRecord, recordField } from "@/views/optionsRadarData";
import { DecisionFunnelPanel } from "@/views/health/decisionFunnel";

export function HealthRoute() {
  const { data, loadScope, scopeStatus } = useMarketData();
  usePanelScope("health", { retries: 3 });

  const jobs = useRefreshJobs();
  const [reloading, setReloading] = useState(false);
  const [optionHistory, setOptionHistory] = useState<OptionHistoryHealth | null>(null);
  const [decisionFunnel, setDecisionFunnel] = useState<DecisionFunnel | null>(null);

  useEffect(() => { void loadOptionHistoryHealth().then(setOptionHistory).catch(() => setOptionHistory(null)); }, []);
  useEffect(() => {
    void loadDecisionFunnel().then(setDecisionFunnel).catch(() => setDecisionFunnel(null));
  }, []);

  const sourceRows = useMemo(() => parseSourceCatalog(data), [data]);
  const summary = useMemo(() => summarizeSourceHealth(sourceRows), [sourceRows]);
  const families = useMemo(() => sourceFamilyHealth(sourceRows), [sourceRows]);
  const flowStages = useMemo(() => buildFlowStages(families), [families]);
  const topErrors = useMemo(() => collectSourceErrors(sourceRows, jobs.rows), [jobs.rows, sourceRows]);
  const catalogJobs = useMemo(
    () => new Set(sourceRows.flatMap((row) => row.refresh_jobs.length ? row.refresh_jobs : [row.refresh_job]).filter(Boolean)),
    [sourceRows],
  );
  // The scope can render once before its snapshot arrives.  The previous
  // direct dereference caused the Health route to crash during that state.
  const recoveryHealth = data.optionRecoveryHealth?.rows?.[0];
  const recoveryCapture = recordField(recoveryHealth, "capture");
  const recoveryStorage = recordField(recoveryHealth, "storage");
  const recoveryProgram = recordField(recoveryHealth, "program");
  const recoveryPaper = recoveryProgram ? recordField(recoveryProgram, "paper_staging") : undefined;
  const recoveryCoverage = numberFromRecord(recoveryCapture, "slot_coverage");
  const recoveryCompleteness = numberFromRecord(recoveryCapture, "contract_completeness");
  const recoveryContinuity = numberFromRecord(recoveryCapture, "same_contract_continuity");
  const recoveryLeaseCount = numberFromRecord(recoveryCapture, "active_robinhood_leases");
  const recoveryP95 = numberFromRecord(recoveryCapture, "capture_p95_minutes");

  const metrics: MetricSpec[] = [
    ["Active", summary.active.toLocaleString(), `${summary.enabled.toLocaleString()} enabled across ${summary.total.toLocaleString()} sources`, summary.active ? "info" : "muted"],
    ["Healthy", summary.healthy.toLocaleString(), "active and reporting", summary.activeAttention ? "info" : "good"],
    ["Needs Attention", summary.activeAttention.toLocaleString(), "active degraded, missing, or stale", summary.activeAttention ? "warn" : "good"],
    ["Failed", summary.failed.toLocaleString(), "latest attempt failed", summary.failed ? "bad" : "good"],
    ["Standby", summary.standby.toLocaleString(), "available but not selected", "info"],
    ["Archived", summary.archived.toLocaleString(), "historical evidence only", "muted"],
    ["Disabled", summary.disabled.toLocaleString(), "excluded from health alerts", "muted"],
    ["Last Success", formatDateTime(summary.lastSuccessAt), "freshest successful source check", summary.lastSuccessAt ? "info" : "muted"],
    ["Option history", optionHistory ? `${optionHistory.complete_captures.toLocaleString()} complete captures · ${optionHistory.observed_regular_session_dates.toLocaleString()} observed dates` : "Unavailable", optionHistory ? `${formatBytes(optionHistory.storage_bytes)} retained for ${optionHistory.retention_days} days` : "Waiting for database status", optionHistory ? "info" : "muted"],
    ["History coverage", optionHistory?.average_completeness !== null && optionHistory?.average_completeness !== undefined ? optionHistory.average_completeness.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "Collecting", optionHistory?.latest_complete_slot ? `${optionHistory.qualified_regular_sessions}/${optionHistory.required_regular_sessions} qualified post-fix sessions · latest ${formatDateTime(optionHistory.latest_complete_slot)}` : "No complete full-chain slot yet", optionHistory?.complete_captures ? "good" : "warn"],
    ["Recovery tape", recoveryCapture ? `${numberFromRecord(recoveryCapture, "active_events").toLocaleString()} active · ${numberFromRecord(recoveryCapture, "covered_slots").toLocaleString()}/${numberFromRecord(recoveryCapture, "scheduled_slots").toLocaleString()} slots` : "Unavailable", recoveryCapture ? "forward 15-minute event strips" : "Waiting for recovery health", recoveryCapture ? "info" : "muted"],
    ["Recovery canary", recoveryProgram ? `${String(recoveryProgram.program_state ?? "collecting").replaceAll("_", " ")} · ${numberFromRecord(recoveryProgram, "qualified_dates")}/${numberFromRecord(recoveryProgram, "required_qualified_dates").toLocaleString()} dates` : "Collecting", recoveryProgram ? `Paper staging: ${recoveryPaper?.eligible === true ? "enabled" : "disabled"}` : "Waiting for cohort health", recoveryPaper?.eligible === true ? "good" : "warn"],
    ["Recovery quality", recoveryCapture ? `${formatPercent(recoveryCoverage)} coverage · ${formatPercent(recoveryCompleteness)} complete · ${formatPercent(recoveryContinuity)} continuity` : "Collecting", recoveryCapture ? `${Number.isFinite(recoveryP95) ? `${recoveryP95.toFixed(1)}m p95` : "No capture latency yet"} · ${recoveryLeaseCount.toLocaleString()}/2 Robinhood leases` : "No event-strip data yet", recoveryCapture && recoveryCoverage >= 0.95 && recoveryCompleteness >= 0.98 && recoveryContinuity >= 0.90 && recoveryLeaseCount <= 2 ? "good" : "warn"],
    ["Recovery storage", recoveryStorage ? `${numberFromRecord(recoveryStorage, "observations").toLocaleString()} counterfactual observations` : "Unavailable", recoveryStorage ? `${numberFromRecord(recoveryStorage, "events").toLocaleString()} events · ${numberFromRecord(recoveryStorage, "captures").toLocaleString()} captures · ${numberFromRecord(recoveryStorage, "recovery_paper_orders").toLocaleString()} paper orders` : "Waiting for database status", recoveryStorage ? "info" : "muted"],
  ];

  // Note: a background scheduler job is almost always "running", so the Reload
  // button tracks its own local state instead of jobs.anyRunning.
  const reload = useCallback(async () => {
    setReloading(true);
    try {
      await Promise.all([
        loadScope("health").catch(() => undefined),
        jobs.refresh(),
        loadOptionHistoryHealth().then(setOptionHistory).catch(() => setOptionHistory(null)),
        loadDecisionFunnel().then(setDecisionFunnel).catch(() => setDecisionFunnel(null)),
      ]);
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
      {scopeStatus.health?.state === "loading" && sourceRows.length === 0 ? (
        <div role="status" className="rounded-xl border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
          Loading source health…
        </div>
      ) : null}
      <ScopeStatusNotice status={scopeStatus.health} onRetry={() => void reload()} />
      <DataFlowDiagram stages={flowStages} />

      <DecisionFunnelPanel funnel={decisionFunnel} />

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

function formatPercent(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "-";
}
