import { ExternalLink, RefreshCw, Search } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTableFrame, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import { cn } from "@/lib/utils";
import type { AppModel } from "@/model";
import type { PanelData, RowRecord, TickerPayload } from "@/types";
import { displayValue, rows, tickerSymbolFromRow } from "@/utils";
import { displayField, formatMoney, formatPct, fullField, listField, numberField, symbolList, textField, titleLabel, toneFromText } from "./rowFormat";

type OpenTicker = (symbol: string) => void;

export function FeedPage({ data, lastRefresh, loading, onRefresh, onOpenTicker }: { data: PanelData; model: AppModel; lastRefresh: Date | null; loading: boolean; onRefresh: () => void; onOpenTicker: OpenTicker }) {
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
            <Input className="h-9 pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter source feed" aria-label="Filter source feed" />
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

export function WatchlistPage({ data, onOpenTicker }: { data: PanelData; onOpenTicker: OpenTicker }) {
  return <DatasetPage title="Watchlist" eyebrow="Market data" subtitle="Quote, screener, and TradingView watchlist rows." sections={[["Quotes", rows(data.quotes)], ["TradingView Watchlists", rows(data.tradingviewWatchlists)], ["Universe Screen", rows(data.universeScreen)]]} onOpenTicker={onOpenTicker} />;
}

export function SourcesPage({ data, onOpenTicker }: { data: PanelData; onOpenTicker: OpenTicker }) {
  return <DatasetPage title="Sources" eyebrow="Source health" subtitle="Freshness, provider runs, source health, and consensus state." sections={[["Freshness", rows(data.sourceFreshness)], ["Provider Runs", rows(data.providerRuns)], ["Source Health", rows(data.sourceHealth)], ["Source Consensus", rows(data.sourceConsensus)]]} onOpenTicker={onOpenTicker} />;
}

export function SuperinvestorsPage({ data, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: OpenTicker }) {
  return <DatasetPage title="Superinvestors" eyebrow="Disclosure tracking" subtitle="Investor disclosure rows and ownership consensus from the backend." sections={[["Disclosures", rows(data.disclosures)], ["Trader Twins", rows(data.traderTwins)], ["Ownership Consensus", rows(data.ownershipConsensus)]]} onOpenTicker={onOpenTicker} />;
}

export function MarketContextPage({ data }: { data: PanelData }) {
  return <DatasetPage title="Market Valuation" eyebrow="Macro context" subtitle="Market context, valuation, technicals, liquidity, and earnings setup." sections={[["Market Context", rows(data.marketContext)], ["Valuations", rows(data.valuations)], ["Technicals", rows(data.technicals)], ["Earnings Setups", rows(data.earningsSetups)]]} />;
}

export function PortfolioPage({ data, model, onOpenTicker, onRefresh }: { data: PanelData; model: AppModel; onOpenTicker: OpenTicker; onRefresh: () => Promise<void> }) {
  const riskRows = rows(data.portfolioRiskCards);
  const reviewRows = rows(data.reviewActions);
  const topHolding = model.holdings.slice().sort((a, b) => b.weight - a.weight)[0];
  return (
    <WorkspacePage
      eyebrow="Portfolio"
      title="Portfolio"
      subtitle="Position sizing, concentration risk, and review actions that can change portfolio decisions."
      actions={<Button type="button" variant="outline" onClick={() => void onRefresh()}><RefreshCw /> Refresh</Button>}
      metrics={[
        ["Portfolio Value", formatMoney(model.portfolioValue), `${model.holdings.length} holdings`, model.portfolioValue ? "info" : "muted"],
        ["Top Exposure", topHolding ? `${topHolding.ticker} ${topHolding.weight.toFixed(1)}%` : "None", topHolding?.nextStep ?? "no priced holding", topHolding && topHolding.weight > 30 ? "warn" : "info"],
        ["Risk Cards", riskRows.length, "concentration and thesis gaps", riskRows.length ? "warn" : "muted"],
        ["Review Actions", reviewRows.length, "open portfolio decisions", reviewRows.length ? "warn" : "good"],
      ]}
    >
      <HoldingsTable holdings={model.holdings} onOpenTicker={onOpenTicker} />
      <RowsSection title="Risk Cards" rows={riskRows} onOpenTicker={onOpenTicker} />
      <RowsSection title="Review Actions" rows={reviewRows} onOpenTicker={onOpenTicker} />
      <RowsSection title="Exposure Clusters" rows={rows(data.exposureClusters)} onOpenTicker={onOpenTicker} />
    </WorkspacePage>
  );
}

export function ResearchPage({ data, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: OpenTicker }) {
  return <DatasetPage title="Research Queue" eyebrow="Opportunities" subtitle="Names to accept, reject, watch, or research next." sections={[["Decision Queue", rows(data.decisionQueue)], ["Ranked Opportunities", rows(data.opportunitiesRanked)], ["Opportunity Sources", rows(data.opportunitySources)], ["Research Packets", rows(data.researchPackets)], ["Memos", rows(data.memos)]]} onOpenTicker={onOpenTicker} />;
}

export function FilingsPage({ data, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: OpenTicker }) {
  return <DatasetPage title="Filings" eyebrow="Disclosure rows" subtitle="Disclosure and trader filing data." sections={[["Disclosures", rows(data.disclosures)], ["Trader Twins", rows(data.traderTwins)]]} onOpenTicker={onOpenTicker} />;
}

export function CalendarPage({ data, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: OpenTicker }) {
  return <DatasetPage title="Calendar" eyebrow="Catalysts" subtitle="Catalyst and earnings rows that can affect timing." sections={[["Catalysts", rows(data.catalysts)], ["Earnings", rows(data.earnings)]]} onOpenTicker={onOpenTicker} />;
}

export function HealthPage({ model, data }: { model: AppModel; data: PanelData }) {
  return (
    <WorkspacePage
      eyebrow="System health"
      title="Health"
      subtitle="Provider health, source freshness, broker status, and refresh job state."
      metrics={[
        ["Latest Check", model.latestHealthCheck, "freshest health timestamp", model.sources.health === "live" ? "info" : "muted"],
        ["Freshness Rows", rows(data.sourceFreshness).length, "source checks", rows(data.sourceFreshness).length ? "info" : "muted"],
        ["Provider Runs", rows(data.providerRuns).length, "ingestion runs", rows(data.providerRuns).length ? "info" : "muted"],
        ["Broker Status", rows(data.brokerStatus).length, "account source checks", rows(data.brokerStatus).length ? "info" : "muted"],
      ]}
    >
      <RowsSection title="Source Freshness" rows={rows(data.sourceFreshness)} />
      <RowsSection title="Source Health" rows={rows(data.sourceHealth)} />
      <RowsSection title="Provider Runs" rows={rows(data.providerRuns)} />
      <RowsSection title="Refresh Jobs" rows={rows(data.refreshJobs)} />
    </WorkspacePage>
  );
}

export function SettingsPage({ data }: { data: PanelData }) {
  const config = data.settings.config ?? {};
  const integration = data.settings.integration ?? {};
  return (
    <WorkspacePage eyebrow="Configuration" title="Settings" subtitle="Local configuration and integration state.">
      <RowsSection title="Config" rows={objectRows(config)} />
      <RowsSection title="Integration" rows={objectRows(integration)} />
    </WorkspacePage>
  );
}

export function TickerPage({ symbol, ticker, data, onOpenTicker }: { symbol: string; ticker: TickerPayload | null; model: AppModel; data: PanelData; onOpenTicker: OpenTicker }) {
  const tickerSections = tickerTableSections(ticker);
  const thesisRows = rows(data.thesisMonitor).filter((row) => symbolList(row).includes(symbol));
  const title = ticker?.found === false ? `${symbol} not found` : symbol;
  return (
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Ticker evidence, thesis state, decision snapshot, and source rows.">
      <TradingViewChart symbol={symbol} ticker={ticker} />
      {ticker?.decision_brief || ticker?.decision_snapshot ? (
        <div className="mb-4 grid min-w-0 gap-3 lg:grid-cols-2 [&>*]:min-w-0">
          {ticker.decision_brief && <DossierCard title="Decision Brief" row={ticker.decision_brief} />}
          {ticker.decision_snapshot && <DossierCard title="Decision Snapshot" row={ticker.decision_snapshot} />}
        </div>
      ) : null}
      <RowsSection title="Thesis State" rows={thesisRows} onOpenTicker={onOpenTicker} />
      {tickerSections.map(([sectionTitle, sectionRows]) => <RowsSection key={sectionTitle} title={sectionTitle} rows={sectionRows} onOpenTicker={onOpenTicker} />)}
    </WorkspacePage>
  );
}

function TradingViewChart({ symbol, ticker }: { symbol: string; ticker: TickerPayload | null }) {
  const tradingViewSymbol = resolveTradingViewSymbol(symbol, ticker);
  const chartUrl = tradingViewEmbedUrl(tradingViewSymbol);
  const externalUrl = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tradingViewSymbol)}`;
  return (
    <DataTableFrame
      title="Chart"
      action={
        <Button asChild type="button" variant="outline" size="sm">
          <a href={externalUrl} target="_blank" rel="noreferrer"><ExternalLink /> Open TradingView</a>
        </Button>
      }
    >
      <div className="h-[360px] w-full bg-muted/30 sm:h-[440px]">
        <iframe
          title={`${symbol} TradingView chart`}
          src={chartUrl}
          className="h-full w-full border-0"
          loading="lazy"
          referrerPolicy="no-referrer-when-downgrade"
          sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
        />
      </div>
    </DataTableFrame>
  );
}

function WorkspacePage({ eyebrow, title, subtitle, actions, metrics = [], children }: { eyebrow: string; title: string; subtitle: string; actions?: ReactNode; metrics?: Array<[string, ReactNode, string, "good" | "warn" | "bad" | "info" | "muted"]>; children: ReactNode }) {
  return (
    <section>
      <PageHeader eyebrow={eyebrow} title={title} subtitle={subtitle} actions={actions} />
      {metrics.length ? (
        <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {metrics.map(([label, value, caption, tone]) => <MetricTile key={label} label={label} value={value} caption={caption} tone={tone} />)}
        </div>
      ) : null}
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function DatasetPage({ title, eyebrow, subtitle, sections, onOpenTicker }: { title: string; eyebrow: string; subtitle: string; sections: Array<[string, RowRecord[]]>; onOpenTicker?: OpenTicker }) {
  const totalRows = sections.reduce((total, [, sectionRows]) => total + sectionRows.length, 0);
  return (
    <WorkspacePage
      eyebrow={eyebrow}
      title={title}
      subtitle={subtitle}
      metrics={[
        ["Loaded Rows", totalRows, "across this workspace", totalRows ? "info" : "muted"],
        ["Sections", sections.length, "backend tables", "info"],
      ]}
    >
      {sections.map(([sectionTitle, sectionRows]) => <RowsSection key={sectionTitle} title={sectionTitle} rows={sectionRows} onOpenTicker={onOpenTicker} />)}
    </WorkspacePage>
  );
}

function RowsSection({ title, rows: sectionRows, onOpenTicker }: { title: string; rows: RowRecord[]; onOpenTicker?: OpenTicker }) {
  const [query, setQuery] = useState("");
  const columns = useMemo(() => columnKeys(sectionRows), [sectionRows]);
  const filteredRows = useMemo(() => filterRows(sectionRows, columns, query), [sectionRows, columns, query]);
  const visibleRows = filteredRows.slice(0, 80);

  if (!sectionRows.length) {
    return (
      <DataTableFrame title={title}>
        <div className="px-4 py-3 text-sm text-muted-foreground">No rows for the current scope.</div>
      </DataTableFrame>
    );
  }

  return (
    <DataTableFrame
      title={title}
      action={
        <div className="flex min-w-0 flex-1 items-center justify-end gap-3">
          <span className="hidden whitespace-nowrap text-xs text-muted-foreground sm:inline">
            {filteredRows.length.toLocaleString()} / {sectionRows.length.toLocaleString()}
          </span>
          <div className="relative w-full max-w-56">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="h-8 pl-8 text-xs"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter rows"
              aria-label={`Filter ${title}`}
            />
          </div>
        </div>
      }
    >
      <table className="w-full min-w-[840px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            {columns.map((column) => <th key={column} className="px-3 py-2">{titleLabel(column)}</th>)}
            {onOpenTicker && <th className="px-3 py-2">Open</th>}
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row, index) => {
            const symbol = tickerSymbolFromRow(row) || symbolList(row)[0];
            return (
              <tr key={index} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                {columns.map((column) => (
                  <td key={column} className={cn("max-w-[360px] px-3 py-2 leading-6", isPriorityColumn(column) && "font-medium")}>
                    {formatCellContent(column, row[column])}
                  </td>
                ))}
                {onOpenTicker && (
                  <td className="px-3 py-2">
                    {symbol ? <Button type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(symbol)}><ExternalLink /> {symbol}</Button> : <span className="text-muted-foreground">-</span>}
                  </td>
                )}
              </tr>
            );
          })}
          {!visibleRows.length && (
            <tr>
              <td colSpan={columns.length + (onOpenTicker ? 1 : 0)} className="px-4 py-6 text-center text-sm text-muted-foreground">
                No rows match "{query.trim()}".
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function formatCellContent(column: string, value: RowRecord[string]): ReactNode {
  if (value === undefined || value === null || value === "") return <span className="text-muted-foreground">-</span>;

  if (isScoreColumn(column)) {
    const numeric = numericValue(value);
    if (numeric !== null) return <ScoreCell value={numeric} display={formatCellValue(column, value)} />;
  }

  if (isToneColumn(column)) {
    const label = formatCellValue(column, value);
    return <StatusBadge tone={toneForCell(column, label)}>{label}</StatusBadge>;
  }

  if (isSymbolColumn(column)) {
    return <span className="font-semibold tracking-normal text-foreground">{formatCellValue(column, value)}</span>;
  }

  if (isDateColumn(column)) {
    return <span className="whitespace-nowrap text-muted-foreground">{formatCellValue(column, value)}</span>;
  }

  return formatCellValue(column, value);
}

function ScoreCell({ value, display }: { value: number; display: string }) {
  const normalized = normalizeScore(value);
  const tone = value >= 70 || (value > 0 && value <= 1 && value >= 0.7) ? "good" : value >= 40 || (value > 0 && value <= 1 && value >= 0.4) ? "warn" : "muted";
  return (
    <div className="min-w-28">
      <div className="mb-1 flex items-center justify-between gap-3">
        <span className="font-medium tabular-nums">{display}</span>
        <span className={cn("size-1.5 shrink-0 rounded-full", tone === "good" ? "bg-green-600" : tone === "warn" ? "bg-amber-500" : "bg-muted-foreground")} />
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full", tone === "good" ? "bg-green-600" : tone === "warn" ? "bg-amber-500" : "bg-muted-foreground")}
          style={{ width: `${normalized}%` }}
        />
      </div>
    </div>
  );
}

function formatCellValue(column: string, value: RowRecord[string]): string {
  if (value === undefined || value === null || value === "") return "-";
  if (typeof value === "string") {
    if (isDateColumn(column)) {
      const date = new Date(value);
      if (!Number.isNaN(date.getTime())) {
        return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      }
    }
    if (isToneColumn(column)) {
      return titleLabel(value);
    }
  }
  return displayValue(value);
}

function filterRows(sectionRows: RowRecord[], columns: string[], query: string): RowRecord[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return sectionRows;
  return sectionRows.filter((row) => {
    const symbol = tickerSymbolFromRow(row) || symbolList(row).join(" ");
    const haystack = [symbol, ...columns.map((column) => formatCellValue(column, row[column]))].join(" ").toLowerCase();
    return haystack.includes(needle);
  });
}

function isDateColumn(column: string): boolean {
  return ["as_of", "updated_at", "created_at", "checked_at", "last_run_at", "timestamp", "filed_at"].includes(column) || column.endsWith("_at");
}

function isToneColumn(column: string): boolean {
  return ["status", "decision", "decision_bucket", "action_grade", "action_type", "risk_type", "severity", "health", "freshness_state", "needs_review", "blocker", "conviction", "confidence", "grade"].includes(column);
}

function isSymbolColumn(column: string): boolean {
  return ["symbol", "ticker"].includes(column);
}

function isPriorityColumn(column: string): boolean {
  return ["symbol", "ticker", "name", "title", "decision", "next_action"].includes(column);
}

function isScoreColumn(column: string): boolean {
  const normalized = column.toLowerCase();
  return normalized.includes("score") || normalized.includes("confidence") || normalized.includes("strength") || normalized.includes("conviction");
}

function numericValue(value: RowRecord[string]): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function normalizeScore(value: number): number {
  if (value > 0 && value <= 1) return Math.max(4, Math.min(100, value * 100));
  return Math.max(4, Math.min(100, value));
}

function toneForCell(column: string, value: string) {
  const combined = `${column} ${value}`;
  if (column === "needs_review" && ["yes", "true"].includes(value.toLowerCase())) return "warn";
  if (column === "blocker" && value !== "-" && value.toLowerCase() !== "none") return "bad";
  return toneFromText(combined);
}

function HoldingsTable({ holdings, onOpenTicker }: { holdings: AppModel["holdings"]; onOpenTicker: OpenTicker }) {
  if (!holdings.length) return <EmptyState title="No holdings loaded" detail="Portfolio rows are empty for this scope." />;
  return (
    <DataTableFrame title="Holdings">
      <table className="w-full min-w-[760px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2">Ticker</th>
            <th className="px-3 py-2">Quantity</th>
            <th className="px-3 py-2">Price</th>
            <th className="px-3 py-2">Market Value</th>
            <th className="px-3 py-2">Weight</th>
            <th className="px-3 py-2">Unrealized</th>
            <th className="px-3 py-2">Review</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((holding) => (
            <tr key={holding.ticker} className="border-b border-border">
              <td className="px-3 py-2"><Button type="button" variant="link" className="h-auto p-0" onClick={() => onOpenTicker(holding.ticker)}>{holding.ticker}</Button></td>
              <td className="px-3 py-2">{holding.quantity.toLocaleString()}</td>
              <td className="px-3 py-2">{formatMoney(holding.price)}</td>
              <td className="px-3 py-2">{formatMoney(holding.marketValue)}</td>
              <td className="px-3 py-2">{holding.hasMarketValue ? formatPct(holding.weight) : "-"}</td>
              <td className="px-3 py-2"><StatusBadge tone={holding.unrealizedPnl >= 0 ? "good" : "bad"}>{formatMoney(holding.unrealizedPnl)}</StatusBadge></td>
              <td className="px-3 py-2">{holding.nextStep}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function DossierCard({ title, row }: { title: string; row: RowRecord }) {
  const evidence = listField(row, ["evidence", "evidence_links", "sources"]);
  const status = displayField(row, ["status", "decision", "action_grade", "grade"], "Loaded");
  return (
    <div className="min-w-0 rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-start justify-between gap-3">
        <h2 className="text-base font-semibold">{title}</h2>
        <StatusBadge tone={toneFromText(status)}>{status}</StatusBadge>
      </div>
      <p className="break-words text-sm leading-6">{dossierSummary(row)}</p>
      <div className="mt-3 text-sm text-muted-foreground"><EvidenceList items={evidence.slice(0, 4)} /></div>
    </div>
  );
}

function dossierSummary(row: RowRecord): string {
  const raw = fullField(row, ["summary", "thesis", "reason", "decision_basis"], "No summary");
  const parsed = parseJsonObject(raw);
  if (parsed) {
    const parts = [
      textField(parsed, ["summary", "decision_basis", "reason"]),
      textField(parsed, ["action_grade", "decision", "status"]),
      displayField(parsed, ["score", "decision_score"]),
      textField(parsed, ["next_action", "nextAction"]),
    ].filter((part) => part && part !== "-");
    return truncateText(parts.join(" | ") || JSON.stringify(parsed as Record<string, unknown>), 280);
  }
  return truncateText(raw, 280);
}

function parseJsonObject(value: string): RowRecord | null {
  if (!value.trim().startsWith("{")) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as RowRecord : null;
  } catch {
    return null;
  }
}

function truncateText(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}…` : value;
}

