import { useMemo } from "react";

import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import type { PanelData, RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import { DataGridSection } from "@/views/dataGridSection";
import { displayField, numberField, textField, titleLabel, toneFromText } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";

type SourceStatusRollup = {
  source: string;
  status: string;
  tone: Tone;
  latestAt: string;
  items: number;
  signals: number;
  tickers: number;
  checks: number;
  detail: string;
  origins: string[];
};

export function HealthRoute() {
  const { data, model } = useMarketData();
  usePanelScope("health");

  const sourceRollups = useMemo(() => buildSourceStatusRollups(data), [data]);
  const brokenRows = useMemo(() => sourceRollups.filter((row) => row.tone === "bad" || row.tone === "warn"), [sourceRollups]);
  const providerRows = rows(data.providerRuns);
  const refreshRows = rows(data.refreshJobs);
  const brokerRows = rows(data.brokerStatus);
  const badCount = sourceRollups.filter((row) => row.tone === "bad").length;
  const warnCount = sourceRollups.filter((row) => row.tone === "warn").length;

  const metrics: MetricSpec[] = [
    ["Broken Now", brokenRows.length.toLocaleString(), "bad or warning source statuses", brokenRows.length ? "bad" : "good"],
    ["Failed", badCount.toLocaleString(), "source rollups marked bad", badCount ? "bad" : "good"],
    ["Warning", warnCount.toLocaleString(), "source rollups needing attention", warnCount ? "warn" : "good"],
    ["Sources", sourceRollups.length.toLocaleString(), "rolled up across source tables", sourceRollups.length ? "info" : "muted"],
    ["Latest Check", model.latestHealthCheck, "freshest health timestamp", model.sources.health === "live" ? "info" : "muted"],
  ];

  return (
    <WorkspacePage
      eyebrow="System health"
      title="Health"
      subtitle="Current failures first, then per-source status rollups and raw drill-down rows."
      metrics={metrics}
    >
      <SourceStatusTable title="Broken Now" rows={brokenRows.slice(0, 60)} emptyText="No bad or warning source statuses in the current snapshot." />
      <SourceStatusTable title="Source Status Rollup" rows={sourceRollups.slice(0, 120)} emptyText="No source status rows available." />
      <RunStatusTable title="Provider Runs" rows={providerRows.slice(0, 80)} />
      <RunStatusTable title="Refresh Jobs" rows={refreshRows.slice(0, 80)} />
      <RunStatusTable title="Broker Status" rows={brokerRows.slice(0, 80)} />
      <DataGridSection title="Source Health Rows" rows={rows(data.sourceHealth)} />
      <DataGridSection title="Source Freshness Rows" rows={rows(data.sourceFreshness)} />
    </WorkspacePage>
  );
}

function SourceStatusTable({ title, rows: sourceRows, emptyText }: { title: string; rows: SourceStatusRollup[]; emptyText: string }) {
  return (
    <DataTableFrame title={title}>
      <table className="w-full min-w-[980px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Source</th>
            <th className="px-3 py-3">Status</th>
            <th className="px-3 py-3">Latest</th>
            <th className="px-3 py-3">Items</th>
            <th className="px-3 py-3">Signals</th>
            <th className="px-3 py-3">Tickers</th>
            <th className="px-3 py-3">Rows</th>
            <th className="px-3 py-3">Evidence</th>
            <th className="px-3 py-3">Detail</th>
          </tr>
        </thead>
        <tbody>
          {sourceRows.map((row) => (
            <tr key={row.source} className="border-b border-border align-top hover:bg-accent/40">
              <td className="max-w-[260px] px-3 py-3 font-medium">{row.source}</td>
              <td className="px-3 py-3"><StatusBadge tone={row.tone}>{titleLabel(row.status)}</StatusBadge></td>
              <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(row.latestAt)}</td>
              <td className="px-3 py-3 tabular-nums">{row.items.toLocaleString()}</td>
              <td className="px-3 py-3 tabular-nums">{row.signals.toLocaleString()}</td>
              <td className="px-3 py-3 tabular-nums">{row.tickers.toLocaleString()}</td>
              <td className="px-3 py-3 tabular-nums">{row.checks.toLocaleString()}</td>
              <td className="max-w-[220px] px-3 py-3 text-muted-foreground">{row.origins.join(", ")}</td>
              <td className="max-w-[420px] px-3 py-3 text-muted-foreground">{row.detail || "-"}</td>
            </tr>
          ))}
          {!sourceRows.length ? <EmptyRow colSpan={9} text={emptyText} /> : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function RunStatusTable({ title, rows: runRows }: { title: string; rows: RowRecord[] }) {
  return (
    <DataTableFrame title={title}>
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
          {!runRows.length ? <EmptyRow colSpan={6} text={`No ${title.toLowerCase()} rows available.`} /> : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function buildSourceStatusRollups(data: PanelData): SourceStatusRollup[] {
  const rollups = new Map<string, SourceStatusRollup & { originSet: Set<string> }>();

  for (const row of rows(data.sources)) {
    const status = displayField(row, ["latest_run_status", "freshness", "health", "status"], booleanField(row, "enabled") ? "not_loaded" : "disabled");
    mergeSourceStatus(rollups, row, {
      source: displayField(row, ["source_name", "source_id", "source"], "Source"),
      status,
      latestAt: displayField(row, ["latest_run_at", "latest_at", "checked_at"], ""),
      detail: displayField(row, ["latest_failure_detail", "detail", "notes"], ""),
      origin: "directory",
    });
  }

  for (const row of rows(data.sourceFreshness)) {
    const status = displayField(row, ["status", "freshness", "freshness_status", "freshness_state", "health"], "not_loaded");
    mergeSourceStatus(rollups, row, {
      source: displayField(row, ["source_name", "source_id", "source", "provider"], "Source"),
      status,
      latestAt: displayField(row, ["latest_run_at", "latest_at", "checked_at", "as_of", "updated_at"], ""),
      detail: displayField(row, ["detail", "message", "reason", "blocker"], ""),
      origin: "freshness",
    });
  }

  for (const row of rows(data.sourceHealth)) {
    const status = displayField(row, ["status", "health"], "not_loaded");
    mergeSourceStatus(rollups, row, {
      source: displayField(row, ["source", "source_name", "source_id", "provider"], "Source"),
      status,
      latestAt: displayField(row, ["checked_at", "latest_at", "updated_at"], ""),
      detail: displayField(row, ["detail", "message", "error"], ""),
      origin: "health",
    });
  }

  for (const row of rows(data.brokerStatus)) {
    const provider = displayField(row, ["provider", "source"], "broker");
    const status = displayField(row, ["status", "provider_status", "health"], "not_loaded");
    mergeSourceStatus(rollups, row, {
      source: `broker:${provider}`,
      status,
      latestAt: displayField(row, ["checked_at", "last_data_at", "updated_at"], ""),
      detail: displayField(row, ["detail", "message", "error"], ""),
      origin: "broker",
    });
  }

  return [...rollups.values()]
    .map(({ originSet, ...row }) => ({ ...row, origins: [...originSet].sort() }))
    .sort((a, b) => toneRank(a.tone) - toneRank(b.tone) || compareDateDesc(a.latestAt, b.latestAt) || a.source.localeCompare(b.source));
}

function mergeSourceStatus(
  rollups: Map<string, SourceStatusRollup & { originSet: Set<string> }>,
  row: RowRecord,
  patch: { source: string; status: string; latestAt: string; detail: string; origin: string },
) {
  const key = patch.source.trim().toLowerCase() || "source";
  const tone = toneFromText(patch.status);
  const current = rollups.get(key);
  const next = current ?? {
    source: patch.source,
    status: patch.status,
    tone,
    latestAt: "",
    items: 0,
    signals: 0,
    tickers: 0,
    checks: 0,
    detail: "",
    origins: [],
    originSet: new Set<string>(),
  };

  if (!current || toneRank(tone) < toneRank(next.tone)) {
    next.status = patch.status;
    next.tone = tone;
    next.detail = patch.detail;
  } else if (!next.detail && patch.detail) {
    next.detail = patch.detail;
  }

  if (compareDateDesc(patch.latestAt, next.latestAt) < 0) {
    next.latestAt = patch.latestAt;
  }
  next.items = Math.max(next.items, numberField(row, ["items_count", "item_count"], 0));
  next.signals = Math.max(next.signals, numberField(row, ["signals_count", "signal_count"], 0));
  next.tickers = Math.max(next.tickers, numberField(row, ["tickers_count", "ticker_count"], 0));
  next.checks += 1;
  next.originSet.add(patch.origin);
  rollups.set(key, next);
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <tr>
      <td className="px-4 py-6 text-sm text-muted-foreground" colSpan={colSpan}>{text}</td>
    </tr>
  );
}

function booleanField(row: RowRecord, key: string): boolean {
  const value = row[key];
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["true", "yes", "1", "enabled"].includes(value.trim().toLowerCase());
  return false;
}

function toneRank(tone: Tone): number {
  return tone === "bad" ? 0 : tone === "warn" ? 1 : tone === "info" ? 2 : tone === "good" ? 3 : 4;
}

function compareDateDesc(a: string, b: string): number {
  return dateMs(b) - dateMs(a);
}

function dateMs(value: string): number {
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function formatDateTime(value: string): string {
  if (!value || value === "-") return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
