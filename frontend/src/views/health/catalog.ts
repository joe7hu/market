import type { SourceCatalogCategory, SourceCatalogProvider, SourceCatalogTone } from "@/api";
import type { PanelData } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import { sourceFamilyDef, type SourceFamilyId } from "@/views/health/dataFlow";
import { booleanFromJson, jsonRecord, numberFromJson, stringFromJson } from "@/views/health/format";

/** Map the backend catalog tone vocabulary onto the UI Tone system. */
export function catalogTone(tone: SourceCatalogTone | string | undefined): Tone {
  switch (tone) {
    case "good":
      return "good";
    case "warn":
      return "warn";
    case "bad":
      return "bad";
    default:
      return "muted"; // neutral | unknown | undefined
  }
}

function parseProvider(raw: unknown): SourceCatalogProvider | null {
  const record = jsonRecord(raw);
  const provider = stringFromJson(record.provider, "");
  if (!provider) return null;
  return {
    provider,
    status: stringFromJson(record.status, "unknown"),
    tone: stringFromJson(record.tone, "unknown") as SourceCatalogTone,
    provider_status: stringFromJson(record.provider_status, "unknown"),
    last_observed_at: typeof record.last_observed_at === "string" ? record.last_observed_at : null,
    stale_after: stringFromJson(record.stale_after, ""),
    symbol_count: numberFromJson(record.symbol_count, 0),
    rate_limited: booleanFromJson(record.rate_limited, false),
    freshness_status: stringFromJson(record.freshness_status, "unknown"),
    detail: stringFromJson(record.detail, ""),
  };
}

/** Parse the `sourceCatalog` snapshot rows (category objects) into typed categories. */
export function parseSourceCatalog(data: PanelData): SourceCatalogCategory[] {
  return rows(data.sourceCatalog).map((row) => {
    const record = jsonRecord(row);
    const sourceTypes = Array.isArray(record.source_types)
      ? record.source_types.map((value) => stringFromJson(value, "")).filter(Boolean)
      : [];
    const fallback = Array.isArray(record.fallback)
      ? record.fallback.map(parseProvider).filter((block): block is SourceCatalogProvider => block !== null)
      : [];
    return {
      id: stringFromJson(record.id, ""),
      label: stringFromJson(record.label, "Unknown"),
      family: stringFromJson(record.family, "other"),
      cadence_label: stringFromJson(record.cadence_label, ""),
      cadence_seconds: numberFromJson(record.cadence_seconds, 0),
      refresh_job: stringFromJson(record.refresh_job, ""),
      stale_after: stringFromJson(record.stale_after, ""),
      source_types: sourceTypes,
      live_fetcher: booleanFromJson(record.live_fetcher, false),
      tone: stringFromJson(record.tone, "unknown") as SourceCatalogTone,
      primary: parseProvider(record.primary),
      fallback,
    };
  });
}

export type CatalogFamilyGroup = {
  id: SourceFamilyId;
  label: string;
  tone: Tone;
  categories: SourceCatalogCategory[];
};

const FAMILY_ORDER: Record<string, number> = {
  market_data: 0,
  social: 1,
  blog: 2,
  filing: 3,
  podcast: 4,
  broker: 5,
  other: 6,
};

const TONE_RANK: Record<Tone, number> = { bad: 0, warn: 1, info: 2, good: 3, muted: 4 };

function worstCatalogTone(categories: SourceCatalogCategory[]): Tone {
  let worst: Tone = "muted";
  for (const category of categories) {
    const tone = catalogTone(category.tone);
    if (TONE_RANK[tone] < TONE_RANK[worst]) worst = tone;
  }
  return worst;
}

/** Group catalog categories under their family, ordered for display. */
export function groupCatalogByFamily(categories: SourceCatalogCategory[]): CatalogFamilyGroup[] {
  const byFamily = new Map<string, SourceCatalogCategory[]>();
  for (const category of categories) {
    const list = byFamily.get(category.family) ?? [];
    list.push(category);
    byFamily.set(category.family, list);
  }
  return [...byFamily.entries()]
    .map(([id, list]) => ({
      id: id as SourceFamilyId,
      label: sourceFamilyDef(id as SourceFamilyId).label,
      tone: worstCatalogTone(list),
      categories: list,
    }))
    .sort((a, b) => (FAMILY_ORDER[a.id] ?? 99) - (FAMILY_ORDER[b.id] ?? 99) || a.label.localeCompare(b.label));
}

/** Roll catalog tones up to top-level fresh/stale/failed metric counts. */
export function catalogToneCounts(categories: SourceCatalogCategory[]): { fresh: number; stale: number; failed: number } {
  let fresh = 0;
  let stale = 0;
  let failed = 0;
  for (const category of categories) {
    const tone = catalogTone(category.tone);
    if (tone === "good") fresh += 1;
    else if (tone === "warn") stale += 1;
    else if (tone === "bad") failed += 1;
  }
  return { fresh, stale, failed };
}
