import { ClipboardCheck, Minus, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

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
  thesis: "Source theses",
  research: "Research",
  filing: "Filings & 13F",
  podcast: "Podcasts",
  transcript: "Transcripts",
};
const FAMILY_ORDER = ["news", "blog", "memo", "thesis", "research", "filing", "podcast", "transcript"];

export function FeedPage({ data, lastRefresh, loading, onRefresh, onOpenTicker }: { data: PanelData; lastRefresh: Date | null; loading: boolean; onRefresh: () => void; onOpenTicker: OpenTicker }) {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [family, setFamily] = useState("all");
  const [source, setSource] = useState("all");
  // Seed the ticker filter from ?ticker= so thesis-monitor can deep-link here.
  const [tickerQuery, setTickerQuery] = useState(() => (searchParams.get("ticker") ?? "").toUpperCase());
  const reviewThesis = (symbol: string) => navigate(`/thesis-monitor?symbol=${encodeURIComponent(symbol)}`);

  const feedCards = useMemo(() => sourceFeedCards(rows(data.feedSignals)), [data.feedSignals]);

  const familyCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const row of feedCards) counts[cardFamily(row)] = (counts[cardFamily(row)] ?? 0) + 1;
    return counts;
  }, [feedCards]);

  const families = useMemo(() => FAMILY_ORDER.filter((key) => familyCounts[key]), [familyCounts]);

  // Individual sources available within the current family, ranked by volume — so
  // the source can be picked from a list instead of typed.
  const sourceOptions = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const row of feedCards) {
      if (family !== "all" && cardFamily(row) !== family) continue;
      for (const name of cardSources(row)) counts[name] = (counts[name] ?? 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [feedCards, family]);

  const tickers = useMemo(() => {
    const seen = new Set<string>();
    for (const row of feedCards) for (const symbol of symbolList(row)) seen.add(symbol);
    return [...seen].sort();
  }, [feedCards]);

  const visibleCards = useMemo(() => {
    const tq = tickerQuery.trim().toUpperCase();
    return feedCards.filter((row) => {
      if (family !== "all" && cardFamily(row) !== family) return false;
      if (source !== "all" && !cardSources(row).includes(source)) return false;
      if (tq && !symbolList(row).some((symbol) => symbol.includes(tq))) return false;
      return true;
    });
  }, [feedCards, family, source, tickerQuery]);

  const pickFamily = (key: string) => {
    setFamily(key);
    setSource("all");
  };

  return (
    <WorkspacePage
      eyebrow="Source feed"
      title="Feed"
      subtitle="Source-backed theses, countercases, and evidence from news, blogs, memos, research, filings, and 13F ownership inputs."
      actions={
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
          <Select value={source} onValueChange={setSource}>
            <SelectTrigger className="sm:w-56" aria-label="Pick a source"><SelectValue placeholder="Source" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All sources ({feedCards.length})</SelectItem>
              {sourceOptions.map(([name, count]) => <SelectItem key={name} value={name}>{name} ({count})</SelectItem>)}
            </SelectContent>
          </Select>
          <div className="min-w-0 sm:w-44">
            <Input
              list="feed-ticker-options"
              value={tickerQuery}
              onChange={(event) => setTickerQuery(event.target.value.toUpperCase())}
              placeholder="Ticker…"
              aria-label="Filter by ticker"
              autoComplete="off"
            />
            <datalist id="feed-ticker-options">
              {tickers.map((symbol) => <option key={symbol} value={symbol} />)}
            </datalist>
          </div>
          <Button type="button" variant="outline" onClick={onRefresh}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>
        </div>
      }
    >
      <div className="mb-4 flex flex-wrap gap-1.5">
        <FamilyChip label={`All (${feedCards.length})`} active={family === "all"} onClick={() => pickFamily("all")} />
        {families.map((key) => (
          <FamilyChip key={key} label={`${FAMILY_LABELS[key] ?? titleLabel(key)} (${familyCounts[key]})`} active={family === key} onClick={() => pickFamily(key)} />
        ))}
      </div>
      {visibleCards.length ? (
        <div className="grid gap-4 xl:grid-cols-2">
          {visibleCards.map((row, index) => <FeedSignalCard key={textField(row, ["id"], `feed-${index}`)} row={row} onOpenTicker={onOpenTicker} onReviewThesis={reviewThesis} />)}
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
  if (sentiment === "neutral") return <Minus className={cn("size-4 text-muted-foreground", className)} aria-label="Neutral" />;
  const bullish = sentiment === "bullish";
  const Icon = bullish ? TrendingUp : TrendingDown;
  return <Icon className={cn("size-4", bullish ? "text-emerald-600" : "text-red-600", className)} aria-label={bullish ? "Bullish" : "Bearish"} />;
}

// Sentiment-tinted classes so the bull/bear read on each ticker chip is obvious.
const SENTIMENT_CHIP: Record<Sentiment, string> = {
  bullish: "border-emerald-300 text-emerald-700 hover:bg-emerald-50",
  bearish: "border-red-300 text-red-700 hover:bg-red-50",
  neutral: "",
};

function FeedSignalCard({ row, onOpenTicker, onReviewThesis }: { row: RowRecord; onOpenTicker: OpenTicker; onReviewThesis: (symbol: string) => void }) {
  const symbols = symbolList(row);
  const primarySymbol = textField(row, ["primary_symbol"]) || symbols[0] || "";
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
          {symbols.map((symbol) => {
            const symbolSentimentValue = sentimentBySymbol[symbol] ?? sentiment;
            return (
              <Button key={symbol} type="button" variant="outline" size="sm" className={cn("gap-1", SENTIMENT_CHIP[symbolSentimentValue])} onClick={() => onOpenTicker(symbol)}>
                <SentimentMark sentiment={symbolSentimentValue} />
                {symbol}
              </Button>
            );
          })}
        </div>
      ) : null}
      <div className="mt-4 space-y-4 text-sm leading-6">
        <FeedBlock label="Thesis">{thesis}</FeedBlock>
        {antithesis && !antithesis.startsWith("No structured") && <FeedBlock label="Antithesis">{antithesis}</FeedBlock>}
        {portfolioRelevance && <FeedBlock label="Portfolio">{portfolioRelevance}</FeedBlock>}
        {nextAction && <FeedBlock label="Next">{nextAction}</FeedBlock>}
        {evidence.length ? <FeedBlock label="Evidence"><EvidenceList items={evidence.map(evidenceNode)} /></FeedBlock> : null}
      </div>
      {primarySymbol ? (
        <div className="mt-3 flex justify-end">
          <Button type="button" size="sm" variant="ghost" className="h-7 gap-1 text-xs text-muted-foreground" onClick={() => onReviewThesis(primarySymbol)}>
            <ClipboardCheck className="size-3.5" /> Review thesis: {primarySymbol}
          </Button>
        </div>
      ) : null}
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

// Grouped cards expose every contributing source in `sources`; single cards only
// carry `source` (which may be a "X +N" rollup label, so prefer the array).
function cardSources(row: RowRecord): string[] {
  const list = listField(row, ["sources"]);
  if (list.length) return list;
  const single = textField(row, ["source"]);
  return single ? [single] : [];
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