function columnKeys(sectionRows: RowRecord[]): string[] {
  const preferred = ["symbol", "ticker", "name", "status", "decision", "score", "rank", "reason", "next_action", "source", "as_of", "updated_at"];
  const discovered = new Set<string>();
  for (const row of sectionRows.slice(0, 12)) {
    Object.keys(row).forEach((key) => discovered.add(key));
  }
  const ordered = preferred.filter((key) => discovered.has(key));
  const extras = [...discovered].filter((key) => !ordered.includes(key)).slice(0, Math.max(0, 8 - ordered.length));
  return [...ordered, ...extras].slice(0, 8);
}

function objectRows(object: Record<string, unknown>): RowRecord[] {
  return Object.entries(object).map(([key, value]) => ({ key, value: typeof value === "object" && value !== null ? JSON.stringify(value) : String(value) }));
}

const tickerSectionOrder: Array<[string, keyof NonNullable<TickerPayload["tables"]>]> = [
  ["Decision Queue", "decision_queue"],
  ["Decision Readiness", "decision_readiness"],
  ["Candidate Screen", "candidates"],
  ["Universe Context", "universe_screen"],
  ["Quote", "quotes"],
  ["Technicals", "technicals"],
  ["SEPA", "sepa"],
  ["Liquidity", "liquidity"],
  ["Valuations", "valuations"],
  ["Earnings Setup", "earnings_setups"],
  ["Earnings", "earnings"],
  ["Analyst Estimates", "analyst_estimates"],
  ["Source Signals", "ticker_source_signals"],
  ["News", "news"],
  ["Disclosures", "disclosures"],
  ["Ownership Consensus", "ownership_consensus"],
  ["Options Payoff", "options_payoff_scenarios"],
  ["Options Chain", "options_chain"],
  ["TradingView Watchlists", "tradingview_watchlists"],
  ["TradingView Alerts", "tradingview_alerts"],
  ["TradingView Chart State", "tradingview_chart_state"],
  ["Broker Positions", "broker_positions"],
  ["Agent Recommendations", "agent_recommendations"],
];

