import { ChevronDown, ChevronRight, Database, Loader2, RefreshCw, Search } from "lucide-react";
import { Fragment, useMemo, useState } from "react";

import type { SourceCatalogRow } from "@/api";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { jobDef, StatusDot } from "@/views/health/dataFlow";
import {
  filterSourceHealth,
  groupSourceHealth,
  sourceHealthTone,
  type SourceHealthFilter,
  type SourceHealthGroup,
} from "@/views/health/catalog";
import { formatAge, formatDateTime } from "@/views/health/format";
import type { UseRefreshJobs } from "@/views/health/useRefreshJobs";

const FILTERS: Array<{ id: SourceHealthFilter; label: string }> = [
  { id: "all", label: "All active" },
  { id: "attention", label: "Needs attention" },
  { id: "failed", label: "Failed" },
  { id: "degraded", label: "Degraded" },
  { id: "missing", label: "Missing" },
  { id: "stale", label: "Stale" },
  { id: "healthy", label: "Healthy" },
];

export function SourceHealthControlPlane({
  sourceRows,
  jobs,
}: {
  sourceRows: SourceCatalogRow[];
  jobs: UseRefreshJobs;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<SourceHealthFilter>("all");
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(() => new Set());
  const [expandedSource, setExpandedSource] = useState<string | null>(null);
  const [showDisabled, setShowDisabled] = useState(false);

  const activeRows = useMemo(() => filterSourceHealth(sourceRows, query, filter), [filter, query, sourceRows]);
  const groups = useMemo(() => groupSourceHealth(activeRows), [activeRows]);
  const disabledRows = useMemo(
    () => sourceRows.filter((row) => !row.enabled && matchesQuery(row, query)),
    [query, sourceRows],
  );

  const toggleGroup = (group: SourceHealthGroup) => {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (next.has(group.id)) next.delete(group.id);
      else next.add(group.id);
      return next;
    });
  };

  return (
    <DataTableFrame title="Operational Sources">
      <div className="border-b border-border bg-muted/20 p-3 sm:p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="relative min-w-0 flex-1 lg:max-w-sm">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search source, family, or capability"
              className="pl-9"
            />
          </div>
          <div className="flex gap-1 overflow-x-auto pb-1 lg:pb-0" aria-label="Source health filters">
            {FILTERS.map((item) => (
              <Button
                key={item.id}
                type="button"
                size="sm"
                variant={filter === item.id ? "default" : "ghost"}
                className="shrink-0"
                onClick={() => setFilter(item.id)}
              >
                {item.label}
              </Button>
            ))}
          </div>
        </div>
      </div>

      <div className="space-y-3 p-3 sm:p-4">
        {groups.map((group) => {
          const isExpanded = expandedGroups.has(group.id) || Boolean(query) || filter !== "all";
          const visibleRows = isExpanded ? group.rows : collapsedRows(group.rows);
          return (
            <section key={group.id} className="overflow-hidden rounded-xl border border-border bg-background shadow-sm">
              <div className="flex flex-col gap-3 border-b border-border bg-muted/30 px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
                <button type="button" className="flex min-w-0 items-center gap-2 text-left" onClick={() => toggleGroup(group)}>
                  {isExpanded ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
                  <Database className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate text-sm font-semibold">{group.label}</span>
                  <StatusBadge tone={group.tone}>{group.attention ? `${group.attention} attention` : "healthy"}</StatusBadge>
                  <span className="text-xs tabular-nums text-muted-foreground">{group.healthy}/{group.rows.length} healthy</span>
                </button>
                <GroupActions group={group} jobs={jobs} />
              </div>

              <div className="hidden md:block">
                <SourceTable rows={visibleRows} expandedSource={expandedSource} onToggleSource={setExpandedSource} />
              </div>
              <div className="divide-y divide-border md:hidden">
                {visibleRows.map((row) => (
                  <SourceCard key={row.source_id} row={row} expanded={expandedSource === row.source_id} onToggle={() => setExpandedSource(expandedSource === row.source_id ? null : row.source_id)} />
                ))}
              </div>
              {!isExpanded && group.rows.length > visibleRows.length ? (
                <button type="button" className="w-full border-t border-border px-4 py-2 text-xs text-primary hover:bg-accent" onClick={() => toggleGroup(group)}>
                  Show all {group.rows.length} sources
                </button>
              ) : null}
            </section>
          );
        })}

        {!groups.length ? (
          <div className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">
            No enabled sources match this filter.
          </div>
        ) : null}

        <section className="overflow-hidden rounded-xl border border-border bg-muted/10">
          <button
            type="button"
            className="flex w-full items-center justify-between px-4 py-3 text-left"
            onClick={() => setShowDisabled((value) => !value)}
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              {showDisabled ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
              Disabled and catalog-only sources
            </span>
            <StatusBadge tone="muted">{disabledRows.length}</StatusBadge>
          </button>
          {showDisabled ? (
            <div className="grid gap-2 border-t border-border p-3 sm:grid-cols-2 xl:grid-cols-3">
              {disabledRows.map((row) => (
                <div key={row.source_id} className="rounded-lg border border-border bg-background px-3 py-2">
                  <div className="truncate text-sm font-medium">{row.source_name}</div>
                  <div className="mt-0.5 truncate text-xs text-muted-foreground">{row.source_family} · {row.source_kind || "catalog"}</div>
                </div>
              ))}
            </div>
          ) : null}
        </section>
      </div>
    </DataTableFrame>
  );
}

function GroupActions({ group, jobs }: { group: SourceHealthGroup; jobs: UseRefreshJobs }) {
  const validJobs = group.jobs.filter((job) => !jobs.allowlist.length || jobs.allowlist.includes(job));
  if (!validJobs.length) return <span className="text-xs text-muted-foreground">No direct refresh action</span>;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {validJobs.map((job) => {
        const running = jobs.pendingJobs.has(job) || jobs.jobStates[job]?.status === "running";
        return (
          <Button key={job} type="button" size="sm" variant="outline" disabled={running} onClick={() => void jobs.start(job)} title={jobDef(job).description}>
            {running ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            {jobDef(job).label}
          </Button>
        );
      })}
    </div>
  );
}

function SourceTable({
  rows,
  expandedSource,
  onToggleSource,
}: {
  rows: SourceCatalogRow[];
  expandedSource: string | null;
  onToggleSource: (sourceId: string | null) => void;
}) {
  return (
    <table className="w-full table-fixed text-sm">
      <thead className="bg-muted/20 text-left text-xs text-muted-foreground">
        <tr>
          <th className="w-[30%] px-3 py-2.5">Source</th>
          <th className="w-[13%] px-3 py-2.5">Health</th>
          <th className="w-[17%] px-3 py-2.5">Last Check</th>
          <th className="w-[17%] px-3 py-2.5">Last Data</th>
          <th className="w-[23%] px-3 py-2.5">Coverage</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const expanded = expandedSource === row.source_id;
          return (
            <Fragment key={row.source_id}>
              <tr className="cursor-pointer border-t border-border/70 align-top hover:bg-accent/40" onClick={() => onToggleSource(expanded ? null : row.source_id)}>
                <td className="px-3 py-3">
                  <div className="flex items-start gap-2">
                    <StatusDot tone={sourceHealthTone(row.effective_status)} className="mt-1" />
                    <div className="min-w-0">
                      <div className="truncate font-medium">{row.source_name}</div>
                      <div className="truncate text-xs text-muted-foreground">{row.source_kind || row.source_family} · {row.cadence_label}</div>
                    </div>
                  </div>
                </td>
                <td className="px-3 py-3"><HealthBadge row={row} /></td>
                <td className="px-3 py-3"><TimeCell value={row.last_attempt_at} inherited={row.inherited_check} /></td>
                <td className="px-3 py-3"><TimeCell value={row.last_data_at} emptyLabel="Checked, no rows" /></td>
                <td className="px-3 py-3"><Coverage row={row} /></td>
              </tr>
              {expanded ? <SourceDetail row={row} colSpan={5} /> : null}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

function SourceCard({ row, expanded, onToggle }: { row: SourceCatalogRow; expanded: boolean; onToggle: () => void }) {
  return (
    <div className="p-3">
      <button type="button" className="w-full text-left" onClick={onToggle}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2">
            <StatusDot tone={sourceHealthTone(row.effective_status)} className="mt-1.5" />
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">{row.source_name}</div>
              <div className="truncate text-xs text-muted-foreground">{row.source_kind || row.source_family}</div>
            </div>
          </div>
          <HealthBadge row={row} />
        </div>
        <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
          <CompactFact label="Checked" value={formatAge(row.last_attempt_at)} />
          <CompactFact label="Data" value={formatAge(row.last_data_at)} />
          <CompactFact label="Coverage" value={`${row.item_count.toLocaleString()} items`} />
        </div>
      </button>
      {expanded ? <DetailBody row={row} /> : null}
    </div>
  );
}

function HealthBadge({ row }: { row: SourceCatalogRow }) {
  return <StatusBadge tone={sourceHealthTone(row.effective_status)}>{row.effective_status.replaceAll("_", " ")}</StatusBadge>;
}

function TimeCell({ value, inherited = false, emptyLabel = "Never checked" }: { value: string | null; inherited?: boolean; emptyLabel?: string }) {
  if (!value) return <span className="text-xs text-muted-foreground">{emptyLabel}</span>;
  return (
    <div>
      <div className="text-xs font-medium">{formatAge(value)}</div>
      <div className="truncate text-[11px] text-muted-foreground">{formatDateTime(value)}{inherited ? " · inherited" : ""}</div>
    </div>
  );
}

function Coverage({ row }: { row: SourceCatalogRow }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs tabular-nums">
      <span>{row.item_count.toLocaleString()} <span className="text-muted-foreground">items</span></span>
      <span>{row.ticker_count.toLocaleString()} <span className="text-muted-foreground">tickers</span></span>
    </div>
  );
}

function SourceDetail({ row, colSpan }: { row: SourceCatalogRow; colSpan: number }) {
  return (
    <tr className="border-t border-border bg-muted/20">
      <td colSpan={colSpan} className="px-4 py-3"><DetailBody row={row} /></td>
    </tr>
  );
}

function DetailBody({ row }: { row: SourceCatalogRow }) {
  return (
    <div className="mt-3 grid gap-3 rounded-lg border border-border bg-background p-3 text-xs md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
      <div className="space-y-1 text-muted-foreground">
        <div><span className="text-foreground">ID:</span> {row.source_id}</div>
        <div><span className="text-foreground">Mode:</span> {row.ingestion_mode || "not declared"}</div>
        <div><span className="text-foreground">Capability:</span> {row.latest_capability || "not checked"}</div>
        <div><span className="text-foreground">Run outcome:</span> {row.run_status}</div>
        <div><span className="text-foreground">Status observed:</span> {formatDateTime(row.status_at)}</div>
        <div><span className="text-foreground">Last success:</span> {formatDateTime(row.last_success_at)}</div>
        {row.capability_health.length > 1 ? (
          <div className="pt-1">
            <div className="mb-1 text-foreground">Acquisition paths</div>
            <div className="flex flex-wrap gap-1">
              {row.capability_health.map((capability) => (
                <StatusBadge key={capability.capability} tone={capability.status === "succeeded" ? "good" : capability.status === "failed" ? "bad" : "warn"}>
                  {capability.capability}: {capability.status}
                </StatusBadge>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      <div className="space-y-2">
        {row.remediation ? <div className="font-medium text-amber-700 dark:text-amber-300">{row.remediation}</div> : null}
        {row.failure_detail ? <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-2 font-mono text-[11px] text-muted-foreground">{row.failure_detail}</pre> : <div className="text-muted-foreground">No active failure detail.</div>}
      </div>
    </div>
  );
}

function CompactFact({ label, value }: { label: string; value: string }) {
  return <div><div className="text-muted-foreground">{label}</div><div className="mt-0.5 truncate font-medium">{value}</div></div>;
}

function matchesQuery(row: SourceCatalogRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [row.source_name, row.source_id, row.source_family, row.source_kind].some((value) => value.toLowerCase().includes(needle));
}

function collapsedRows(rows: SourceCatalogRow[]): SourceCatalogRow[] {
  const attention = rows.filter((row) => row.effective_status !== "healthy");
  const healthy = rows.filter((row) => row.effective_status === "healthy").slice(0, Math.max(0, 6 - attention.length));
  return [...attention, ...healthy];
}
