import type { ReactNode } from "react";

import type { PanelData, RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import type { MetricSpec } from "@/views/workspacePage";

export type DatasetProfileId = "watchlist" | "sources" | "superinvestors" | "market" | "research" | "filings" | "calendar";

export type DatasetSection = {
  title: string;
  rows: RowRecord[];
};

export type DatasetProfile = {
  id: DatasetProfileId;
  title: string;
  eyebrow: string;
  subtitle: string;
  sections: DatasetSection[];
  metrics: MetricSpec[];
};

type SectionSpec = {
  title: string;
  getRows: (data: PanelData) => RowRecord[];
};

type ProfileSpec = {
  title: string;
  eyebrow: string;
  subtitle: string;
  sections: SectionSpec[];
  metricLabels: Array<[string, string, string, Tone]>;
};

const profileSpecs: Record<DatasetProfileId, ProfileSpec> = {
  watchlist: {
    title: "Watchlist",
    eyebrow: "Market data",
    subtitle: "Prices, watchlists, and ranked universe context.",
    sections: [
      { title: "Quotes", getRows: (data) => rows(data.quotes) },
      { title: "TradingView Watchlists", getRows: (data) => rows(data.tradingviewWatchlists) },
      { title: "Universe Screen", getRows: (data) => rows(data.universeScreen) },
    ],
    metricLabels: [
      ["Quote Coverage", "Quotes", "priced names available", "good"],
      ["Watchlists", "TradingView Watchlists", "TradingView lists loaded", "info"],
      ["Universe", "Universe Screen", "rankable names", "info"],
    ],
  },
  sources: {
    title: "Sources",
    eyebrow: "Evidence",
    subtitle: "Ticker-ranked source evidence, raw signal ledgers, source coverage, and opportunity inputs.",
    sections: [
      { title: "Ticker Source Rankings", getRows: (data) => rows(data.sourceTickerRankings) },
      { title: "Ticker Source Signals", getRows: (data) => rows(data.tickerSourceSignals) },
      { title: "Source Items", getRows: (data) => rows(data.sourceItems) },
      { title: "Source Consensus", getRows: (data) => rows(data.sourceConsensus) },
      { title: "Opportunity Evidence", getRows: (data) => rows(data.opportunitySources) },
      { title: "Thesis Evidence", getRows: (data) => rows(data.theses) },
      { title: "News", getRows: (data) => rows(data.news) },
      { title: "Source Directory", getRows: (data) => rows(data.sources) },
    ],
    metricLabels: [
      ["Ranked Tickers", "Ticker Source Rankings", "source-backed ticker coverage", "good"],
      ["Signal Rows", "Ticker Source Signals", "raw ticker evidence", "info"],
      ["Source Items", "Source Items", "raw ingested items", "info"],
      ["Sources", "Source Directory", "enabled source registry", "info"],
    ],
  },
  superinvestors: {
    title: "Superinvestors",
    eyebrow: "Disclosure tracking",
    subtitle: "Investor disclosures, ownership consensus, and tracked investor context.",
    sections: [
      { title: "Disclosures", getRows: (data) => rows(data.disclosures) },
      { title: "Trader Twins", getRows: (data) => rows(data.traderTwins) },
      { title: "Ownership Consensus", getRows: (data) => rows(data.ownershipConsensus) },
    ],
    metricLabels: [
      ["Disclosures", "Disclosures", "filings available", "info"],
      ["Investor Models", "Trader Twins", "tracked investor profiles", "info"],
      ["Consensus", "Ownership Consensus", "shared ownership signals", "good"],
    ],
  },
  market: {
    title: "Market Valuation",
    eyebrow: "Macro context",
    subtitle: "Market context, valuation, technicals, liquidity, and earnings setup.",
    sections: [
      { title: "Market Context", getRows: (data) => rows(data.marketContext) },
      { title: "Valuations", getRows: (data) => rows(data.valuations) },
      { title: "Technicals", getRows: (data) => rows(data.technicals) },
      { title: "Earnings Setups", getRows: (data) => rows(data.earningsSetups) },
    ],
    metricLabels: [
      ["Context", "Market Context", "macro and valuation backdrop", "info"],
      ["Technicals", "Technicals", "trend features loaded", "good"],
      ["Earnings Setups", "Earnings Setups", "event timing", "info"],
    ],
  },
  research: {
    title: "Research Queue",
    eyebrow: "Opportunities",
    subtitle: "Names to accept, reject, watch, or research next.",
    sections: [
      { title: "Idea Queue", getRows: (data) => rows(data.decisionQueue) },
      { title: "Ranked Opportunities", getRows: (data) => rows(data.opportunitiesRanked) },
      { title: "Opportunity Evidence", getRows: (data) => rows(data.opportunitySources) },
      { title: "Research Packets", getRows: (data) => rows(data.researchPackets) },
      { title: "Memos", getRows: (data) => rows(data.memos) },
    ],
    metricLabels: [
      ["Ideas", "Idea Queue", "accept, reject, watch", "good"],
      ["Research Packets", "Research Packets", "evidence packets ready", "info"],
      ["Memos", "Memos", "stored decision writeups", "info"],
    ],
  },
  filings: {
    title: "Filings",
    eyebrow: "Disclosure tracking",
    subtitle: "Disclosure and tracked-investor context.",
    sections: [
      { title: "Disclosures", getRows: (data) => rows(data.disclosures) },
      { title: "Trader Twins", getRows: (data) => rows(data.traderTwins) },
    ],
    metricLabels: [
      ["Disclosures", "Disclosures", "filings available", "info"],
      ["Trader Twins", "Trader Twins", "investor profiles", "info"],
    ],
  },
  calendar: {
    title: "Calendar",
    eyebrow: "Catalysts",
    subtitle: "Catalysts and earnings dates that can affect timing.",
    sections: [
      { title: "Catalysts", getRows: (data) => rows(data.catalysts) },
      { title: "Earnings", getRows: (data) => rows(data.earnings) },
    ],
    metricLabels: [
      ["Catalysts", "Catalysts", "review-driving events", "good"],
      ["Earnings", "Earnings", "earnings dates", "info"],
    ],
  },
};

export function buildDatasetProfile(id: DatasetProfileId, data: PanelData): DatasetProfile {
  const spec = profileSpecs[id];
  const sections = spec.sections.map((section) => ({ title: section.title, rows: section.getRows(data) }));
  return {
    id,
    title: spec.title,
    eyebrow: spec.eyebrow,
    subtitle: spec.subtitle,
    sections,
    metrics: buildMetrics(sections, spec.metricLabels),
  };
}

function buildMetrics(sections: DatasetSection[], labels: Array<[string, string, string, Tone]>): MetricSpec[] {
  const count = (sectionTitle: string) => sections.find((candidate) => candidate.title === sectionTitle)?.rows.length ?? 0;
  const populated = sections.filter((section) => section.rows.length).length;
  const metrics = labels.map(([label, sectionTitle, caption, tone]) => {
    const sectionCount = count(sectionTitle);
    return [label, sectionCount.toLocaleString() as ReactNode, caption, sectionCount ? tone : emptyTone(tone)] as MetricSpec;
  });
  if (metrics.length < 3) {
    metrics.push(["Evidence Sets", `${populated}/${sections.length}`, "populated sections", populated === sections.length ? "good" : "muted"]);
  }
  return metrics;
}

function emptyTone(tone: Tone): Tone {
  return tone === "good" ? "warn" : "muted";
}
