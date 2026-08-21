import type { RefreshJob, SourceCatalogRow } from "@/api/panel";
import type { PanelData } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import { dateMs, truncate } from "@/views/health/format";
import type { ErrorAgg } from "@/views/health/types";
import type { FamilyHealth, SourceFamilyId } from "@/views/health/dataFlow";

export type SourceHealthFilter = "all" | "attention" | "failed" | "degraded" | "missing" | "stale" | "healthy";

export type SourceHealthGroup = {
  id: string;
  label: string;
  rows: SourceCatalogRow[];
  tone: Tone;
  healthy: number;
  attention: number;
  jobs: string[];
};

export type SourceHealthSummary = {
  total: number;
  enabled: number;
  healthy: number;
  attention: number;
  failed: number;
  disabled: number;
  lastSuccessAt: string;
};

const GROUP_LABELS: Record<string, string> = {
  market_data: "Market & Options",
  research: "News & Research",
  filings: "Filings & Disclosures",
  social: "Social & Private Graph",
  events: "Events & Calendars",
  broker: "Brokers",
  legacy: "Legacy Imports",
  other: "Other Sources",
};

const GROUP_ORDER = ["market_data", "research", "filings", "social", "events", "broker", "legacy", "other"];
const STATUS_RANK: Record<string, number> = { failed: 0, degraded: 1, running: 2, missing: 3, stale: 4, healthy: 5, disabled: 6 };
const TONE_RANK: Record<Tone, number> = { bad: 0, warn: 1, info: 2, good: 3, muted: 4 };

export function sourceHealthTone(status: string): Tone {
  if (status === "failed") return "bad";
  if (status === "degraded" || status === "missing" || status === "stale") return "warn";
  if (status === "running") return "info";
  if (status === "healthy") return "good";
  return "muted";
}

export function parseSourceCatalog(data: PanelData): SourceCatalogRow[] {
  return rows(data.sourceCatalog)
    .map((row) => ({
      source_id: text(row.source_id),
      source_name: text(row.source_name, "Unknown source"),
      source_family: text(row.source_family, "other"),
      source_kind: text(row.source_kind),
      operational_group: text(row.operational_group, "other"),
      enabled: bool(row.enabled),
      ingestion_mode: text(row.ingestion_mode),
      refresh_job: text(row.refresh_job),
      refresh_jobs: stringArray(row.refresh_jobs),
      cadence_label: text(row.cadence_label, "event driven"),
      run_status: text(row.run_status, "not_checked"),
      freshness_status: text(row.freshness_status, "missing"),
      effective_status: text(row.effective_status, bool(row.enabled) ? "missing" : "disabled"),
      latest_capability: text(row.latest_capability),
      capability_health: parseCapabilityHealth(row.capability_health),
      last_attempt_at: nullableText(row.last_attempt_at),
      status_at: nullableText(row.status_at),
      last_success_at: nullableText(row.last_success_at),
      last_data_at: nullableText(row.last_data_at),
      item_count: number(row.item_count),
      ticker_count: number(row.ticker_count),
      failure_detail: text(row.failure_detail),
      remediation: text(row.remediation),
      inherited_check: bool(row.inherited_check),
      source_url: text(row.source_url),
    }))
    .filter((row) => row.source_id)
    .sort(compareRows);
}

export function summarizeSourceHealth(sourceRows: SourceCatalogRow[]): SourceHealthSummary {
  const enabledRows = sourceRows.filter((row) => row.enabled);
  const successes = enabledRows.map((row) => row.last_success_at ?? "").filter(Boolean).sort((a, b) => dateMs(b) - dateMs(a));
  return {
    total: sourceRows.length,
    enabled: enabledRows.length,
    healthy: enabledRows.filter((row) => row.effective_status === "healthy").length,
    attention: enabledRows.filter((row) => row.effective_status !== "healthy").length,
    failed: enabledRows.filter((row) => row.effective_status === "failed").length,
    disabled: sourceRows.filter((row) => !row.enabled).length,
    lastSuccessAt: successes[0] ?? "",
  };
}

export function filterSourceHealth(
  sourceRows: SourceCatalogRow[],
  query: string,
  filter: SourceHealthFilter,
): SourceCatalogRow[] {
  const needle = query.trim().toLowerCase();
  return sourceRows.filter((row) => {
    if (!row.enabled) return false;
    if (filter === "attention" && row.effective_status === "healthy") return false;
    if (filter !== "all" && filter !== "attention" && row.effective_status !== filter) return false;
    if (!needle) return true;
    return [row.source_name, row.source_id, row.source_family, row.source_kind, row.latest_capability]
      .some((value) => value.toLowerCase().includes(needle));
  });
}

