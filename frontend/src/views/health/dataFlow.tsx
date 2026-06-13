import {
  Banknote,
  CalendarClock,
  Database,
  FileText,
  Mic,
  Newspaper,
  Radio,
  Share2,
  TrendingUp,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { Tone } from "@/ui/tone";

// --- Source families -------------------------------------------------------

export type SourceFamilyId =
  | "filing"
  | "transcript"
  | "podcast"
  | "blog"
  | "social"
  | "market_data"
  | "broker"
  | "events"
  | "other";

export type SourceFamilyDef = {
  id: SourceFamilyId;
  label: string;
  icon: typeof Database;
  /** Refresh job that re-pulls this family from its upstream sources. */
  job: string;
};

export const SOURCE_FAMILIES: SourceFamilyDef[] = [
  { id: "filing", label: "Filings & Disclosures", icon: FileText, job: "update_disclosures" },
  { id: "transcript", label: "Transcripts", icon: Newspaper, job: "update_free_sources" },
  { id: "podcast", label: "Podcasts", icon: Radio, job: "update_free_sources" },
  { id: "blog", label: "Blogs & Memos", icon: Newspaper, job: "update_free_sources" },
  { id: "social", label: "Social Graph (X / Arco)", icon: Share2, job: "update_arco_data" },
  { id: "market_data", label: "Market & Options Data", icon: TrendingUp, job: "update_free_sources_radar" },
  { id: "broker", label: "Broker", icon: Banknote, job: "update_broker_sources" },
  { id: "events", label: "Event Calendar", icon: CalendarClock, job: "update_event_calendar" },
  { id: "other", label: "Other", icon: Database, job: "full_market_refresh" },
];

const FAMILY_BY_ID = new Map(SOURCE_FAMILIES.map((family) => [family.id, family]));

export function sourceFamilyDef(id: SourceFamilyId): SourceFamilyDef {
  return FAMILY_BY_ID.get(id) ?? SOURCE_FAMILIES[SOURCE_FAMILIES.length - 1];
}

/** Classify a source into a family from its name/kind/type text. */
export function classifyFamily(...hints: string[]): SourceFamilyId {
  const text = hints.join(" ").toLowerCase();
  if (/broker|ibkr|account|position|paper/.test(text)) return "broker";
  if (/social|arco|tweet|twitter|\bx\b|reddit/.test(text)) return "social";
  if (/filing|disclosure|edgar|13[fdg]|sec|insider|holding/.test(text)) return "filing";
  if (/transcript|earnings call|conference call/.test(text)) return "transcript";
  if (/podcast|episode|audio/.test(text)) return "podcast";
  if (/blog|newsletter|memo|substack|letter/.test(text)) return "blog";
  if (/event|calendar|catalyst|earnings_date|schedule/.test(text)) return "events";
  if (/market|quote|price|option|chain|tradingview|yfinance|coingecko|defillama|provider|estimate|technical|valuation/.test(text)) {
    return "market_data";
  }
  return "other";
}

// --- Job catalog -----------------------------------------------------------

export type JobGroup = "ingestion" | "synthesis" | "full";

export type JobDef = {
  label: string;
  description: string;
  group: JobGroup;
};

export const JOB_CATALOG: Record<string, JobDef> = {
  full_market_refresh: {
    label: "Full Market Refresh",
    description: "End-to-end: pull every source, then rematerialize models and signals.",
    group: "full",
  },
  update_free_sources: {
    label: "Free Sources",
    description: "Filings, transcripts, podcasts, blogs, and source analyses.",
    group: "ingestion",
  },
  update_free_sources_radar: {
    label: "Radar Source Pull",
    description: "TradingView option chains/quotes plus yfinance OI/volume liquidity.",
    group: "ingestion",
  },
  update_ibkr_options: {
    label: "IBKR Option Chains",
    description: "Price, greeks, OI and volume pulled directly from IBKR.",
    group: "ingestion",
  },
  update_arco_data: {
    label: "Social Graph (X / Arco)",
    description: "Private social-graph signal pull.",
    group: "ingestion",
  },
  update_disclosures: {
    label: "Disclosures & Filings",
    description: "SEC filings and 13F holdings ingestion.",
    group: "ingestion",
  },
  update_broker_sources: {
    label: "Broker Sync",
    description: "Broker accounts, positions, and market snapshots.",
    group: "ingestion",
  },
  update_event_calendar: {
    label: "Event Calendar",
    description: "Earnings and catalyst calendar refresh.",
    group: "ingestion",
  },
  refresh_decision_models: {
    label: "Decision Models",
    description: "Recompute decision-readiness models from current data.",
    group: "synthesis",
  },
  daily_screen: {
    label: "Daily Screen",
    description: "Re-run the daily universe screen (offline).",
    group: "synthesis",
  },
  hourly_options_radar: {
    label: "Hourly Options Radar",
    description: "Hourly options radar refresh.",
    group: "synthesis",
  },
  premarket_options_intelligence: {
    label: "Premarket Options Intel",
    description: "Premarket options intelligence pass.",
    group: "synthesis",
  },
  refresh_options_radar: {
    label: "Options Radar (Full)",
    description: "Full radar rematerialization including agents.",
    group: "synthesis",
  },
  refresh_options_radar_deterministic: {
    label: "Options Radar (Deterministic)",
    description: "Deterministic radar math, gates, and ranking — no agents.",
    group: "synthesis",
  },
  refresh_options_radar_signal: {
    label: "Options Radar (Signal)",
    description: "Fast fresh-signal rematerialization for the continuous loop.",
    group: "synthesis",
  },
  refresh_options_radar_signal_ibkr: {
    label: "Options Radar (Signal · IBKR)",
    description: "Fast signal refresh from the reliable IBKR chains only.",
    group: "synthesis",
  },
  run_option_agents: {
    label: "Option Agents",
    description: "Run the Codex thesis/postmortem option agents.",
    group: "synthesis",
  },
};

export function jobDef(jobName: string): JobDef {
  return (
    JOB_CATALOG[jobName] ?? {
      label: titleCase(jobName),
      description: "Allowlisted refresh job.",
      group: "ingestion",
    }
  );
}

function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

// --- Tone helpers ----------------------------------------------------------

export function toneRank(tone: Tone): number {
  return tone === "bad" ? 0 : tone === "warn" ? 1 : tone === "info" ? 2 : tone === "good" ? 3 : 4;
}

/** Worst (most-degraded) tone across a set. Empty -> muted. */
export function worstTone(tones: Tone[]): Tone {
  if (!tones.length) return "muted";
  // Seed from the first element (not "good") so an all-muted set stays muted.
  return tones.reduce((worst, tone) => (toneRank(tone) < toneRank(worst) ? tone : worst));
}

const DOT_CLASS: Record<Tone, string> = {
  good: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-500",
  info: "bg-sky-500",
  muted: "bg-slate-400",
};

export function StatusDot({ tone, className }: { tone: Tone; className?: string }) {
  return <span className={cn("inline-block size-2.5 shrink-0 rounded-full", DOT_CLASS[tone], className)} aria-hidden />;
}

const CHIP_TONE: Record<Tone, string> = {
  good: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  warn: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  bad: "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300",
  info: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
  muted: "border-border bg-muted/60 text-muted-foreground",
};

// --- Data-flow diagram -----------------------------------------------------

export type FlowChip = { label: string; tone: Tone; hint?: string };
export type FlowStage = { id: string; title: string; caption: string; tone: Tone; chips: FlowChip[] };

export type FamilyHealth = {
  id: SourceFamilyId;
  label: string;
  tone: Tone;
  total: number;
  healthy: number;
};

/**
 * Build the five pipeline stages. Upstream health propagates downstream: a
 * failing source family degrades the synthesis and ticker-signal stages it
 * feeds, so the diagram reflects how a broken source contributes to signals.
 */
export function buildFlowStages(families: FamilyHealth[]): FlowStage[] {
  const present = families.filter((family) => family.total > 0);
  const sourcesTone = worstTone(present.map((family) => family.tone));

  const sourceChips: FlowChip[] = present.map((family) => ({
    label: family.label,
    tone: family.tone,
    hint: `${family.healthy}/${family.total} healthy`,
  }));

  const jobChips: FlowChip[] = dedupeChips(
    present.map((family) => ({
      label: jobDef(sourceFamilyDef(family.id).job).label,
      tone: family.tone,
    })),
  );

  // Downstream stages can only be as healthy as the data feeding them.
  const downstreamTone = sourcesTone;

  return [
    {
      id: "sources",
      title: "External Sources",
      caption: "Independent providers contributing evidence",
      tone: sourcesTone,
      chips: sourceChips.length ? sourceChips : [{ label: "No sources loaded", tone: "muted" }],
    },
    {
      id: "ingestion",
      title: "Ingestion Jobs",
      caption: "Scheduled pulls into the canonical store",
      tone: sourcesTone,
      chips: jobChips.length ? jobChips : [{ label: "No jobs mapped", tone: "muted" }],
    },
    {
      id: "store",
      title: "Canonical Store",
      caption: "Deduped items, provider runs, freshness",
      tone: downstreamTone,
      chips: [
        { label: "source_items", tone: downstreamTone },
        { label: "provider_runs", tone: downstreamTone },
        { label: "source_freshness", tone: downstreamTone },
      ],
    },
    {
      id: "synthesis",
      title: "Signal Synthesis",
      caption: "Consensus, rankings, ticker links",
      tone: downstreamTone,
      chips: [
        { label: "source_consensus", tone: downstreamTone },
        { label: "ticker_rankings", tone: downstreamTone },
        { label: "ticker_source_signals", tone: downstreamTone },
      ],
    },
    {
      id: "decisions",
      title: "Per-Ticker Signals",
      caption: "What each ticker's decision sees",
      tone: downstreamTone,
      chips: [
        { label: "decision_snapshots", tone: downstreamTone },
        { label: "options_radar", tone: downstreamTone },
        { label: "decision_queue", tone: downstreamTone },
      ],
    },
  ];
}

function dedupeChips(chips: FlowChip[]): FlowChip[] {
  const byLabel = new Map<string, FlowChip>();
  for (const chip of chips) {
    const existing = byLabel.get(chip.label);
    if (!existing || toneRank(chip.tone) < toneRank(existing.tone)) {
      byLabel.set(chip.label, chip);
    }
  }
  return [...byLabel.values()];
}

export function DataFlowDiagram({ stages }: { stages: FlowStage[] }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Data Flow</h2>
          <p className="text-sm text-muted-foreground">
            How sources flow into the canonical store and contribute to each ticker's signals. Stage color reflects the
            health of the data feeding it.
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-2 lg:flex-row lg:items-stretch">
        {stages.map((stage, index) => (
          <div key={stage.id} className="flex flex-1 items-stretch gap-2">
            <StageCard stage={stage} />
            {index < stages.length - 1 ? <FlowArrow /> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function StageCard({ stage }: { stage: FlowStage }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col rounded-lg border border-border bg-background p-3">
      <div className="mb-1 flex items-center gap-2">
        <StatusDot tone={stage.tone} />
        <span className="truncate text-sm font-semibold">{stage.title}</span>
      </div>
      <p className="mb-2 text-xs leading-5 text-muted-foreground">{stage.caption}</p>
      <div className="mt-auto flex flex-wrap gap-1">
        {stage.chips.map((chip, index) => (
          <span
            key={`${chip.label}-${index}`}
            title={chip.hint}
            className={cn(
              "rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-4",
              CHIP_TONE[chip.tone],
            )}
          >
            {chip.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function FlowArrow() {
  return (
    <div className="flex shrink-0 items-center justify-center text-muted-foreground" aria-hidden>
      <span className="hidden lg:inline">→</span>
      <span className="inline lg:hidden">↓</span>
    </div>
  );
}