function tickerTableSections(ticker: TickerPayload | null): Array<[string, RowRecord[]]> {
  const tables = ticker?.tables;
  if (!tables) return [];
  return tickerSectionOrder
    .map(([title, key]) => [title, compactRows(tables[key])] as [string, RowRecord[]])
    .filter(([, sectionRows]) => sectionRows.length > 0);
}

function compactRows(sectionRows: RowRecord[] | undefined): RowRecord[] {
  return (sectionRows ?? [])
    .map((row) => Object.fromEntries(Object.entries(row).filter(([, value]) => !isEmptyCell(value))) as RowRecord)
    .filter((row) => Object.keys(row).length > 0);
}

function isEmptyCell(value: RowRecord[string]): boolean {
  if (value === undefined || value === null || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === "object" && Object.keys(value).length === 0;
}

function resolveTradingViewSymbol(symbol: string, ticker: TickerPayload | null): string {
  const normalized = symbol.toUpperCase();
  const tables = ticker?.tables ?? {};
  const candidates = [
    ...compactRows(tables.quotes).map((row) => nestedString(row, ["raw", "symbol"])),
    ...compactRows(tables.tradingview_chart_state).map((row) => textField(row, ["symbol"])),
    ...compactRows(tables.tradingview_symbol_search).map((row) => {
      const exchange = textField(row, ["exchange"]);
      const rowSymbol = textField(row, ["symbol", "ticker"]);
      return exchange && rowSymbol && !rowSymbol.includes(":") ? `${exchange}:${rowSymbol}` : rowSymbol;
    }),
  ];
  const explicit = candidates.find((candidate) => candidate.includes(":"));
  if (explicit) return explicit.toUpperCase();
  if (normalized.endsWith("-USD")) return `COINBASE:${normalized.replace("-USD", "USD")}`;
  if (["SPY", "QQQ"].includes(normalized)) return `AMEX:${normalized}`;
  return `NASDAQ:${normalized}`;
}

function nestedString(row: RowRecord, path: string[]): string {
  let current: unknown = row;
  for (const key of path) {
    if (!current || typeof current !== "object" || !(key in current)) return "";
    current = (current as Record<string, unknown>)[key];
  }
  return typeof current === "string" ? current.trim() : "";
}

function tradingViewEmbedUrl(tradingViewSymbol: string): string {
  const params = new URLSearchParams({
    frameElementId: `market-chart-${tradingViewSymbol.replace(/[^A-Za-z0-9]/g, "-")}`,
    symbol: tradingViewSymbol,
    interval: "D",
    range: "12M",
    hidesidetoolbar: "1",
    symboledit: "1",
    saveimage: "0",
    toolbarbg: "F1F3F6",
    studies: "MASimple@tv-basicstudies,RSI@tv-basicstudies,MACD@tv-basicstudies",
    theme: "light",
    style: "1",
    timezone: "Etc/UTC",
    withdateranges: "1",
    hideideas: "1",
  });
  return `https://www.tradingview.com/widgetembed/?${params.toString()}`;
}
