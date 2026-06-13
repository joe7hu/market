import type { SourceFamilyId } from "@/views/health/dataFlow";
import type { Tone } from "@/ui/tone";

// --- Source/category health shapes -----------------------------------------

export type ProviderStat = {
  provider: string;
  tone: Tone;
  status: string;
  checks: number;
  fresh: number;
  stale: number;
  failed: number;
  latestAt: string;
  detail: string;
  items: number;
  signals: number;
  tickers: number;
};

export type Category = {
  id: string;
  label: string;
  family: SourceFamilyId;
  tone: Tone;
  total: number; // distinct providers
  fresh: number;
  stale: number;
  failed: number;
  checks: number;
  items: number;
  signals: number;
  tickers: number;
  latestAt: string;
  origins: string[];
  providers: ProviderStat[];
};

export type JobState = { status: string; startedAt?: string; finishedAt?: string | null; error?: string | null };
export type AgentRuntime = { active: boolean; configured: boolean; limit: number; timeoutSeconds: number; requestCap?: number; cadence: string };
export type AgentPipeline = {
  id: string;
  label: string;
  caption: string;
  tone: Tone;
  active: boolean;
  open: number;
  fulfilled: number;
  failed: number;
  superseded: number;
  limit: number;
  timeoutSeconds: number;
  latestAt: string;
};

export type ErrorAgg = { message: string; tone: Tone; count: number; latestAt: string; sources: string[] };

// --- Category map -----------------------------------------------------------

export type CategoryDef = { id: string; label: string; family: SourceFamilyId };

export const CATEGORY_BY_TYPE: Record<string, CategoryDef> = {
  provider_health: { id: "provider_health", label: "Provider Health", family: "market_data" },
  closing_quote: { id: "quotes", label: "Quotes", family: "market_data" },
  intraday_quote: { id: "quotes", label: "Quotes", family: "market_data" },
  crypto_quote: { id: "quotes", label: "Quotes", family: "market_data" },
  options: { id: "options", label: "Options Chains", family: "market_data" },
  fundamental: { id: "fundamentals", label: "Fundamentals", family: "market_data" },
  filing: { id: "filings", label: "Filings & Ownership", family: "filing" },
  news: { id: "news", label: "News Wires", family: "blog" },
  provider_run: { id: "ingestion_runs", label: "Source Ingestion Runs", family: "other" },
  daily: { id: "daily", label: "Daily Analyses", family: "market_data" },
  documentation: { id: "documentation", label: "Documentation", family: "other" },
  arco_thesis: { id: "social", label: "Social Graph", family: "social" },
};

export const OTHER_CATEGORY: CategoryDef = { id: "other", label: "Other", family: "other" };
export const INGESTION_RUNS_CATEGORY: CategoryDef = { id: "ingestion_runs", label: "Source Ingestion Runs", family: "other" };

// Freshness types whose providers are followed content sources (not raw data
// feeds), so they should be filed under their directory family rather than the
// generic "Source Ingestion Runs" bucket.
export const CONTENT_TYPES = new Set(["provider_run", "news", "filing", "arco_thesis"]);

// Directory source_family -> category. Ids are shared with CATEGORY_BY_TYPE so a
// followed source and its freshness rows collapse into ONE category (no
// "News Wires" vs "Followed News" duplication).
export const DIR_FAMILY_CATEGORY: Record<string, CategoryDef> = {
  news: { id: "news", label: "News Wires", family: "blog" },
  podcast: { id: "podcasts", label: "Podcasts", family: "podcast" },
  blog: { id: "blogs", label: "Blogs & Memos", family: "blog" },
  private_graph: { id: "social", label: "Social Graph", family: "social" },
  social: { id: "social", label: "Social Graph", family: "social" },
  filing: { id: "filings", label: "Filings & Ownership", family: "filing" },
  transcript: { id: "transcripts", label: "Transcripts", family: "transcript" },
  market_data: { id: "market_data", label: "Market Data Sources", family: "market_data" },
  provider: { id: "provider_health", label: "Provider Health", family: "market_data" },
  estimates: { id: "fundamentals", label: "Fundamentals", family: "market_data" },
};

export type DirEntry = { name: string; def: CategoryDef; items: number; signals: number; tickers: number; status: string; latestAt: string; detail: string };
