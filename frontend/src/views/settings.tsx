import { Clock3, MessageCircle, Save, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { loadSettings, updateResearchSources, type ResearchSourcesInput } from "@/api";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useMarketData } from "@/marketData";
import type { SettingsPayload } from "@/types";
import type { Tone } from "@/ui/tone";
import { titleLabel, toneFromText } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "./workspacePage";

type ResearchForm = {
  xEnabled: boolean;
  listId: string;
  priorityHandles: string;
  xLimit: number;
  newsEnabled: boolean;
  newsProviders: string;
  blogsEnabled: boolean;
  substackUrls: string;
  rssUrls: string;
};

const EMPTY_RESEARCH: ResearchForm = {
  xEnabled: true,
  listId: "",
  priorityHandles: "balajis, karpathy, citrini, BillAckman, dylan522p, IncomeSharks",
  xLimit: 30,
  newsEnabled: true,
  newsProviders: "bloomberg, reuters, google-news, hackernews",
  blogsEnabled: true,
  substackUrls: "",
  rssUrls: "",
};

export function SettingsPage() {
  const { data, loadScope } = useMarketData();
  const [directSettings, setDirectSettings] = useState<SettingsPayload | null>(null);
  const settings = directSettings ?? data.settings;
  const statusMetadata = jsonRecord(data.dashboard.status?.metadata);
  const statusConfig = jsonRecord(statusMetadata.config);
  const scheduler = jsonRecord(settings.agents?.scheduler ?? statusMetadata.scheduler);
  const settingsReady = Boolean(directSettings || settings.config || statusConfig.research_sources);

  const researchConfig = useMemo(
    () => researchFromSettings(settings.config?.research_sources ?? statusConfig.research_sources),
    [settings.config, statusConfig.research_sources],
  );
  const [researchDraft, setResearchDraft] = useState<ResearchForm | null>(null);
  const [savingSources, setSavingSources] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const research = researchDraft ?? researchConfig;

  useEffect(() => {
    let cancelled = false;
    void loadSettings()
      .then((payload) => {
        if (!cancelled) setDirectSettings(payload);
      })
      .catch((exc) => {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "Failed to load settings");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const metrics: MetricSpec[] = [
    ["X list", research.listId ? "Configured" : "Not set", research.xEnabled ? "enabled" : "paused", research.listId && research.xEnabled ? "good" : research.xEnabled ? "warn" : "muted"],
    ["News", research.newsEnabled ? "Enabled" : "Paused", `${splitList(research.newsProviders).length} providers`, research.newsEnabled ? "good" : "muted"],
    ["Blogs", research.blogsEnabled ? "Enabled" : "Paused", `${splitList(research.substackUrls).length + splitList(research.rssUrls).length} sources`, research.blogsEnabled ? "good" : "muted"],
    ["Social cadence", `${stringFromJson(scheduler.social_refresh_seconds, "1800")}s`, "X pull interval", "info"],
  ];

  const saveSources = async () => {
    setSavingSources(true);
    setError("");
    setMessage("");
    const payload: ResearchSourcesInput = {
      x: { enabled: research.xEnabled, list_id: research.listId, priority_handles: splitList(research.priorityHandles), limit: research.xLimit },
      news: { enabled: research.newsEnabled, providers: splitList(research.newsProviders) },
      blogs: { enabled: research.blogsEnabled, substack_urls: splitList(research.substackUrls), rss_urls: splitList(research.rssUrls) },
    };
    try {
      const saved = await updateResearchSources(payload);
      setDirectSettings(saved);
      await loadScope("settings");
      setResearchDraft(null);
      setMessage("Research & social sources saved to config.yaml.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to save research sources");
    } finally {
      setSavingSources(false);
    }
  };

  return (
    <WorkspacePage
      eyebrow="Configuration"
      title="Source Control"
      subtitle="Live X / news / blog source pulls. (The option agent has its own control plane on the Agent page.)"
      metrics={metrics}
    >
      {message ? <InlineNotice tone="good">{message}</InlineNotice> : null}
      {error ? <InlineNotice tone="bad">{error}</InlineNotice> : null}

      <ResearchSourcesCard value={research} loaded={settingsReady} saving={savingSources} onChange={setResearchDraft} onSave={() => void saveSources()} />
      <SourceInventory
        rows={settings.sources?.rows ?? []}
        draft={research}
        loaded={settingsReady}
        onRemove={(row) => setResearchDraft(removeConfiguredSource(research, row))}
      />

      <RuntimePanel scheduler={scheduler} />
    </WorkspacePage>
  );
}

function ResearchSourcesCard({
  value,
  loaded,
  saving,
  onChange,
  onSave,
}: {
  value: ResearchForm;
  loaded: boolean;
  saving: boolean;
  onChange: (value: ResearchForm) => void;
  onSave: () => void;
}) {
  const xConfigured = value.xEnabled && Boolean(value.listId);
  const tone: Tone = !loaded ? "muted" : xConfigured ? "good" : value.xEnabled ? "warn" : "muted";
  return (
    <DataTableFrame
      title="Research & Social Sources"
      action={
        <div className="flex items-center gap-2">
          <StatusBadge tone={tone}>{!loaded ? "Loading" : xConfigured ? "X list set" : value.xEnabled ? "Needs list ID" : "Paused"}</StatusBadge>
          <Button type="button" size="sm" variant="outline" onClick={onSave} disabled={saving || !loaded}>
            <Save className={saving ? "animate-pulse" : undefined} />
            {saving ? "Saving" : "Save sources"}
          </Button>
        </div>
      }
    >
      <div className="space-y-5 p-4">
        <div className="flex items-start gap-3">
          <div className="rounded-md border border-border bg-muted p-2"><MessageCircle className="size-5 text-muted-foreground" /></div>
          <p className="text-sm leading-6 text-muted-foreground">
            Continuous coverage of important X accounts (beyond Arco bookmarks) plus news and blog ingestion via opencli. The X list is
            fetched in one paced call; per-account fetches are the staggered fallback.
          </p>
        </div>

        <div className="space-y-4 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">X / Social</span>
            <Toggle checked={value.xEnabled} disabled={!loaded} onChange={(next) => onChange({ ...value, xEnabled: next })} />
          </div>
          <Field label="X list ID" detail="Numeric ID of a curated X/Twitter list. Leave empty to use per-account fetch only.">
            <Input value={value.listId} onChange={(event) => onChange({ ...value, listId: event.target.value })} placeholder="e.g. 1734567890123456789" />
          </Field>
          <Field label="Priority handles" detail="Comma-separated X handles for the per-account fallback (the @ is optional).">
            <Input value={value.priorityHandles} onChange={(event) => onChange({ ...value, priorityHandles: event.target.value })} placeholder="balajis, karpathy, citrini" />
          </Field>
          <Field label="Tweets per fetch" detail="Max tweets per list/account call.">
            <Input type="number" min={1} max={200} value={value.xLimit} onChange={(event) => onChange({ ...value, xLimit: boundedNumber(event.target.value, 1, 200) })} />
          </Field>
        </div>

        <div className="space-y-4 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">News</span>
            <Toggle checked={value.newsEnabled} disabled={!loaded} onChange={(next) => onChange({ ...value, newsEnabled: next })} />
          </div>
          <Field label="Providers" detail="Comma-separated opencli news adapters.">
            <Input value={value.newsProviders} onChange={(event) => onChange({ ...value, newsProviders: event.target.value })} placeholder="bloomberg, reuters, google-news, hackernews" />
          </Field>
        </div>

        <div className="space-y-4 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">Blogs & Memos</span>
            <Toggle checked={value.blogsEnabled} disabled={!loaded} onChange={(next) => onChange({ ...value, blogsEnabled: next })} />
          </div>
          <Field label="Substack URLs" detail="Comma- or newline-separated Substack publication URLs.">
            <Input value={value.substackUrls} onChange={(event) => onChange({ ...value, substackUrls: event.target.value })} placeholder="https://www.example.substack.com" />
          </Field>
          <Field label="RSS URLs" detail="Comma- or newline-separated RSS/Atom feed URLs for non-Substack sites.">
            <Input value={value.rssUrls} onChange={(event) => onChange({ ...value, rssUrls: event.target.value })} placeholder="https://example.com/feed.xml" />
          </Field>
        </div>
      </div>
    </DataTableFrame>
  );
}

function SourceInventory({
  rows,
  draft,
  loaded,
  onRemove,
}: {
  rows: Record<string, unknown>[];
  draft: ResearchForm;
  loaded: boolean;
  onRemove: (row: Record<string, unknown>) => void;
}) {
  const draftRows = useMemo(() => mergeDraftSources(rows, draft), [rows, draft]);
  return (
    <DataTableFrame title="Configured Source Inventory" action={<StatusBadge tone={loaded ? "info" : "muted"}>{draftRows.length} configured</StatusBadge>}>
      <table className="w-full min-w-[860px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Source</th>
            <th className="px-3 py-3">Kind</th>
            <th className="px-3 py-3">Latest</th>
            <th className="px-3 py-3">Items</th>
            <th className="px-3 py-3">Tickers</th>
            <th className="px-3 py-3">Runs</th>
            <th className="px-3 py-3">Config</th>
            <th className="px-3 py-3 text-right">Action</th>
          </tr>
        </thead>
        <tbody>
          {draftRows.length ? draftRows.map((row, index) => {
            const status = stringFromJson(row.latest_status, "configured");
            const removable = booleanFromJson(row.removable, true);
            return (
              <tr key={`${stringFromJson(row.config_path, "source")}:${stringFromJson(row.value, String(index))}`} className="border-b border-border align-top hover:bg-accent/40">
                <td className="px-3 py-3">
                  <div className="font-medium">{stringFromJson(row.label, "Source")}</div>
                  <div className="mt-1 max-w-[260px] break-all text-xs text-muted-foreground">{stringFromJson(row.value, "")}</div>
                </td>
                <td className="px-3 py-3"><StatusBadge tone="muted">{titleLabel(stringFromJson(row.kind, "source"))}</StatusBadge></td>
                <td className="px-3 py-3">
                  <StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge>
                  <div className="mt-1 text-xs text-muted-foreground">{shortDate(row.latest_finished_at)}</div>
                </td>
                <td className="px-3 py-3 tabular-nums">{numberFromJson(row.latest_item_count, 0)}</td>
                <td className="px-3 py-3 tabular-nums">{numberFromJson(row.latest_ticker_count, 0)}</td>
                <td className="px-3 py-3 tabular-nums">{numberFromJson(row.observed_run_count, 0)}</td>
                <td className="px-3 py-3 text-xs text-muted-foreground">{stringFromJson(row.config_path, "")}</td>
                <td className="px-3 py-3 text-right">
                  <Button type="button" size="sm" variant="outline" disabled={!loaded || !removable} onClick={() => onRemove(row)}>
                    <Trash2 />
                    Delete
                  </Button>
                </td>
              </tr>
            );
          }) : (
            <tr>
              <td colSpan={8} className="px-3 py-8 text-center text-muted-foreground">No configured research sources.</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="border-t border-border px-4 py-3 text-xs text-muted-foreground">
        Delete removes the source from the editable config draft. Use Save sources to persist the change.
      </div>
    </DataTableFrame>
  );
}

function RuntimePanel({ scheduler }: { scheduler: Record<string, unknown> }) {
  const rows = [
    ["Radar refresh", stringFromJson(scheduler.radar_refresh_seconds, "900"), "fast deterministic signal cadence"],
    ["Source refresh", stringFromJson(scheduler.source_refresh_seconds, "3600"), "option-chain/source pull cadence"],
    ["Learning refresh", stringFromJson(scheduler.learning_refresh_seconds, "21600"), "heavy deterministic learning cadence"],
    ["Social refresh", stringFromJson(scheduler.social_refresh_seconds, "1800"), "X / social pull cadence"],
    ["Research refresh", stringFromJson(scheduler.research_refresh_seconds, "3600"), "news / blogs pull cadence"],
    ["Market environment", stringFromJson(scheduler.market_environment_refresh_seconds, "3600"), "valuation and asset-matrix cadence"],
    ["Radar source", stringFromJson(scheduler.radar_option_source, "robinhood"), "option source used by scheduler"],
  ];
  return (
    <DataTableFrame title="Data Refresh Cadence" action={<StatusBadge tone="info">read-only env</StatusBadge>}>
      <table className="w-full min-w-[640px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Control</th>
            <th className="px-3 py-3">Value</th>
            <th className="px-3 py-3">Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, value, source]) => (
            <tr key={label} className="border-b border-border align-top hover:bg-accent/40">
              <td className="px-3 py-3">
                <div className="flex items-center gap-2 font-medium"><Clock3 className="size-4 text-muted-foreground" /> {label}</div>
              </td>
              <td className="px-3 py-3"><StatusBadge tone={toneFromText(value)}>{titleLabel(value || "default")}</StatusBadge></td>
              <td className="px-3 py-3 text-muted-foreground">{source}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function Toggle({ checked, disabled, onChange }: { checked: boolean; disabled: boolean; onChange: (next: boolean) => void }) {
  return <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} className="size-5 accent-primary" />;
}

function Field({ label, detail, children }: { label: string; detail: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="flex items-center gap-2 text-sm font-medium"><SlidersHorizontal className="size-4 text-muted-foreground" /> {label}</span>
      {children}
      <span className="block text-xs text-muted-foreground">{detail}</span>
    </label>
  );
}

function InlineNotice({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <div className="rounded-md border border-border bg-card px-4 py-3 text-sm"><StatusBadge tone={tone}>{children}</StatusBadge></div>;
}

function researchFromSettings(value: unknown): ResearchForm {
  const research = jsonRecord(value);
  const x = jsonRecord(research.x);
  const news = jsonRecord(research.news);
  const blogs = jsonRecord(research.blogs);
  return {
    xEnabled: booleanFromJson(x.enabled, EMPTY_RESEARCH.xEnabled),
    listId: stringFromJson(x.list_id, EMPTY_RESEARCH.listId),
    priorityHandles: joinList(x.priority_handles, EMPTY_RESEARCH.priorityHandles),
    xLimit: numberFromJson(x.limit, EMPTY_RESEARCH.xLimit),
    newsEnabled: booleanFromJson(news.enabled, EMPTY_RESEARCH.newsEnabled),
    newsProviders: joinList(news.providers, EMPTY_RESEARCH.newsProviders),
    blogsEnabled: booleanFromJson(blogs.enabled, EMPTY_RESEARCH.blogsEnabled),
    substackUrls: joinList(blogs.substack_urls, ""),
    rssUrls: joinList(blogs.rss_urls, ""),
  };
}

function removeConfiguredSource(form: ResearchForm, row: Record<string, unknown>): ResearchForm {
  const value = stringFromJson(row.value, "");
  const path = stringFromJson(row.config_path, "");
  if (!value) return form;
  if (path.endsWith(".list_id")) return { ...form, listId: "" };
  if (path.endsWith(".priority_handles")) return { ...form, priorityHandles: removeFromList(form.priorityHandles, value) };
  if (path.endsWith(".providers")) return { ...form, newsProviders: removeFromList(form.newsProviders, value) };
  if (path.endsWith(".substack_urls")) return { ...form, substackUrls: removeFromList(form.substackUrls, value) };
  if (path.endsWith(".rss_urls")) return { ...form, rssUrls: removeFromList(form.rssUrls, value) };
  return form;
}

function mergeDraftSources(rows: Record<string, unknown>[], form: ResearchForm): Record<string, unknown>[] {
  const byKey = new Map(rows.map((row) => [`${stringFromJson(row.config_path, "")}:${stringFromJson(row.value, "")}`, row]));
  const ensure = (configPath: string, values: string[], kind: string, family: string) => {
    for (const value of values) {
      const key = `${configPath}:${value}`;
      if (byKey.has(key)) continue;
      byKey.set(key, {
        source_id: "",
        family,
        kind,
        label: kind === "x_handle" ? `@${value}` : hostLabel(value),
        value,
        config_path: configPath,
        removable: true,
        enabled: true,
        latest_status: "unsaved",
        latest_item_count: 0,
        latest_ticker_count: 0,
        observed_run_count: 0,
      });
    }
  };
  if (form.listId) ensure("research_sources.x.list_id", [form.listId], "x_list", "x");
  ensure("research_sources.x.priority_handles", splitList(form.priorityHandles), "x_handle", "x");
  ensure("research_sources.news.providers", splitList(form.newsProviders), "news_provider", "news");
  ensure("research_sources.blogs.substack_urls", splitList(form.substackUrls), "substack", "blog");
  ensure("research_sources.blogs.rss_urls", splitList(form.rssUrls), "rss", "blog");
  return Array.from(byKey.values()).filter((row) => {
    const value = stringFromJson(row.value, "");
    const path = stringFromJson(row.config_path, "");
    if (path.endsWith(".list_id")) return form.listId === value;
    if (path.endsWith(".priority_handles")) return splitList(form.priorityHandles).includes(value);
    if (path.endsWith(".providers")) return splitList(form.newsProviders).includes(value);
    if (path.endsWith(".substack_urls")) return splitList(form.substackUrls).includes(value);
    if (path.endsWith(".rss_urls")) return splitList(form.rssUrls).includes(value);
    return true;
  });
}

function removeFromList(value: string, target: string): string {
  return splitList(value).filter((item) => item !== target).join(", ");
}

function hostLabel(value: string): string {
  try {
    return new URL(value).host.replace(/^www\./, "") || value;
  } catch {
    return value;
  }
}

function shortDate(value: unknown): string {
  const text = stringFromJson(value, "");
  if (!text) return "";
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return parsed.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function splitList(value: string): string[] {
  const out: string[] = [];
  for (const raw of value.split(/[\n,]+/)) {
    const token = raw.trim().replace(/^@/, "");
    if (token && !out.includes(token)) out.push(token);
  }
  return out;
}

function joinList(value: unknown, fallback: string): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  if (typeof value === "string") return value;
  return fallback;
}

function boundedNumber(value: string, min: number, max: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return min;
  return Math.max(min, Math.min(max, Math.round(parsed)));
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