export function groupSourceHealth(sourceRows: SourceCatalogRow[]): SourceHealthGroup[] {
  const groups = new Map<string, SourceCatalogRow[]>();
  for (const row of sourceRows) {
    const list = groups.get(row.operational_group) ?? [];
    list.push(row);
    groups.set(row.operational_group, list);
  }
  return [...groups.entries()]
    .map(([id, groupRows]) => {
      const sorted = groupRows.sort(compareRows);
      const attention = sorted.filter((row) => row.effective_status !== "healthy").length;
      const tone = sorted
        .map((row) => sourceHealthTone(row.effective_status))
        .sort((a, b) => TONE_RANK[a] - TONE_RANK[b])[0] ?? "muted";
      return {
        id,
        label: GROUP_LABELS[id] ?? id.replaceAll("_", " "),
        rows: sorted,
        tone,
        healthy: sorted.length - attention,
        attention,
        jobs: [...new Set(sorted.flatMap((row) => row.refresh_jobs.length ? row.refresh_jobs : [row.refresh_job]).filter(Boolean))].sort(),
      };
    })
    .sort((a, b) => indexOfGroup(a.id) - indexOfGroup(b.id) || a.label.localeCompare(b.label));
}

export function sourceFamilyHealth(sourceRows: SourceCatalogRow[]): FamilyHealth[] {
  return groupSourceHealth(sourceRows.filter((row) => row.enabled)).map((group) => ({
    id: familyId(group.id),
    label: group.label,
    tone: group.tone,
    total: group.rows.length,
    healthy: group.healthy,
    jobs: group.jobs,
  }));
}

export function collectSourceErrors(sourceRows: SourceCatalogRow[], jobRows: RefreshJob[]): ErrorAgg[] {
  const errors = new Map<string, ErrorAgg>();
  const add = (message: string, tone: Tone, latestAt: string, source: string) => {
    if (!message) return;
    const key = message.toLowerCase();
    const existing = errors.get(key);
    if (existing) {
      existing.count += 1;
      if (!existing.sources.includes(source)) existing.sources.push(source);
      if (dateMs(latestAt) > dateMs(existing.latestAt)) existing.latestAt = latestAt;
      if (tone === "bad") existing.tone = "bad";
      return;
    }
    errors.set(key, { message, tone, count: 1, latestAt, sources: [source] });
  };
  for (const row of sourceRows) {
    const tone = sourceHealthTone(row.effective_status);
    if (!row.enabled || (tone !== "bad" && tone !== "warn")) continue;
    const message = row.remediation || truncate(row.failure_detail) || `${row.source_name} is ${row.effective_status}.`;
    add(message, tone, row.status_at ?? row.last_attempt_at ?? row.last_success_at ?? "", row.source_name);
  }
  const latestJobs = new Map<string, RefreshJob>();
  for (const row of jobRows) {
    const name = row.job_name ?? "Refresh job";
    const current = latestJobs.get(name);
    if (!current || dateMs(row.started_at) > dateMs(current.started_at)) latestJobs.set(name, row);
  }
  for (const row of latestJobs.values()) {
    if (row.status === "failed" && row.error) {
      add(truncate(row.error), "bad", row.finished_at ?? row.started_at ?? "", row.job_name ?? "Refresh job");
    }
  }
  return [...errors.values()]
    .sort((a, b) => (a.tone === b.tone ? b.count - a.count || dateMs(b.latestAt) - dateMs(a.latestAt) : a.tone === "bad" ? -1 : 1))
    .slice(0, 12);
}

function compareRows(a: SourceCatalogRow, b: SourceCatalogRow): number {
  return (STATUS_RANK[a.effective_status] ?? 9) - (STATUS_RANK[b.effective_status] ?? 9)
    || dateMs(b.last_attempt_at) - dateMs(a.last_attempt_at)
    || a.source_name.localeCompare(b.source_name);
}

function indexOfGroup(id: string): number {
  const index = GROUP_ORDER.indexOf(id);
  return index === -1 ? GROUP_ORDER.length : index;
}

function familyId(groupId: string): SourceFamilyId {
  if (groupId === "market_data") return "market_data";
  if (groupId === "research") return "blog";
  if (groupId === "filings") return "filing";
  if (groupId === "social") return "social";
  if (groupId === "events") return "events";
  if (groupId === "broker") return "broker";
  return "other";
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : fallback;
}

function nullableText(value: unknown): string | null {
  const valueText = text(value);
  return valueText || null;
}

function bool(value: unknown): boolean {
  return value === true || value === 1 || (typeof value === "string" && ["true", "1", "yes", "enabled"].includes(value.toLowerCase()));
}

function number(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function parseCapabilityHealth(value: unknown): SourceCatalogRow["capability_health"] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const row = entry && typeof entry === "object" ? entry as Record<string, unknown> : {};
    return {
      capability: text(row.capability),
      status: text(row.status),
      finished_at: nullableText(row.finished_at),
      failure_detail: text(row.failure_detail),
    };
  }).filter((entry) => entry.capability);
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((entry) => text(entry)).filter(Boolean) : [];
}
