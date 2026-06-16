import { Minus, RefreshCw, Search, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { EmptyState, EvidenceList, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { fullField, listField, symbolList, textField, titleLabel, toneFromText } from "./rowFormat";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

type Sentiment = "bullish" | "bearish" | "neutral";

// Coarse families the feed can be navigated by, in display order. Keys match the
// backend `source_family` field; `all` is the synthetic "show everything" facet.
const FAMILY_LABELS: Record<string, string> = {
  all: "All",
  news: "News",
  blog: "Blog",
  memo: "Memo",
  thesis: "Theses",
  research: "Research",
  filing: "Filings & 13F",
  podcast: "Podcasts",
  transcript: "Transcripts",
};
const FAMILY_ORDER = ["news", "blog", "memo", "thesis", "research", "filing", "podcast", "transcript"];

export function FeedPage({ data, lastRefresh, loading, onRefresh, onOpenTicker }: { data: PanelData; lastRefresh: Date | null; loading: boolean; onRefresh: () => void; onOpenTicker: OpenTicker }) {
  const [query, setQuery] = useState("");
  const [family, setFamily] = useState("all");
  const [ticker, setTicker] = useState("all");

  const feedCards = useMemo(() => sourceFeedCards(rows(data.feedSignals)), [data.feedSignals]);

  const familyCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const row of feedCards) counts[cardFamily(row)] = (counts[cardFamily(row)] ?? 0) + 1;
    return counts;
  }, [feedCards]);

  const families = useMemo(() => FAMILY_ORDER.filter((key) => familyCounts[key]), [familyCounts]);

  const tickers = useMemo(() => {
    const seen = new Set<string>();
    for (const row of feedCards) for (const symbol of symbolList(row)) seen.add(symbol);
    return [...seen].sort();
  }, [feedCards]);

  const visibleCards = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return feedCards.filter((row) => {
      if (family !== "all" && cardFamily(row) !== family) return false;
      if (ticker !== "all" && !symbolList(row).includes(ticker)) return false;
      if (normalized && !feedSearchText(row).includes(normalized)) return false;
      return true;
    });
  }, [feedCards, family, ticker, query]);

  return (
    <WorkspacePage
      eyebrow="Source feed"
      title="Feed"
      subtitle="Source-backed theses, countercases, and evidence from news, blogs, memos, research, filings, and 13F ownership inputs."
      actions={
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
          <Select value={ticker} onValueChange={setTicker}>
            <SelectTrigger className="sm:w-40" aria-label="Filter feed by ticker"><SelectValue placeholder="Ticker" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All tickers</SelectItem>
              {tickers.map((symbol) => <SelectItem key={symbol} value={symbol}>{symbol}</SelectItem>)}
            </SelectContent>
          </Select>
          <div className="relative min-w-0 sm:w-72">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter source feed" aria-label="Filter source feed" />
          </div>
          <Button type="button" variant="outline" onClick={onRefresh}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>
        </div>
      }
    >
      <div className="mb-4 flex flex-wrap gap-1.5">
        <FamilyChip label={`All (${feedCards.length})`} active={family === "all"} onClick={() => setFamily("all")} />
        {families.map((key) => (
          <FamilyChip key={key} label={`${FAMILY_LABELS[key] ?? titleLabel(key)} (${familyCounts[key]})`} active={family === key} onClick={() => setFamily(key)} />
        ))}
      </div>
      {visibleCards.length ? (
        <div className="grid gap-4 xl:grid-cols-2">
          {visibleCards.map((row, index) => <FeedSignalCard key={textField(row, ["id"], `feed-${index}`)} row={row} onOpenTicker={onOpenTicker} />)}
        </div>
      ) : (
        <EmptyState title="No source cards loaded" detail={lastRefresh ? "No source-backed thesis cards match this filter." : "Refresh the feed to load source-backed cards."} />
      )}
    </WorkspacePage>
  );
}

function FamilyChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <Button type="button" size="sm" variant={active ? "default" : "outline"} onClick={onClick}>{label}</Button>
  );
}

function SentimentMark({ sentiment, className }: { sentiment: Sentiment; className?: string }) {
  if (sentiment === "neutral") return <Minus className={cn("size-3.5 text-muted-foreground", className)} aria-label="Neutral" />;
  const bullish = sentiment === "bullish";
  const Icon = bullish ? TrendingUp : TrendingDown;
  return <Icon className={cn("size-3.5", bullish ? "text-emerald-600" : "text-red-600", className)} aria-label={bullish ? "Bullish" : "Bearish"} />;
}

function FeedSignalCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: OpenTicker }) {
  const symbols = symbolList(row);
  const title = textField(row, ["title"], "Source update");
  const source = textField(row, ["source"], "Source");
  const familyLabel = FAMILY_LABELS[cardFamily(row)] ?? titleLabel(textField(row, ["source_type"], "source"));
  const date = feedDateLabel(textField(row, ["date", "published_at", "observed_at"]));
  const sentiment = cardSentiment(row);
  const sentimentBySymbol = symbolSentiment(row);
  const thesis = fullField(row, ["thesis", "summary", "reason"], title);
  const antithesis = fullField(row, ["antithesis", "invalidation"], "");
  const nextAction = fullField(row, ["next_action"], "");
  const portfolioRelevance = fullField(row, ["portfolio_relevance"], "");
  const evidence = listField(row, ["evidence", "evidence_refs", "source_url"]).filter((item) => item && item !== title).slice(0, 4);
  const tone = toneFromText(`${textField(row, ["severity"])} ${familyLabel} ${antithesis}`);
  return (
    <article className={cn("min-w-0 rounded-lg border border-border bg-card p-4", tone === "bad" && "border-red-200", tone === "warn" && "border-amber-200")}>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <StatusBadge tone={tone === "bad" || tone === "warn" ? tone : "info"}>{familyLabel}</StatusBadge>
        <span className="font-medium text-foreground">{source}</span>
        {date && <span>{date}</span>}
        <SentimentMark sentiment={sentiment} className="ml-auto" />
      </div>
      <h2 className="text-base font-semibold leading-6">{title}</h2>
      {symbols.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {symbols.map((symbol) => (
            <Button key={symbol} type="button" variant="outline" size="sm" className="gap-1" onClick={() => onOpenTicker(symbol)}>
              {symbol}
              <SentimentMark sentiment={sentimentBySymbol[symbol] ?? sentiment} />
            </Button>
          ))}
        </div>
      ) : null}
      <div className="mt-4 space-y-4 text-sm leading-6">
        <FeedBlock label="Thesis">{thesis}</FeedBlock>
        {antithesis && !antithesis.startsWith("No structured") && <FeedBlock label="Antithesis">{antithesis}</FeedBlock>}
        {portfolioRelevance && <FeedBlock label="Portfolio">{portfolioRelevance}</FeedBlock>}
        {nextAction && <FeedBlock label="Next">{nextAction}</FeedBlock>}
        {evidence.length ? <FeedBlock label="Evidence"><EvidenceList items={evidence.map(evidenceNode)} /></FeedBlock> : null}
      </div>
    </article>
  );
}

function FeedBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="break-words text-foreground">{children}</div>
    </div>
  );
}

function sourceFeedCards(feedRows: RowRecord[]): RowRecord[] {
  const operationalTypes = new Set(["decision_queue", "top_portfolio_changes", "top_risks", "blocked_stale_items", "source_freshness", "provider_run"]);
  return feedRows.filter((row) => {
    const sourceType = textField(row, ["source_type"]).toLowerCase();
    const title = textField(row, ["title"]);
    const thesis = fullField(row, ["thesis"], "");
    if (!title && !thesis) return false;
    return !operationalTypes.has(sourceType);
  });
}

// Prefer the backend `source_family`; fall back to deriving a coarse family from
// `source_type` so older payloads still group sensibly.
function cardFamily(row: RowRecord): string {
  const family = textField(row, ["source_family"]).toLowerCase();
  if (family) return family;
  const sourceType = textField(row, ["source_type"]).toLowerCase();
  if (sourceType === "socials") return "thesis";
  if (FAMILY_ORDER.includes(sourceType)) return sourceType;
  return "news";
}

function normalizeSentiment(value: string): Sentiment {
  const text = value.toLowerCase();
  if (text === "bullish" || text === "good") return "bullish";
  if (text === "bearish" || text === "bad" || text === "sell") return "bearish";
  return "neutral";
}

function cardSentiment(row: RowRecord): Sentiment {
  const explicit = textField(row, ["sentiment"]);
  if (explicit) return normalizeSentiment(explicit);
  return normalizeSentiment(textField(row, ["severity"]));
}

function symbolSentiment(row: RowRecord): Record<string, Sentiment> {
  const contexts = row.ticker_contexts;
  const result: Record<string, Sentiment> = {};
  if (Array.isArray(contexts)) {
    for (const context of contexts) {
      if (context && typeof context === "object" && !Array.isArray(context)) {
        const symbol = textField(context as RowRecord, ["symbol"]);
        const sentiment = textField(context as RowRecord, ["sentiment", "severity"]);
        if (symbol && sentiment) result[symbol.toUpperCase()] = normalizeSentiment(sentiment);
      }
    }
  }
  return result;
}

function feedSearchText(row: RowRecord): string {
  return [
    textField(row, ["title"]),
    textField(row, ["source"]),
    textField(row, ["source_type"]),
    textField(row, ["source_family"]),
    fullField(row, ["thesis"], ""),
    fullField(row, ["antithesis"], ""),
    symbolList(row).join(" "),
  ].join(" ").toLowerCase();
}

function feedDateLabel(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function evidenceNode(value: string): ReactNode {
  if (/^https?:\/\//i.test(value)) {
    return <a className="text-primary underline-offset-4 hover:underline" href={value} target="_blank" rel="noreferrer">{value}</a>;
  }
  return value;
}
