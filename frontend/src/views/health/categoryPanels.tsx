import { AlertTriangle, ChevronDown, ChevronRight, Loader2, RefreshCw } from "lucide-react";

import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { jobDef, sourceFamilyDef, StatusDot } from "@/views/health/dataFlow";
import { titleLabel } from "@/views/rowFormat";
import { formatDateTime, statusLabel } from "@/views/health/format";
import type { Category, ErrorAgg } from "@/views/health/types";
import type { UseRefreshJobs } from "@/views/health/useRefreshJobs";
import { FragmentRow } from "@/views/health/tables";

export function TopErrorsPanel({ errors }: { errors: ErrorAgg[] }) {
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

export function CategoryControlPlane({
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
