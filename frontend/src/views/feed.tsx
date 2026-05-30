import { RefreshCw, Search } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { EmptyState, EvidenceList, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { fullField, listField, symbolList, textField, titleLabel, toneFromText } from "./rowFormat";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

export function FeedPage({ data, lastRefresh, loading, onRefresh, onOpenTicker }: { data: PanelData; lastRefresh: Date | null; loading: boolean; onRefresh: () => void; onOpenTicker: OpenTicker }) {
  const [query, setQuery] = useState("");
  const feedCards = useMemo(() => sourceFeedCards(rows(data.feedSignals)), [data.feedSignals]);
  const visibleCards = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return feedCards;
    return feedCards.filter((row) => feedSearchText(row).includes(normalized));
  }, [feedCards, query]);
  return (
    <WorkspacePage
      eyebrow="Source feed"
      title="Feed"
      subtitle="Source-backed theses, countercases, and evidence from news, filings, research, and ownership inputs."
      actions={
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
          <div className="relative min-w-0 sm:w-72">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter source feed" aria-label="Filter source feed" />
          </div>
          <Button type="button" variant="outline" onClick={onRefresh}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>
        </div>
      }
    >
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

function FeedSignalCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: OpenTicker }) {
  const symbols = symbolList(row);
  const title = textField(row, ["title"], "Source update");
  const source = textField(row, ["source"], "Source");
  const sourceType = titleLabel(textField(row, ["source_type"], "source"));
  const date = feedDateLabel(textField(row, ["date", "published_at", "observed_at"]));
  const thesis = fullField(row, ["thesis", "summary", "reason"], title);
  const antithesis = fullField(row, ["antithesis", "invalidation"], "");
  const nextAction = fullField(row, ["next_action"], "");
  const portfolioRelevance = fullField(row, ["portfolio_relevance"], "");
  const evidence = listField(row, ["evidence", "evidence_refs", "source_url"]).filter((item) => item && item !== title).slice(0, 4);
  const tone = toneFromText(`${textField(row, ["severity"])} ${sourceType} ${antithesis}`);
  return (
    <article className={cn("min-w-0 rounded-lg border border-border bg-card p-4", tone === "bad" && "border-red-200", tone === "warn" && "border-amber-200")}>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <StatusBadge tone={tone === "bad" || tone === "warn" ? tone : "info"}>{sourceType}</StatusBadge>
        <span className="font-medium text-foreground">{source}</span>
        {date && <span>{date}</span>}
      </div>
      <h2 className="text-base font-semibold leading-6">{title}</h2>
      {symbols.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {symbols.map((symbol) => (
            <Button key={symbol} type="button" variant="outline" size="sm" onClick={() => onOpenTicker(symbol)}>{symbol}</Button>
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

function feedSearchText(row: RowRecord): string {
  return [
    textField(row, ["title"]),
    textField(row, ["source"]),
    textField(row, ["source_type"]),
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
