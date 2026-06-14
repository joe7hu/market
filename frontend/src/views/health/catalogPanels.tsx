import { ChevronDown, ChevronRight, Loader2, RefreshCw } from "lucide-react";

import type { SourceCatalogCategory, SourceCatalogProvider } from "@/api";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { PanelData } from "@/types";
import { rows } from "@/utils";
import { displayField } from "@/views/rowFormat";
import { jobDef, sourceFamilyDef, StatusDot, type SourceFamilyId } from "@/views/health/dataFlow";
import { catalogTone, type CatalogFamilyGroup } from "@/views/health/catalog";
import { baseProvider, dateMs, formatDateTime, freshnessTone, statusLabel, truncate } from "@/views/health/format";
import type { UseRefreshJobs } from "@/views/health/useRefreshJobs";
import { FragmentRow } from "@/views/health/tables";

export function CatalogControlPlane({
  families,
  data,
  expanded,
  onToggle,
  jobs,
}: {
  families: CatalogFamilyGroup[];
  data: PanelData;
  expanded: string | null;
  onToggle: (id: string) => void;
  jobs: UseRefreshJobs;
}) {
  return (
    <DataTableFrame title="Data Sources by Category">
      {families.length ? (
        <div className="space-y-5 p-4">
          {families.map((family) => {
            const Icon = sourceFamilyDef(family.id).icon;
            return (
              <div key={family.id}>
                <div className="mb-2 flex items-center gap-2">
                  <Icon className="size-4 text-muted-foreground" />
                  <h3 className="text-sm font-semibold">{family.label}</h3>
                  <StatusBadge tone={family.tone}>{statusLabel(family.tone)}</StatusBadge>
                </div>
                <div className="overflow-hidden rounded-lg border border-border">
                  <table className="w-full min-w-[960px] text-sm">
                    <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
                      <tr>
                        <th className="w-8 px-2 py-2.5" />
                        <th className="px-3 py-2.5">Category</th>
                        <th className="px-3 py-2.5">Cadence</th>
                        <th className="px-3 py-2.5">Fetcher</th>
                        <th className="px-3 py-2.5">Primary</th>
                        <th className="px-3 py-2.5">Fallback</th>
                        <th className="px-3 py-2.5">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {family.categories.map((category) => (
                        <CategoryRow
                          key={category.id}
                          category={category}
                          data={data}
                          isOpen={expanded === category.id}
                          onToggle={() => onToggle(category.id)}
                          jobs={jobs}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="flex items-center gap-2 px-4 py-6 text-sm text-muted-foreground">
          <StatusDot tone="muted" /> Source catalog not available yet. Run a refresh job to initialize it.
        </div>
      )}
    </DataTableFrame>
  );
}

function CategoryRow({
  category,
  data,
  isOpen,
  onToggle,
  jobs,
}: {
  category: SourceCatalogCategory;
  data: PanelData;
  isOpen: boolean;
  onToggle: () => void;
  jobs: UseRefreshJobs;
}) {
  const job = category.refresh_job || sourceFamilyDef(category.family as SourceFamilyId).job;
  const running = jobs.pendingJobs.has(job) || jobs.jobStates[job]?.status === "running";
  return (
    <FragmentRow>
      <tr className="border-b border-border align-top hover:bg-accent/40">
        <td className="px-2 py-3">
          <button
            type="button"
            onClick={onToggle}
            className="text-muted-foreground hover:text-foreground"
            aria-label={isOpen ? "Collapse" : "Expand"}
          >
            {isOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
        </td>
        <td className="px-3 py-3">
          <div className="flex items-center gap-2">
            <StatusDot tone={catalogTone(category.tone)} />
            <span className="font-medium">{category.label}</span>
          </div>
          {category.stale_after ? (
            <div className="mt-0.5 text-xs text-muted-foreground">stale after {category.stale_after}</div>
          ) : null}
        </td>
        <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{category.cadence_label || "-"}</td>
        <td className="px-3 py-3">
          <FetcherBadge category={category} />
        </td>
        <td className="px-3 py-3">
          {category.primary ? <ProviderChip provider={category.primary} /> : <span className="text-muted-foreground">-</span>}
        </td>
        <td className="px-3 py-3">
          {category.fallback.length ? (
            <div className="flex flex-wrap gap-1.5">
              {category.fallback.map((provider) => (
                <ProviderChip key={provider.provider} provider={provider} />
              ))}
            </div>
          ) : (
            <span className="text-muted-foreground">-</span>
          )}
        </td>
        <td className="px-3 py-3">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={running}
            onClick={() => void jobs.start(job)}
            title={`Run ${jobDef(job).label}`}
          >
            {running ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            Refresh
          </Button>
        </td>
      </tr>
      {isOpen ? (
        <tr className="border-b border-border bg-muted/30">
          <td />
          <td colSpan={6} className="px-3 py-3">
            <CategoryDrillDown category={category} data={data} job={job} />
          </td>
        </tr>
      ) : null}
    </FragmentRow>
  );
}

function FetcherBadge({ category }: { category: SourceCatalogCategory }) {
  if (category.primary?.rate_limited) {
    return <StatusBadge tone="warn">Rate-limited</StatusBadge>;
  }
  if (!category.live_fetcher) {
    return <StatusBadge tone="muted">No fetcher</StatusBadge>;
  }
  return <StatusBadge tone="good">Live</StatusBadge>;
}

function ProviderChip({ provider }: { provider: SourceCatalogProvider }) {
  const tone = catalogTone(provider.tone);
  const title = [
    `Status: ${provider.status}`,
    provider.last_observed_at ? `Updated: ${formatDateTime(provider.last_observed_at)}` : "No observation yet",
    provider.stale_after ? `Stale after: ${provider.stale_after}` : null,
    provider.detail ? truncate(provider.detail) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 text-xs"
    >
      <StatusDot tone={tone} />
      <span className="font-medium">{provider.provider}</span>
      <span className="text-muted-foreground">{provider.last_observed_at ? formatDateTime(provider.last_observed_at) : "—"}</span>
    </span>
  );
}

type FreshnessRow = { sourceKey: string; provider: string; sourceType: string; status: string; tone: ReturnType<typeof freshnessTone>; latestAt: string; staleAfter: string; detail: string };

function categoryFreshnessRows(category: SourceCatalogCategory, data: PanelData): FreshnessRow[] {
  const types = new Set(category.source_types);
  const providers = new Set(
    [category.primary?.provider, ...category.fallback.map((block) => block.provider)].filter(Boolean).map((name) => baseProvider(String(name))),
  );
  const out: FreshnessRow[] = [];
  for (const row of rows(data.sourceFreshness)) {
    const sourceType = displayField(row, ["source_type"], "");
    const provider = displayField(row, ["provider", "source_key", "source"], "unknown");
    const typeMatch = types.size ? types.has(sourceType) : false;
    const providerMatch = providers.has(baseProvider(provider));
    if (!typeMatch && !providerMatch) continue;
    out.push({
      sourceKey: displayField(row, ["source_key", "provider", "source"], provider),
      provider,
      sourceType,
      status: displayField(row, ["freshness_status", "status"], "not_loaded"),
      tone: freshnessTone(displayField(row, ["freshness_status"], ""), displayField(row, ["status"], "")),
      latestAt: displayField(row, ["last_observed_at", "checked_at"], ""),
      staleAfter: displayField(row, ["stale_after"], ""),
      detail: displayField(row, ["detail"], ""),
    });
  }
  return out.sort((a, b) => dateMs(b.latestAt) - dateMs(a.latestAt) || a.sourceKey.localeCompare(b.sourceKey));
}

function CategoryDrillDown({ category, data, job }: { category: SourceCatalogCategory; data: PanelData; job: string }) {
  const freshnessRows = categoryFreshnessRows(category, data);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <span>Refresh job: <span className="text-foreground">{jobDef(job).label}</span></span>
        <span>Cadence: <span className="text-foreground">{category.cadence_label || "-"}</span></span>
        <span>Source types: <span className="text-foreground">{category.source_types.join(", ") || "-"}</span></span>
        {category.primary?.symbol_count ? (
          <span>Symbols tracked: <span className="text-foreground tabular-nums">{category.primary.symbol_count.toLocaleString()}</span></span>
        ) : null}
      </div>
      {freshnessRows.length ? (
        <table className="w-full min-w-[680px] text-xs">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-1 pr-3">Source</th>
              <th className="py-1 pr-3">Type</th>
              <th className="py-1 pr-3">Status</th>
              <th className="py-1 pr-3">Latest</th>
              <th className="py-1 pr-3">Stale After</th>
              <th className="py-1 pr-3">Detail</th>
            </tr>
          </thead>
          <tbody>
            {freshnessRows.slice(0, 40).map((row) => (
              <tr key={row.sourceKey} className="border-t border-border/60 align-top">
                <td className="py-1 pr-3 font-medium">{row.sourceKey}</td>
                <td className="py-1 pr-3 text-muted-foreground">{row.sourceType || "-"}</td>
                <td className="py-1 pr-3"><StatusBadge tone={row.tone}>{statusLabel(row.tone)}</StatusBadge></td>
                <td className="whitespace-nowrap py-1 pr-3 text-muted-foreground">{formatDateTime(row.latestAt)}</td>
                <td className="whitespace-nowrap py-1 pr-3 text-muted-foreground">{row.staleAfter || "-"}</td>
                <td className="max-w-[320px] truncate py-1 pr-3 text-muted-foreground" title={row.detail}>{row.detail || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="text-xs text-muted-foreground">No live freshness rows for this category yet.</div>
      )}
      {freshnessRows.length > 40 ? (
        <div className="text-xs text-muted-foreground">Showing 40 of {freshnessRows.length} freshness rows (most recent first).</div>
      ) : null}
    </div>
  );
}
