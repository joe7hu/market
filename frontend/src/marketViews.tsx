import {
  AlertTriangle,
  CalendarDays,
  ChevronRight,
  ClipboardList,
  Database,
  FileSearch,
  HeartPulse,
  Home,
  Layers3,
  RefreshCw,
  Sparkles,
  Star,
  Sun,
  UserRound,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  Cell,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  EmptyState,
  FilterRail,
  GhostButton,
  IconButton,
  MetricBadge,
  MetricStrip,
  PageFrame,
  Panel,
  SegmentedControl,
  SourceNotice,
  SourcePill,
  TabBar,
  TableFrame,
  TextLink,
} from "./components/primitives";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
  type Table,
} from "@tanstack/react-table";
import { deletePortfolioPosition, savePortfolioPosition } from "./api";
import type { JsonValue, PanelData, RowRecord, TablePayload, TickerPayload } from "./types";
import { displayValue, rows, symbolFromRow, tickerSymbol } from "./utils";

type Tone = "good" | "warn" | "bad" | "info" | "muted";

type WatchItem = {
  symbol: string;
  price: string;
  change: number;
};

type Opportunity = {
  rank: number;
  ticker: string;
  name: string;
  assetClass: string;
  category: string;
  score: number;
  grade: string;
  actionGrade: DecisionBucket;
  confidence: number;
  decision: string;
  freshnessStatus: FreshnessStatus;
  sourceCluster: string;
  inclusionReasons: string[];
  blockingGates: string[];
  decisionBasis: string;
  asOf: string;
  latestQuote: string;
  catalystWindow: string;
  liquidity: string;
  portfolioImpact: string;
  owned: boolean;
  sourceCount: number;
  isSourceThin: boolean;
  isStale: boolean;
  whyNow: string;
  nextAction: string;
  invalidation: string;
  freshness: string;
  tags: string[];
  components: Array<[string, number]>;
  evidenceCount: number;
};

type Holding = {
  ticker: string;
  quantity: number;
  assetClass: string;
  category: string;
  weight: number;
  price: number;
  marketValue: number;
  hasMarketValue: boolean;
  averageCost: number;
  purchaseDate: string;
  holdingDays: number;
  taxLotTerm: string;
  unrealizedPnl: number;
  unrealizedPnlPct: number;
  hasPnl: boolean;
  quoteFreshness: string;
  dayChangePct: number;
  dayChangeValue: number;
  addStance: string;
  nextStep: string;
  decisionScore: number;
  blockers: string[];
};

type PortfolioStats = {
  totalCount: number;
  pricedCount: number;
  unpricedCount: number;
  portfolioValue: number;
  costBasis: number;
  unrealizedPnl: number;
  unrealizedPnlPct: number;
  dayChange: number;
  dayChangePct: number;
  top3Weight: number;
  largest?: Holding;
  gainers: number;
  losers: number;
  shortTermWeight: number;
  longTermWeight: number;
  quoteGapCount: number;
  riskScore: number;
};

type CalendarEvent = {
  id: string;
  fullDate: string;
  date: number;
  dateText: string;
  monthLabel: string;
  label: string;
  symbol: string;
  sourceUrl: string;
  sourceName: string;
  status: string;
  importance: string;
  type: "earnings" | "economic" | "filing" | "event" | "options";
};

type Filing = {
  investor: string;
  ticker: string;
  security: string;
  action: string;
  shares: number;
  value: number;
  filed: string;
  event: string;
};

type TraderFilingCard = {
  investor: string;
  filed: string;
  event: string;
  holdings: Filing[];
  totalValue: number;
  tickerCount: number;
  topTicker: string;
};

type TraderPortfolioHolding = {
  ticker: string;
  label: string;
  security: string;
  identifier: string;
  isTickerMapped: boolean;
  quantity: number;
  price: number;
  marketValue: number;
  costBasis: number;
  unrealizedPnl: number;
  weight: number;
};

type TraderPortfolioTransaction = {
  symbol: string;
  type: string;
  quantity: number;
  price: number;
  estimatedAmount: number;
  date: string;
  filedDate: string;
  weightBefore: number;
  weightAfter: number;
};

type TraderPortfolio = {
  investor: string;
  description: string;
  category: string;
  updated: string;
  totalValue: number;
  estimatedInvested: number;
  performance: number;
  holdingsCount: number;
  riskLevel: string;
  diversificationScore: number;
  topSectors: string[];
  holdings: TraderPortfolioHolding[];
  transactions: TraderPortfolioTransaction[];
  history: TraderPortfolioHistoryPoint[];
  sourceUrl: string;
  caveat: string;
  performanceMethodology: string;
  nextFilingDueDate: string;
};

type TraderPortfolioHistoryPoint = {
  date: string;
  value: number;
  costBasis: number;
  performance: number;
  holdingsCount?: number;
};

type SignalSourcePanel = {
  key: string;
  title: string;
  state: DataSourceState;
  count: number;
  leaders: SummaryItem[];
};

type SignalKey = "quote" | "technical" | "sepa" | "liquidity" | "valuation" | "earnings" | "options" | "filings" | "thesis" | "news" | "tradingview";

type SignalCell = {
  key: SignalKey;
  label: string;
  count: number;
  value: string;
  tone: Tone;
};

type SignalMatrixRow = {
  ticker: string;
  name: string;
  actionGrade: DecisionBucket;
  score: number;
  freshnessStatus: FreshnessStatus;
  evidenceCount: number;
  sourceCount: number;
  catalystWindow: string;
  primaryReason: string;
  blockingGates: string[];
  signals: SignalCell[];
};

type FinanceAnalysis = {
  symbol: string;
  actionGrade: DecisionBucket;
  score: number;
  sourceCount: number;
  evidenceCount: number;
  coverage: number;
  tone: Tone;
  headline: string;
  valuation: SummaryItem;
  earnings: SummaryItem;
  options: SummaryItem;
  tradingview: SummaryItem;
  missingFamilies: string[];
  blockers: string[];
  nextAction: string;
  consumable: boolean;
  decisionText: string;
};

type StressScenario = {
  label: string;
  shock: string;
  loss: number;
  caption: string;
  tone: Tone;
};

type AttentionItem = {
  symbol?: string;
  label: string;
  value: string;
  detail: string;
  action: string;
  tone: Tone;
};

type SourceCoverage = {
  key: SignalKey;
  label: string;
  count: number;
  symbolCount: number;
  leaders: string[];
  tone: Tone;
};

type SignalDefinition = {
  key: SignalKey;
  label: string;
  rows: RowRecord[];
};

type HealthRow = {
  provider: string;
  status: "Healthy" | "Warning" | "Degraded" | "Documentation";
  freshness: string;
  lastRun: string;
  sourceUrl: string;
  kind: "provider" | "freshness" | "documentation" | "run";
  contract: string;
  staleAfter: string;
};

type SummaryItem = {
  label: string;
  value: string;
  caption: string;
  tone: Tone;
  symbol?: string;
};

type DailyBriefItem = {
  itemId: string;
  category: "top_portfolio_changes" | "top_risks" | "top_opportunities" | "blocked_stale_items" | string;
  rank: number;
  title: string;
  symbol: string;
  symbols: string[];
  reason: string;
  evidence: string[];
  blocker: string;
  nextAction: string;
  score: number;
  severity: string;
  sourceModels: string[];
  asOf: string;
};

type DataSourceState = "live" | "empty";
type FreshnessStatus = "fresh" | "stale" | "degraded" | "unknown";
type DecisionBucket = "Act" | "Research" | "Watch" | "Reject" | "Stale";
const CHART_COLORS = ["#34c759", "#0a84ff", "#ffcc00", "#af52de", "#ff9f0a", "#5ac8fa", "#ff375f", "#8e8e93", "#30d158", "#64d2ff"];
const TRADINGVIEW_EXCHANGES: Record<string, string> = {
  AAPL: "NASDAQ",
  AMD: "NASDAQ",
  AMZN: "NASDAQ",
  AVGO: "NASDAQ",
  COIN: "NASDAQ",
  COST: "NASDAQ",
  CRWD: "NASDAQ",
  GOOGL: "NASDAQ",
  MSFT: "NASDAQ",
  NVDA: "NASDAQ",
  PANW: "NASDAQ",
  QQQ: "NASDAQ",
  ROK: "NYSE",
  RTX: "NYSE",
  DVN: "NYSE",
  EMR: "NYSE",
  WMB: "NYSE",
  XOM: "NYSE",
  JPM: "NYSE",
  VST: "NYSE",
  TSLA: "NASDAQ",
  TEM: "NASDAQ",
  SPY: "AMEX",
  IWM: "AMEX",
  DIA: "AMEX",
  BTC: "COINBASE",
  ETH: "COINBASE",
};

type TodayPageProps = {
  model: AppModel;
  lastRefresh: Date | null;
  loading: boolean;
  onRefresh: () => void;
  onOpenTicker: (symbol: string) => void;
};

export function TodayPage({
  model,
  lastRefresh,
  loading,
  onRefresh,
  onOpenTicker,
}: TodayPageProps) {
  const readyRows = model.decisionReadinessRows.filter((row) => stringField(row, ["status"]) === "ready");
  const blockedRows = model.decisionReadinessRows.filter((row) => stringField(row, ["status"]) !== "ready").slice(0, 5);
  const portfolioPnl = model.holdings.reduce((total, holding) => total + holding.unrealizedPnl, 0);
  const portfolioPnlPct = model.portfolioValue ? (portfolioPnl / model.portfolioValue) * 100 : 0;
  const attentionItems = buildAttentionItems(model);
  const largestHolding = model.holdings.filter((holding) => holding.hasMarketValue).slice().sort((a, b) => b.weight - a.weight)[0];
  const reviewReadyCount = model.financeAnalyses.filter((analysis) => analysis.consumable).length;
  const dailyBrief = model.dailyBriefRows;
  const topRiskItem = dailyBrief.find((item) => item.category === "top_risks");
  const changedRows = todayChangedRows(model, lastRefresh);
  const mattersRows = todayMattersRows(model, readyRows.length);
  const reviewRows = todayReviewRows(model, attentionItems);
  const ignoreRows = todayIgnoreRows(model);
  const blockedEvidenceRows = todayBlockedEvidenceRows(model, blockedRows);
  const primaryAction = topRiskItem?.nextAction || attentionItems[0]?.action || "Review only what changes sizing, thesis, or timing.";
  return (
    <section className="terminal-dashboard today-page" aria-label="Today decision brief">
      <header className="terminal-hero">
        <div className="terminal-hero-kicker">
          <span>Personal Attention Brief // {lastRefresh ? `Refreshed ${lastRefresh.toLocaleTimeString()}` : "Waiting for local data"}</span>
          <button type="button" onClick={onRefresh} aria-label="Refresh today brief" title="Refresh today brief">
            <RefreshCw size={16} className={loading ? "spin" : ""} />
          </button>
        </div>
        <strong>{todayHeadline(largestHolding, topRiskItem)}</strong>
        <div className="terminal-hero-metrics">
          <span>
            <small>PORTFOLIO P&L</small>
            <b className={portfolioPnl >= 0 ? "positive" : "negative"}>{model.holdings.length ? `${formatMoney(portfolioPnl)} (${formatPct(portfolioPnlPct)})` : "NO POSITIONS"}</b>
          </span>
          <span>
            <small>TOP EXPOSURE</small>
            <b>{largestHolding ? `${largestHolding.ticker} ${largestHolding.weight.toFixed(1)}%` : "NONE"}</b>
          </span>
          <span>
            <small>NEXT DECISION</small>
            <b>{primaryAction}</b>
          </span>
        </div>
      </header>

      {dailyBrief.length ? (
        <DailyBriefBoard items={dailyBrief} onOpenTicker={onOpenTicker} />
      ) : (
        <div className="today-answer-grid">
          <Panel title="Top Portfolio Changes">
            <SummaryList rows={changedRows} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel title="Top Risks">
            <SummaryList rows={mattersRows} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel title="Top Opportunities / Research">
            <div className="today-review-grid">
              <SummaryList rows={reviewRows} onOpenTicker={onOpenTicker} />
              <div className="ignore-list">
                <strong>Ignore for now</strong>
                <SummaryList rows={ignoreRows} onOpenTicker={onOpenTicker} />
              </div>
            </div>
          </Panel>
          <Panel title="Blocked / Stale Items">
            <SummaryList rows={blockedEvidenceRows} onOpenTicker={onOpenTicker} />
          </Panel>
        </div>
      )}

      <div className="terminal-board-grid">
        <Panel className="terminal-holdings-panel" title="Portfolio Attention">
          <TerminalHoldingsBook holdings={model.holdings} opportunities={model.opportunities.slice(0, 7)} watchlist={model.watchlist} onOpenTicker={onOpenTicker} />
        </Panel>
        {!dailyBrief.length && (
          <Panel className="terminal-signal-panel" title="Proactive Review Actions">
            <AlgorithmicSignalFeed opportunities={model.opportunities.slice(0, 4)} blockedRows={blockedRows} onOpenTicker={onOpenTicker} />
          </Panel>
        )}
      </div>

      <Panel className="terminal-risk-panel dashboard-risk-cockpit" title="Portfolio Risk & Cluster Exposure">
        <RiskProfileTerminal model={model} readyCount={readyRows.length} />
      </Panel>
      {!dailyBrief.length && <FinanceAnalysisPanel analyses={model.financeAnalyses.slice(0, 6)} onOpenTicker={onOpenTicker} />}
      {!dailyBrief.length && (
        <div className="terminal-secondary-grid">
          <Panel title="Needs Attention">
            <AttentionQueue items={attentionItems} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel title="Ready Reviews">
            <SummaryList rows={[
              { label: "Ready reviews", value: reviewReadyCount ? `${reviewReadyCount} names` : "None", caption: reviewReadyCount ? "Enough source context for dossier review." : "No consumable finance analysis rows.", tone: reviewReadyCount ? "good" : "warn" },
            ]} />
          </Panel>
        </div>
      )}
    </section>
  );
}

function todayHeadline(largestHolding: Holding | undefined, topRiskItem: DailyBriefItem | undefined): string {
  if (topRiskItem?.title) return topRiskItem.title;
  if (largestHolding) return `${largestHolding.ticker} is ${largestHolding.weight.toFixed(1)}% of priced portfolio`;
  return "Portfolio attention brief";
}

function DailyBriefBoard({ items, onOpenTicker }: { items: DailyBriefItem[]; onOpenTicker: (symbol: string) => void }) {
  const groups = ([
    ["top_portfolio_changes", "Portfolio Moves"],
    ["top_risks", "Risk Decisions"],
    ["top_opportunities", "Research Candidates"],
    ["blocked_stale_items", "Before Adding"],
  ] satisfies Array<[DailyBriefItem["category"], string]>).filter(([category]) => items.some((item) => item.category === category));
  if (!groups.length) {
    return <EmptyState title="No decision brief" detail="No portfolio, risk, or research item currently changes today's action list." />;
  }
  return (
    <section className="daily-brief-board" aria-label="Backend daily brief">
      {groups.map(([category, title]) => {
        const groupItems = items.filter((item) => item.category === category).slice(0, 5);
        return (
          <Panel key={category} title={title}>
            {groupItems.length ? (
              <div className="daily-brief-stack">
                {groupItems.map((item) => (
                  <DailyBriefCard key={item.itemId} item={item} onOpenTicker={onOpenTicker} />
                ))}
              </div>
            ) : (
              <EmptyState title="No ranked items" detail="The backend daily_brief model has no rows for this category." />
            )}
          </Panel>
        );
      })}
    </section>
  );
}

function DailyBriefCard({ item, onOpenTicker }: { item: DailyBriefItem; onOpenTicker: (symbol: string) => void }) {
  const symbol = item.symbol || item.symbols[0] || "";
  const blockerActive = item.blocker && item.blocker.toLowerCase() !== "none";
  return (
    <article className={`daily-brief-card ${briefTone(item)}`}>
      <button type="button" disabled={!symbol} onClick={() => symbol && onOpenTicker(symbol)}>
        <span>{String(item.rank).padStart(2, "0")}</span>
        <strong>{item.title}</strong>
        {symbol && <ChevronRight size={14} />}
      </button>
      <p>{item.reason}</p>
      <div className="daily-brief-detail-grid">
        <div>
          <span>Evidence</span>
          <small>{item.evidence.slice(0, 3).join(" · ") || "No evidence rows"}</small>
        </div>
        {blockerActive && (
          <div>
            <span>Before Acting</span>
            <small className="negative">{item.blocker}</small>
          </div>
        )}
        <div>
          <span>Do Next</span>
          <small>{item.nextAction}</small>
        </div>
      </div>
    </article>
  );
}

function briefTone(item: DailyBriefItem): Tone {
  const severity = item.severity.toLowerCase();
  if (severity === "bad" || severity === "critical") return "bad";
  if (severity === "good") return "good";
  if (severity === "watch" || severity === "warn") return "warn";
  return "info";
}

export function TickerPage({ symbol, ticker, model, data }: { symbol: string; ticker: TickerPayload | null; model: AppModel; data: PanelData; onOpenTicker: (symbol: string) => void }) {
  const [activeTab, setActiveTab] = useState("Overview");
  useEffect(() => {
    setActiveTab("Overview");
  }, [symbol]);
  const tickerScopedData = useMemo(() => ticker ? panelDataWithTickerTables(data, ticker) : data, [data, ticker]);
  const dossierModel = useMemo(() => ticker ? buildModel(tickerScopedData) : model, [model, ticker, tickerScopedData]);
  const opportunity = dossierModel.opportunities.find((item) => item.ticker === symbol);
  const quote = dossierModel.watchlist.find((item) => item.symbol === symbol);
  const setup = dossierModel.setupRows.find((item) => item.symbol === symbol);
  const liquidity = dossierModel.liquidityRows.find((item) => item.symbol === symbol);
  const valuation = dossierModel.valuationRows.find((item) => item.symbol === symbol);
  const technical = dossierModel.technicalRows.find((item) => item.symbol === symbol);
  const financeAnalysis = dossierModel.financeAnalyses.find((item) => item.symbol === symbol);
  const evidenceRows = ticker?.tables ? Object.entries(ticker.tables).filter(([, tableRows]) => tableRows?.length).length : 0;
  const foundTables = ticker?.tables ? Object.entries(ticker.tables).filter(([, tableRows]) => tableRows?.length).map(([name]) => name) : [];
  const signalRow = dossierModel.signalMatrix.find((row) => row.ticker === symbol);
  const decisionBrief = objectField(ticker?.decision_brief);
  const thesisMonitor = dossierModel.thesisMonitorRows.find((row) => stringField(row, ["symbol"]) === symbol);
  const recommendation = dossierModel.agentRecommendationRows.find((row) => stringField(row, ["symbol"]) === symbol);
  const showBrokerSurface = brokerProviderSurfaceEnabled(dossierModel);
  const dossierTabs = [
    "Overview",
    "Evidence Stack",
    ...(showBrokerSurface ? ["Broker"] : []),
    "Fundamentals",
    "Estimates",
    "Financials",
    "Options",
    "News",
    "Filings",
    "Memos",
  ];
  const activeDossierTab = dossierTabs.includes(activeTab) ? activeTab : "Overview";

  return (
    <PageFrame
      eyebrow="Ticker Detail / Evidence Dossier"
      title={symbol}
      subtitle={[opportunity?.name || companyName(symbol), titleLabel(opportunity?.assetClass ?? "instrument"), titleLabel(opportunity?.category ?? "watchlist")].filter(Boolean).join(" · ")}
      action={
        <div className="ticker-actions">
          <MetricBadge label="Grade" value={opportunity?.grade ?? "-"} tone={opportunity ? "good" : "muted"} />
          <MetricBadge label="Action" value={opportunity?.actionGrade ?? "-"} tone={opportunity ? (opportunity.actionGrade === "Act" ? "good" : opportunity.actionGrade === "Stale" || opportunity.actionGrade === "Reject" ? "bad" : "warn") : "muted"} />
          <MetricBadge label="Freshness" value={opportunity ? titleLabel(opportunity.freshnessStatus) : "-"} tone={opportunity ? freshnessTone(opportunity.freshnessStatus) : "muted"} />
          <IconButton label="Watch">
            <Star size={15} />
          </IconButton>
        </div>
      }
    >
      <TickerDossierHeader
        symbol={symbol}
        decisionBrief={decisionBrief}
        quote={quote}
        opportunity={opportunity}
        evidenceRows={evidenceRows}
        foundTables={foundTables}
        tickerFound={Boolean(ticker?.found)}
      />
      {showBrokerSurface && <BrokerAgentDossier
        symbol={symbol}
        recommendation={recommendation}
        statusRows={dossierModel.brokerStatusRows}
        accountRows={dossierModel.brokerAccountRows}
        positionRows={dossierModel.brokerPositionRows.filter((row) => stringField(row, ["symbol"]) === symbol)}
        signalRows={dossierModel.brokerSignalRows.filter((row) => stringField(row, ["symbol"]) === symbol)}
      />}
      <DecisionTicket brief={decisionBrief} opportunity={opportunity} />
      {signalRow && <TickerSignalRibbon row={signalRow} brief={decisionBrief} />}
      <TabBar tabs={dossierTabs} active={activeDossierTab} onSelect={setActiveTab} />
      {activeDossierTab === "Overview" ? <div className="ticker-grid">
        <DecisionBriefOverview brief={decisionBrief} opportunity={opportunity} />
        <ThesisStatePanel row={thesisMonitor} />
        <TradeSetupPanel brief={decisionBrief} />
        <Panel className="span-5" title="Evidence Context">
          <ChartContextSummary brief={decisionBrief} />
        </Panel>
        <Panel className="span-5" title="Price & Setup Source Rows">
          <SummaryList rows={[
            { label: "Canonical Quote", value: canonicalQuoteLabel(decisionBrief) || quote?.price || "-", caption: canonicalQuoteCaption(decisionBrief) || "No canonical quote", tone: canonicalQuoteLabel(decisionBrief) ? "info" : "warn" },
            { label: "Catalyst", value: opportunity?.catalystWindow ?? "-", caption: "Nearest visible event window", tone: opportunity?.catalystWindow && opportunity.catalystWindow !== "-" ? "info" : "muted" },
            { label: "Portfolio", value: opportunity?.portfolioImpact ?? "-", caption: opportunity?.owned ? "Owned exposure" : "Unowned", tone: opportunity?.owned ? "warn" : "muted" },
            technical ? { ...technical, label: "Technical Score" } : { label: "Technical Score", value: "-", caption: "No technical feature row", tone: "warn" },
            setup ?? { label: "SEPA Setup", value: "-", caption: "No setup row", tone: "warn" },
            liquidity ?? { label: "Liquidity", value: opportunity?.liquidity ?? "-", caption: "No liquidity row", tone: "warn" },
            valuation ?? { label: "Valuation", value: "-", caption: "No valuation row", tone: "warn" },
          ]} />
        </Panel>
        <RiskPlanPanel brief={decisionBrief} />
        <Panel className="span-4" title="Options Viability">
          <OptionsContextSummary brief={decisionBrief} />
        </Panel>
        <Panel className="span-4" title="Portfolio Fit">
          <PortfolioFitSummary brief={decisionBrief} />
        </Panel>
        <Panel className="span-5" title="Score Breakdown">
          <div className="score-total">
            <span>Total Score</span>
            <strong>{opportunity?.score ?? "-"}<small>/100</small></strong>
          </div>
          <BarList rows={opportunity?.components.length ? opportunity.components : []} showValue />
          <TextLink>View details</TextLink>
        </Panel>
        <InfoPanel tone="good" title="Why Now" items={splitSignalText(opportunity?.whyNow)} />
        <InfoPanel tone="bad" title="Invalidation" items={splitSignalText(opportunity?.invalidation)} />
        <InfoPanel tone={opportunity?.blockingGates.length ? "warn" : "info"} title="Gates / Next Action" items={opportunity?.blockingGates.length ? opportunity.blockingGates.map(formatGateLabel) : splitSignalText(opportunity?.nextAction)} />
        <Panel className="span-12" title="Finance-Skill Analysis">
          {financeAnalysis ? <FinanceAnalysisCard analysis={financeAnalysis} onOpenTicker={() => undefined} expanded /> : <EmptyState title="No finance-skill analysis" detail="Valuation, earnings, options, and source-context rows have not loaded for this ticker." />}
        </Panel>
        <Panel className="span-12" title="Evidence Snapshot">
          <div className="snapshot-grid">
            <MetricBadge label="Evidence Count" value={String(opportunity?.evidenceCount ?? 0)} caption={`${opportunity?.sourceCount ?? 0} sources`} tone={(opportunity?.evidenceCount ?? 0) > 0 ? "good" : "warn"} />
            <MetricBadge label="API Tables" value={String(evidenceRows)} caption={foundTables.slice(0, 2).join(", ") || "No rows"} tone={evidenceRows ? "good" : "warn"} />
            <MetricBadge label="Source Cluster" value={opportunity?.sourceCluster ?? "-"} caption="Primary source mix" tone="info" />
            <MetricBadge label="Technical" value={`${componentValue(opportunity, "technical")}%`} caption="Source score" tone="info" />
            <MetricBadge label="Thesis" value={`${componentValue(opportunity, "thesis")}%`} caption="Source score" tone={componentValue(opportunity, "thesis") >= 50 ? "good" : "warn"} />
            <MetricBadge label="Decision" value={opportunity?.actionGrade ?? "None"} caption={opportunity?.decisionBasis ?? "Backend signal"} tone="info" />
          </div>
          {opportunity && <BulletList tone={opportunity.isStale || opportunity.isSourceThin ? "warn" : "info"} items={opportunity.inclusionReasons} />}
        </Panel>
      </div> : <TickerTabContent activeTab={activeDossierTab} ticker={ticker} data={data} decisionBrief={decisionBrief} />}
    </PageFrame>
  );
}

export function PortfolioPage({ model, onOpenTicker, onRefresh }: { model: AppModel; onOpenTicker: (symbol: string) => void; onRefresh: () => Promise<void> }) {
  const [heatmapMode, setHeatmapMode] = useState("P/L");
  const [allocationFilter, setAllocationFilter] = useState("");
  const hasHoldings = model.holdings.length > 0;
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const unpricedHoldings = model.holdings.length - pricedHoldings.length;
  const portfolioPnl = model.holdings.reduce((total, holding) => total + (holding.hasPnl ? holding.unrealizedPnl : 0), 0);
  const portfolioDayChange = model.holdings.reduce((total, holding) => total + (Number.isFinite(holding.dayChangeValue) ? holding.dayChangeValue : 0), 0);
  const portfolioCostBasis = model.holdings.reduce((total, holding) => total + holding.quantity * holding.averageCost, 0);
  const largestHolding = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  const visibleHoldings = allocationFilter
    ? model.holdings.filter((holding) => portfolioAllocationBucket(holding) === allocationFilter)
    : model.holdings;
  const stats = summarizePortfolio(model.holdings);
  const portfolioReviewRows = portfolioPositionReviewRows(visibleHoldings, model.valuationRows, model.technicalRows);
  const riskRows = portfolioRiskRows(visibleHoldings, model.liquidityRows, model.correlationRows);
  const valuationRows = portfolioValuationRows(visibleHoldings, model.valuationRows, model.technicalRows);
  const taxRows = portfolioTaxRows(visibleHoldings);
  const showBrokerSurface = brokerProviderSurfaceEnabled(model);
  const sourceItems: Array<[string, DataSourceState]> = [
    ["Holdings", model.sources.holdings],
    ["Quotes", model.sources.watchlist],
    ["Analysis", model.valuationRows.length || model.liquidityRows.length ? "live" : "empty"],
    ["Risk Models", model.portfolioRiskCardRows.length || model.exposureClusterRows.length ? "live" : "empty"],
  ];
  if (showBrokerSurface) {
    sourceItems.splice(1, 0, ["Broker", "live"]);
  }
  const metrics: Array<[string, string, string, Tone | string]> = [
    ["Market Value", hasHoldings ? formatMoney(model.portfolioValue) : "Not imported", unpricedHoldings ? `${unpricedHoldings} holding${unpricedHoldings === 1 ? "" : "s"} missing price` : hasHoldings ? "Priced holdings only" : "Portfolio CSV absent", unpricedHoldings ? "warn" : hasHoldings ? "good" : "warn"],
    ["Day Move", hasHoldings ? formatMoney(portfolioDayChange) : "Requires prices", hasHoldings ? "From latest quote change" : "Manual entry below", portfolioDayChange >= 0 ? "good" : "bad"],
    ["Unrealized P/L", hasHoldings ? formatMoney(portfolioPnl) : "Requires import", unpricedHoldings ? "Excludes unpriced holdings" : hasHoldings ? "From priced position rows" : "Requires cost basis", unpricedHoldings ? "warn" : hasHoldings ? "good" : "muted"],
    ["Cost Basis", hasHoldings ? formatMoney(portfolioCostBasis) : "Requires import", hasHoldings ? "Quantity times average cost" : "No owned exposure", "info"],
  ];
  if (showBrokerSurface) {
    metrics.splice(1, 0, ["Broker Accounts", String(model.brokerAccountRows.length), model.brokerStatusRows[0] ? stringField(model.brokerStatusRows[0], ["status", "detail"]) : "Broker sync not loaded", model.brokerStatusRows.some((row) => stringField(row, ["status"]) === "ok") ? "good" : "warn"]);
  }
  const workspacePanels = (
    <>
      <Panel className="span-12" title="Risk & Positioning">
        <PortfolioRiskRibbon stats={stats} />
      </Panel>
      <Panel className="span-4" title="Allocation">
        <PortfolioAllocationPanel holdings={model.holdings} activeBucket={allocationFilter} onBucketSelect={setAllocationFilter} />
      </Panel>
      <Panel className="span-8" title="Exposure Map" headerAction={<SegmentedControl options={["P/L", "Weight", "Day"]} value={heatmapMode} onChange={setHeatmapMode} />}>
        <PortfolioHeatmap holdings={visibleHoldings} mode={heatmapMode} onOpenTicker={onOpenTicker} />
      </Panel>
      <Panel className="span-12" title="Performance">
        <PortfolioPerformanceChart holdings={visibleHoldings} stats={stats} />
      </Panel>
      <Panel className="span-12" title="Correlation Matrix">
        <PortfolioCorrelationMatrix holdings={visibleHoldings} rows={model.correlationEdgeRows} onOpenTicker={onOpenTicker} />
      </Panel>
    </>
  );
  return (
    <PageFrame
      title="Portfolio Risk"
      subtitle={hasHoldings ? `As of ${new Date().toLocaleDateString()}` : "Enter your real positions to enable portfolio analytics"}
    >
      <SourceNotice items={sourceItems} />
      <MetricStrip metrics={metrics} />
      {showBrokerSurface && <div className="portfolio-grid broker-grid">
        <Panel className="span-4" title="Broker Health">
          <SummaryList rows={brokerStatusSummaryRows(model.brokerStatusRows)} />
        </Panel>
        <Panel className="span-4" title="Account Source">
          <SummaryList rows={brokerAccountSummaryRows(model.brokerAccountRows)} />
        </Panel>
        <Panel className="span-4" title="Paper Orders">
          <SummaryList rows={paperOrderSummaryRows(model.paperOrderRows)} onOpenTicker={onOpenTicker} />
        </Panel>
      </div>}
      {!hasHoldings ? (
        <>
          <div className="portfolio-grid">
            {workspacePanels}
          </div>
          <div className="portfolio-onboarding">
            <Panel title="Add First Position">
              <div className="portfolio-onboarding-copy">
                <strong>Build the local portfolio model from explicit positions.</strong>
                <p>Manual entries write to DuckDB and unlock exposure, liquidity, tax-lot, and signal-fit checks without broker credentials.</p>
              </div>
              <PortfolioEntryForm onSaved={onRefresh} />
            </Panel>
            <Panel title="Workspace Readiness">
              <BulletList tone="info" items={["Add ticker, shares, average cost, and purchase date.", "The risk ribbon, allocation view, exposure map, and sortable blotter populate from owned rows.", "Signal decisions continue to show unowned candidates separately."]} />
            </Panel>
          </div>
        </>
      ) : (
        <div className="portfolio-grid">
          {workspacePanels}
          <Panel className="span-12" title={`Holdings (${model.holdings.length})`}>
            {allocationFilter && <button className="text-link portfolio-filter-clear" type="button" onClick={() => setAllocationFilter("")}>Showing {allocationFilter}; clear filter</button>}
            <HoldingsTable holdings={visibleHoldings} onOpenTicker={onOpenTicker} onDelete={onRefresh} />
          </Panel>
          <Panel className="span-8" title="Risk Cards">
            <PortfolioRiskCards rows={model.portfolioRiskCardRows} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel className="span-4" title="Review Actions">
            <PortfolioReviewActions rows={model.reviewActionRows} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel className="span-12" title="Exposure Clusters">
            <PortfolioExposureClusters rows={model.exposureClusterRows} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel className="span-4" title="Add / Update Position">
            <PortfolioEntryForm onSaved={onRefresh} />
          </Panel>
          <Panel className="span-8" title="Position Review">
            <SummaryList rows={portfolioReviewRows} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel className="span-4" title="Risk & Liquidity">
            {riskRows.length ? <SummaryList rows={riskRows} onOpenTicker={onOpenTicker} /> : <EmptyState title="No owned-symbol risk rows" detail="Refresh analyses after portfolio prices are loaded." />}
          </Panel>
          <Panel className="span-8" title="Valuation & Momentum">
            {valuationRows.length ? <SummaryList rows={valuationRows} onOpenTicker={onOpenTicker} /> : <EmptyState title="No owned-symbol valuation rows" detail="Refresh yfinance and deterministic analyses for current holdings." />}
          </Panel>
          <Panel className="span-4" title="Holding Periods">
            <SummaryList rows={taxRows} onOpenTicker={onOpenTicker} />
          </Panel>
        </div>
      )}
    </PageFrame>
  );
}

function BrokerAgentDossier({
  symbol,
  recommendation,
  statusRows,
  accountRows,
  positionRows,
  signalRows,
}: {
  symbol: string;
  recommendation?: RowRecord;
  statusRows: RowRecord[];
  accountRows: RowRecord[];
  positionRows: RowRecord[];
  signalRows: RowRecord[];
}) {
  const blockers = recommendation ? arrayField(recommendation.blockers).map((item) => displayValue(item as JsonValue)).filter(Boolean) : [];
  const preview = objectField(recommendation?.paper_order_preview);
  const status = stringField(recommendation ?? {}, ["status", "action"]) || "not_loaded";
  return (
    <div className="ticker-grid broker-dossier-grid">
      <Panel className="span-4" title="Broker Health">
        <SummaryList rows={brokerStatusSummaryRows(statusRows)} />
      </Panel>
      <Panel className="span-4" title="Portfolio Exposure">
        <SummaryList rows={[
          positionRows[0]
            ? {
              label: symbol,
              value: formatMoney(numberField(positionRows[0], ["market_value"], 0)),
              caption: `${formatNumber(numberField(positionRows[0], ["quantity"], 0))} shares via ${stringField(positionRows[0], ["provider"]) || "broker"}`,
              tone: "info",
              symbol,
            }
            : { label: symbol, value: "No broker position", caption: accountRows.length ? "IBKR account loaded; no current exposure row" : "Account sync not loaded", tone: "muted" },
        ]} />
      </Panel>
      <Panel className="span-4" title="Paper Trade Plan">
        {recommendation ? (
          <SummaryList rows={[
            { label: "Agent Status", value: titleLabel(status), caption: stringField(recommendation, ["action"]) || "advisory only", tone: blockers.length ? "warn" : status === "paper_ready" ? "good" : "info" },
            { label: "Max Notional", value: formatMoney(numberField(recommendation, ["max_notional"], 0)), caption: `Preview ${displayValue(preview.quantity)} @ ${displayValue(preview.limit_price)}`, tone: "info" },
            { label: "Blockers", value: String(blockers.length), caption: blockers.slice(0, 2).join(", ") || "No active safety blocker", tone: blockers.length ? "bad" : "good" },
          ]} />
        ) : (
          <EmptyState title="No agent recommendation" detail="Run the broker source refresh or agent review to materialize a paper-only trade plan." />
        )}
      </Panel>
      {signalRows.length > 0 && (
        <Panel className="span-12" title="moomoo Supplemental Signals">
          <SummaryList rows={signalRows.slice(0, 8).map((row) => ({
            label: stringField(row, ["signal_type", "provider"]) || "scanner",
            value: displayValue(row.score ?? row.rank),
            caption: displayValue(row.metrics ?? row.observed_at),
            tone: "info",
            symbol,
          }))} />
        </Panel>
      )}
    </div>
  );
}

function brokerProviderSurfaceEnabled(model: AppModel): boolean {
  return model.brokerStatusRows.some((row) => {
    const provider = stringField(row, ["provider"]).toLowerCase();
    const status = stringField(row, ["status"]).toLowerCase();
    const detail = stringField(row, ["detail"]).toLowerCase();
    if (!["ibkr", "moomoo"].includes(provider)) return false;
    if (status === "disabled" || status === "not_configured") return false;
    return !detail.includes("disabled");
  });
}

function isBrokerHealthRow(row: HealthRow): boolean {
  return row.provider.toLowerCase().startsWith("broker:") || row.contract.toLowerCase().includes("broker/account");
}

function brokerStatusSummaryRows(statusRows: RowRecord[]): SummaryItem[] {
  if (!statusRows.length) {
    return [{ label: "IBKR", value: "Not synced", caption: "Broker source has not produced a health row.", tone: "warn" }];
  }
  return statusRows.map((row) => {
    const status = stringField(row, ["status", "health"]) || "unknown";
    return {
      label: stringField(row, ["provider"]) || "broker",
      value: titleLabel(status),
      caption: stringField(row, ["detail"]) || stringField(row, ["checked_at"]) || "No detail",
      tone: status === "ok" ? "good" : status === "disabled" ? "warn" : "bad",
    };
  });
}

function brokerAccountSummaryRows(accountRows: RowRecord[]): SummaryItem[] {
  if (!accountRows.length) {
    return [{ label: "IBKR Account", value: "Optional", caption: "Manual portfolio and market-data advisory mode remain available.", tone: "info" }];
  }
  return accountRows.slice(0, 4).map((row) => ({
    label: stringField(row, ["account_id", "provider"]) || "Account",
    value: formatMoney(numberField(row, ["net_liquidation", "cash"], 0)),
    caption: `Buying power ${formatMoney(numberField(row, ["buying_power"], 0))} · ${stringField(row, ["account_mode"]) || "mode unknown"}`,
    tone: "info",
  }));
}

function agentRecommendationSummaryRows(recommendationRows: RowRecord[]): SummaryItem[] {
  if (!recommendationRows.length) {
    return [{ label: "Agent Review", value: "No rows", caption: "Run update_broker_sources or /api/agent/review.", tone: "warn" }];
  }
  return recommendationRows.slice(0, 10).map((row) => {
    const blockers = arrayField(row.blockers);
    const status = stringField(row, ["status", "action"]) || "unknown";
    return {
      label: stringField(row, ["symbol"]) || "Ticker",
      value: `${Math.round(numberField(row, ["actionability_score"], 0))} · ${titleLabel(status)}`,
      caption: blockers.length ? `${blockers.length} blocker${blockers.length === 1 ? "" : "s"}: ${blockers.slice(0, 2).map((item) => displayValue(item as JsonValue)).join(", ")}` : stringField(row, ["setup_type", "entry_trigger"]),
      tone: blockers.length ? "bad" : status === "paper_ready" ? "good" : "info",
      symbol: stringField(row, ["symbol"]),
    };
  });
}

function paperOrderSummaryRows(orderRows: RowRecord[]): SummaryItem[] {
  if (!orderRows.length) {
    return [{ label: "Paper Orders", value: "None staged", caption: "V1 does not expose live trading.", tone: "muted" }];
  }
  return orderRows.slice(0, 5).map((row) => ({
    label: stringField(row, ["symbol"]) || "Paper",
    value: titleLabel(stringField(row, ["status"]) || "staged"),
    caption: `${stringField(row, ["side"]) || "BUY"} ${displayValue(row.quantity)} @ ${displayValue(row.limit_price)} · ${stringField(row, ["created_at"])}`,
    tone: stringField(row, ["status"]) === "blocked" ? "bad" : "info",
    symbol: stringField(row, ["symbol"]),
  }));
}

function PortfolioPerformanceChart({ holdings, stats }: { holdings: Holding[]; stats: PortfolioStats }) {
  const points = portfolioPerformancePoints(holdings, stats);
  if (!holdings.length || !points.length) {
    return <EmptyState title="No performance series" detail="Add priced holdings to compare current value against cost basis and latest day move." />;
  }
  return (
    <div className="portfolio-performance-chart">
      <ResponsiveContainer width="100%" height={230}>
        <LineChart data={points} margin={{ top: 10, right: 18, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#e5e7eb" vertical={false} />
          <XAxis dataKey="label" tick={{ fill: "#475569", fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: "#475569", fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={(value) => formatCompactMoney(Number(value))} width={58} />
          <Tooltip formatter={(value, name) => [formatMoney(Number(value)), name === "value" ? "Portfolio Value" : "Cost Basis"]} />
          <Legend />
          <Line type="monotone" dataKey="costBasis" name="Cost Basis" stroke="#64748b" strokeWidth={2} dot={{ r: 3 }} />
          <Line type="monotone" dataKey="value" name="Portfolio Value" stroke="#2563eb" strokeWidth={3} dot={{ r: 4 }} />
        </LineChart>
      </ResponsiveContainer>
      <div className="portfolio-performance-metrics">
        <MetricBadge label="Open Return" value={formatPct(stats.unrealizedPnlPct)} caption={formatMoney(stats.unrealizedPnl)} tone={stats.unrealizedPnl >= 0 ? "good" : "bad"} />
        <MetricBadge label="Day Move" value={formatPct(stats.dayChangePct)} caption={formatMoney(stats.dayChange)} tone={stats.dayChange >= 0 ? "good" : "bad"} />
        <MetricBadge label="Cost Basis" value={formatMoney(stats.costBasis)} caption={`${stats.pricedCount}/${stats.totalCount} priced`} tone="info" />
      </div>
    </div>
  );
}

function PortfolioCorrelationMatrix({ holdings, rows, onOpenTicker }: { holdings: Holding[]; rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  const symbols = portfolioCorrelationMatrixSymbols(holdings, rows);
  if (symbols.length < 2) {
    return <EmptyState title="No correlation matrix" detail="Load correlation edges for at least one owned holding and a peer." />;
  }
  const correlations = portfolioCorrelationLookup(rows);
  return (
    <div className="portfolio-correlation-matrix-wrap">
      <div className="portfolio-correlation-matrix" style={{ gridTemplateColumns: `minmax(76px, 0.7fr) repeat(${symbols.length}, minmax(56px, 1fr))` }}>
        <div className="portfolio-correlation-corner">Corr</div>
        {symbols.map((symbol) => (
          <button key={`head-${symbol}`} type="button" className="portfolio-correlation-axis top" onClick={() => onOpenTicker(symbol)}>
            {symbol}
          </button>
        ))}
        {symbols.map((rowSymbol) => (
          <Fragment key={`matrix-row-${rowSymbol}`}>
            <button key={`row-${rowSymbol}`} type="button" className="portfolio-correlation-axis side" onClick={() => onOpenTicker(rowSymbol)}>
              {rowSymbol}
            </button>
            {symbols.map((columnSymbol) => {
              const correlation = rowSymbol === columnSymbol ? 1 : correlations.get(`${rowSymbol}:${columnSymbol}`);
              const label = correlation === undefined ? "-" : correlation.toFixed(2);
              return (
                <button
                  key={`${rowSymbol}-${columnSymbol}`}
                  type="button"
                  className={`portfolio-correlation-cell ${portfolioCorrelationTone(correlation)}`}
                  disabled={correlation === undefined}
                  onClick={() => onOpenTicker(rowSymbol)}
                  title={correlation === undefined ? `${rowSymbol}/${columnSymbol} not loaded` : `${rowSymbol}/${columnSymbol} correlation ${label}`}
                >
                  {label}
                </button>
              );
            })}
          </Fragment>
        ))}
      </div>
      <div className="portfolio-correlation-legend" aria-label="Correlation scale">
        <span><i className="negative" /> inverse</span>
        <span><i className="neutral" /> low</span>
        <span><i className="positive" /> medium</span>
        <span><i className="strong" /> high</span>
      </div>
    </div>
  );
}

function PortfolioRiskCards({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No risk cards" detail="Portfolio risk models did not flag a current review item." />;
  }
  return (
    <div className="portfolio-risk-card-list">
      {rows.slice(0, 5).map((row, index) => {
        const severity = stringField(row, ["severity"]) || "info";
        const symbols = arrayField(row.symbols).map((item) => displayValue(item as JsonValue)).filter(Boolean);
        const evidence = arrayField(row.evidence).map((item) => displayValue(item as JsonValue)).filter(Boolean).slice(0, 4);
        const symbol = stringField(row, ["symbol"]) || symbols[0];
        return (
          <button key={stringField(row, ["card_id"]) || `${symbol}-${index}`} type="button" className={`portfolio-risk-card-row ${severity}`} disabled={!symbol} onClick={() => symbol && onOpenTicker(symbol)}>
            <div className="portfolio-risk-card-heading">
              <span>{titleLabel(stringField(row, ["risk_type"]) || "Risk")}</span>
              <strong>{stringField(row, ["title"]) || "Portfolio risk"}</strong>
            </div>
            <div className="portfolio-risk-card-metric">
              <span>{stringField(row, ["impact"]) || `${numberField(row, ["portfolio_weight"], 0).toFixed(1)}% weight`}</span>
              <b>{Math.round(numberField(row, ["score"], 0))}</b>
            </div>
            <p>{stringField(row, ["summary"])}</p>
            <div className="portfolio-risk-card-evidence">
              {evidence.map((item) => <i key={item}>{item}</i>)}
            </div>
            <small><b>Trigger:</b> {stringField(row, ["trigger"]) || "risk model threshold"} · <b>Next:</b> {stringField(row, ["next_step", "review_action"])}</small>
          </button>
        );
      })}
    </div>
  );
}

function PortfolioExposureClusters({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  const visibleRows = portfolioDisplayExposureClusters(rows);
  if (!visibleRows.length) {
    return <EmptyState title="No actionable clusters" detail="Priced holdings and sector or industry metadata are required." />;
  }
  return (
    <div className="portfolio-cluster-ledger">
      {visibleRows.map((row, index) => {
        const symbols = arrayField(row.symbols).map((item) => displayValue(item as JsonValue)).filter(Boolean);
        const evidence = arrayField(row.evidence).map((item) => displayValue(item as JsonValue)).filter(Boolean).slice(0, 4);
        const symbol = stringField(row, ["largest_symbol"]) || symbols[0];
        const weight = numberField(row, ["portfolio_weight"], 0);
        const level = stringField(row, ["concentration_level"]) || "normal";
        return (
          <button key={stringField(row, ["cluster_id"]) || `${symbol}-${index}`} type="button" className={`portfolio-cluster-row ${level}`} disabled={!symbol} onClick={() => symbol && onOpenTicker(symbol)}>
            <div>
              <span>{titleLabel(stringField(row, ["cluster_type"]) || "Cluster")}</span>
              <strong>{stringField(row, ["cluster_name"]) || "Unclassified"}</strong>
              <small>{stringField(row, ["risk_readout", "risk_note"])}</small>
            </div>
            <div className="portfolio-cluster-weight">
              <b>{weight.toFixed(1)}%</b>
              <span>{symbols.join(" / ")}</span>
            </div>
            <div className="portfolio-cluster-tags">
              {evidence.map((item) => <i key={item}>{item}</i>)}
            </div>
            <p>{stringField(row, ["next_step"])}</p>
          </button>
        );
      })}
    </div>
  );
}

function PortfolioReviewActions({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No review actions" detail="No portfolio risk action is currently open." />;
  }
  return (
    <div className="portfolio-action-list">
      {rows.slice(0, 6).map((row, index) => {
        const priority = stringField(row, ["priority"]) || "medium";
        const symbols = arrayField(row.symbols).map((item) => displayValue(item as JsonValue)).filter(Boolean);
        const symbol = stringField(row, ["symbol"]) || symbols[0];
        return (
          <button key={stringField(row, ["action_id"]) || `${symbol}-${index}`} type="button" className={`portfolio-action-row ${priority}`} disabled={!symbol} onClick={() => symbol && onOpenTicker(symbol)}>
            <span>{titleLabel(priority)}</span>
            <strong>{stringField(row, ["title"]) || titleLabel(stringField(row, ["action_type"]) || "Review")}</strong>
            <small>{stringField(row, ["suggested_next_step"])}</small>
            <i>{stringField(row, ["impact"]) || symbols.join(" / ") || titleLabel(stringField(row, ["risk_type"]) || "risk")}</i>
          </button>
        );
      })}
    </div>
  );
}

function PortfolioRiskRibbon({ stats }: { stats: PortfolioStats }) {
  const riskTone: Tone = stats.riskScore >= 70 ? "bad" : stats.riskScore >= 45 ? "warn" : "good";
  return (
    <div className="portfolio-risk-ribbon">
      <div className="portfolio-risk-hero">
        <span>Portfolio Value</span>
        <strong>{formatMoney(stats.portfolioValue)}</strong>
        <small>{stats.pricedCount}/{stats.totalCount} priced holdings</small>
      </div>
      <div className={`portfolio-risk-hero ${stats.unrealizedPnl >= 0 ? "good" : "bad"}`}>
        <span>Unrealized P/L</span>
        <strong>{formatMoney(stats.unrealizedPnl)}</strong>
        <small>{formatPct(stats.unrealizedPnlPct)} · {stats.gainers} up / {stats.losers} down</small>
      </div>
      <div className={`portfolio-risk-hero ${stats.dayChange >= 0 ? "good" : "bad"}`}>
        <span>Today</span>
        <strong>{formatMoney(stats.dayChange)}</strong>
        <small>{formatPct(stats.dayChangePct)}</small>
      </div>
      <div className="portfolio-risk-grid">
        <MetricBadge label="Top 3 Weight" value={`${stats.top3Weight.toFixed(1)}%`} caption={stats.largest ? `${stats.largest.ticker} leads` : "No priced rows"} tone={stats.top3Weight > 65 ? "warn" : "info"} />
        <MetricBadge label="Risk Score" value={String(Math.round(stats.riskScore))} caption="Concentration and data gaps" tone={riskTone} />
        <MetricBadge label="Quote Gaps" value={String(stats.quoteGapCount)} caption="Missing or stale rows" tone={stats.quoteGapCount ? "warn" : "good"} />
        <MetricBadge label="Short Term" value={`${stats.shortTermWeight.toFixed(1)}%`} caption={`${stats.longTermWeight.toFixed(1)}% long term`} tone={stats.shortTermWeight > 50 ? "warn" : "info"} />
      </div>
    </div>
  );
}

function PortfolioAllocationPanel({ holdings, activeBucket, onBucketSelect }: { holdings: Holding[]; activeBucket: string; onBucketSelect: (bucket: string) => void }) {
  const buckets = portfolioAllocationBuckets(holdings);
  if (!buckets.length) {
    return <EmptyState title="No allocation rows" detail="Priced holdings are required for allocation weights." />;
  }
  const total = buckets.reduce((sum, bucket) => sum + bucket.value, 0);
  return (
    <div className="portfolio-allocation">
      <div className="portfolio-donut">
        <ResponsiveContainer width="100%" height={190}>
          <PieChart>
            <Pie data={buckets} dataKey="value" nameKey="name" innerRadius={48} outerRadius={78} paddingAngle={2}>
              {buckets.map((bucket, index) => <Cell key={bucket.name} fill={PORTFOLIO_CHART_COLORS[index % PORTFOLIO_CHART_COLORS.length]} stroke="#ffffff" strokeWidth={2} />)}
            </Pie>
            <Tooltip formatter={(value) => [`${Number(value ?? 0).toFixed(1)}%`, "Weight"]} />
          </PieChart>
        </ResponsiveContainer>
        <div className="portfolio-donut-center">
          <strong>{total.toFixed(0)}%</strong>
          <span>priced</span>
        </div>
      </div>
      <div className="portfolio-allocation-list">
        {buckets.map((bucket, index) => (
          <button key={bucket.name} className={activeBucket === bucket.name ? "active" : ""} type="button" onClick={() => onBucketSelect(activeBucket === bucket.name ? "" : bucket.name)}>
            <i style={{ background: PORTFOLIO_CHART_COLORS[index % PORTFOLIO_CHART_COLORS.length] }} />
            <span>{bucket.name}</span>
            <strong>{bucket.value.toFixed(1)}%</strong>
          </button>
        ))}
      </div>
    </div>
  );
}

function PortfolioHeatmap({ holdings, mode, onOpenTicker }: { holdings: Holding[]; mode: string; onOpenTicker: (symbol: string) => void }) {
  const rows = holdings.filter((holding) => holding.hasMarketValue).slice().sort((a, b) => b.weight - a.weight);
  if (!rows.length) {
    return <EmptyState title="No priced exposure" detail="Refresh quotes before using the exposure map." />;
  }
  return (
    <div className="portfolio-heatmap" role="list">
      {rows.map((holding) => {
        const metric = portfolioHeatmapMetric(holding, mode);
        const tone = portfolioHeatmapTone(metric, mode);
        const size = Math.max(72, Math.min(170, 78 + holding.weight * 2.2));
        return (
          <button
            key={holding.ticker}
            className={`portfolio-heatmap-cell ${tone}`}
            style={{ minHeight: size }}
            type="button"
            role="listitem"
            onClick={() => onOpenTicker(holding.ticker)}
          >
            <span>{holding.ticker}</span>
            <strong>{mode === "Weight" ? `${holding.weight.toFixed(1)}%` : formatPct(metric)}</strong>
            <small>{formatMoney(holding.marketValue)} · {portfolioAllocationBucket(holding)}</small>
          </button>
        );
      })}
    </div>
  );
}

export function OpportunitiesPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [actionGrade, setActionGrade] = useState("");
  const [tickerQuery, setTickerQuery] = useState("");
  const [minScore, setMinScore] = useState(0);
  const [assetClass, setAssetClass] = useState("");
  const [minConfidence, setMinConfidence] = useState(0);
  const [source, setSource] = useState("");
  const [freshness, setFreshness] = useState("");
  const [sourceCluster, setSourceCluster] = useState("");
  const [catalystFilter, setCatalystFilter] = useState("");
  const [liquidityFilter, setLiquidityFilter] = useState("");
  const [ownership, setOwnership] = useState("");
  const filtered = model.opportunities.filter((item) => {
    const decisionMatches = !actionGrade || item.actionGrade === actionGrade;
    const tickerMatches = !tickerQuery || item.ticker.includes(tickerQuery.trim().toUpperCase());
    const assetMatches = !assetClass || item.assetClass === assetClass;
    const confidenceMatches = item.confidence >= minConfidence;
    const sourceMatches = !source || item.components.some(([label, value]) => label === source && value > 0);
    const freshnessMatches = !freshness || item.freshnessStatus === freshness;
    const clusterMatches = !sourceCluster || item.sourceCluster === sourceCluster;
    const catalystMatches = !catalystFilter || (catalystFilter === "has" ? item.catalystWindow !== "-" : item.catalystWindow === "-");
    const liquidityMatches = !liquidityFilter || item.liquidity.toLowerCase().includes(liquidityFilter);
    const ownershipMatches = !ownership || (ownership === "owned" ? item.owned : !item.owned);
    return decisionMatches && tickerMatches && assetMatches && confidenceMatches && sourceMatches && freshnessMatches && clusterMatches && catalystMatches && liquidityMatches && ownershipMatches && item.score >= minScore;
  });
  const decisions = Array.from(new Set(model.opportunities.map((item) => item.actionGrade)));
  const assetClasses = Array.from(new Set(model.opportunities.map((item) => item.assetClass).filter(Boolean))).sort();
  const sources = Array.from(new Set(model.opportunities.flatMap((item) => item.components.map(([label]) => label)))).sort();
  const freshnessOptions = Array.from(new Set(model.opportunities.map((item) => item.freshnessStatus).filter(Boolean))).sort();
  const sourceClusters = Array.from(new Set(model.opportunities.map((item) => item.sourceCluster).filter((item) => item && item !== "-"))).sort();
  const leader = filtered[0];
  const showBrokerSurface = brokerProviderSurfaceEnabled(model);
  const sourceItems: Array<[string, DataSourceState]> = [
    ["Decision Queue", model.sources.opportunities],
    ["Quotes", model.sources.watchlist],
    ["Health", model.sources.health],
  ];
  if (showBrokerSurface) {
    sourceItems.splice(1, 0, ["Broker", "live"]);
  }
  return (
    <div className="split-page">
      <FilterRail
        decision={actionGrade}
        decisions={decisions}
        decisionLabel="Action Grade"
        tickerQuery={tickerQuery}
        minScore={minScore}
        assetClass={assetClass}
        assetClasses={assetClasses}
        minConfidence={minConfidence}
        sources={sources}
        source={source}
        freshness={freshness}
        freshnessOptions={freshnessOptions}
        sourceCluster={sourceCluster}
        sourceClusters={sourceClusters}
        catalystFilter={catalystFilter}
        liquidityFilter={liquidityFilter}
        ownership={ownership}
        onDecision={setActionGrade}
        onTickerQuery={setTickerQuery}
        onMinScore={setMinScore}
        onAssetClass={setAssetClass}
        onMinConfidence={setMinConfidence}
        onSource={setSource}
        onFreshness={setFreshness}
        onSourceCluster={setSourceCluster}
        onCatalystFilter={setCatalystFilter}
        onLiquidityFilter={setLiquidityFilter}
        onOwnership={setOwnership}
        onReset={() => {
          setActionGrade("");
          setTickerQuery("");
          setMinScore(0);
          setAssetClass("");
          setMinConfidence(0);
          setSource("");
          setFreshness("");
          setSourceCluster("");
          setCatalystFilter("");
          setLiquidityFilter("");
          setOwnership("");
        }}
      />
      <PageFrame
        title="Research Queue"
        subtitle={`${filtered.length} of ${model.opportunities.length} results`}
        action={
          <GhostButton title="Filters apply immediately to the loaded source rows">
            <Database size={14} /> Source-backed View
          </GhostButton>
        }
      >
        <SourceNotice items={sourceItems} />
        {leader ? (
          <TopOpportunityTicker opportunity={leader} onOpenTicker={onOpenTicker} />
        ) : (
          <EmptyState title="No top ticker for this filter set" detail="The ranked ticker card appears when at least one opportunity matches the active filters." />
        )}
        {showBrokerSurface && <Panel title="Advisory Agent Queue">
          <SummaryList rows={agentRecommendationSummaryRows(model.agentRecommendationRows)} onOpenTicker={onOpenTicker} />
        </Panel>}
        <div className="source-panel-grid">
          {model.signalSources.map((panel) => (
            <Panel key={panel.key} title={panel.title} headerAction={<SourcePill state={panel.state} />}>
              <SummaryList rows={panel.leaders} onOpenTicker={onOpenTicker} />
              <small className="panel-footnote">{panel.count} source rows</small>
            </Panel>
          ))}
        </div>
        <Panel title="Ranked Research Queue">
          <OpportunityTable rows={filtered} compact onOpenTicker={onOpenTicker} />
        </Panel>
      </PageFrame>
    </div>
  );
}

export function FilingsPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [selectedInvestor, setSelectedInvestor] = useState("");
  const [traderQuery, setTraderQuery] = useState("");
  const fallbackCards = model.traderFilingCards;
  const traderMatches = model.traderPortfolios.filter((portfolio) => {
    const query = traderQuery.trim().toLowerCase();
    return !query || portfolio.investor.toLowerCase().includes(query) || portfolio.holdings.some((holding) => (
      holding.ticker.toLowerCase().includes(query)
      || holding.label.toLowerCase().includes(query)
      || holding.security.toLowerCase().includes(query)
      || holding.identifier.toLowerCase().includes(query)
    ));
  });
  const primary = traderMatches.find((portfolio) => portfolio.investor === selectedInvestor)
    ?? model.traderPortfolios.find((portfolio) => portfolio.investor === selectedInvestor)
    ?? traderMatches[0]
    ?? model.traderPortfolios[0];
  return (
    <div className="trader-workbench">
      <aside className="trader-directory">
        <div className="trader-directory-head">
          <strong>Traders</strong>
          <span>{model.traderPortfolios.length}</span>
        </div>
        <input value={traderQuery} onChange={(event) => setTraderQuery(event.target.value)} placeholder="Search trader or ticker" />
        <div className="trader-directory-list">
          {traderMatches.map((portfolio) => (
            <TraderPortfolioRow
              key={portfolio.investor}
              portfolio={portfolio}
              active={portfolio.investor === primary?.investor}
              onSelect={() => setSelectedInvestor(portfolio.investor)}
            />
          ))}
        </div>
      </aside>
      <PageFrame title="Trader Filings" subtitle={primary ? `${model.traderPortfolios.length} trader portfolios tracked; viewing ${primary.investor}` : `${fallbackCards.length} tracked investors`}>
        <SourceNotice items={[["Disclosures", model.sources.filings]]} />
        {primary ? (
          <div className="trader-portfolio-page">
            <TraderPortfolioHero portfolio={primary} />
            <div className="trader-portfolio-grid">
              <Panel className="span-8" title={primary.estimatedInvested > 0 ? "Portfolio Performance" : "Filing Value History"}>
                <TraderPerformanceChart portfolio={primary} />
              </Panel>
              <Panel className="span-4" title="Holdings Distribution">
                <TraderDistribution holdings={primary.holdings} />
              </Panel>
              <Panel className="span-12" title="Current Holdings">
                <TraderHoldingsTable holdings={primary.holdings} onOpenTicker={onOpenTicker} />
              </Panel>
              <Panel className="span-12" title="Recent Allocation History">
                <TraderTransactionsTable transactions={primary.transactions} onOpenTicker={onOpenTicker} />
              </Panel>
              <Panel className="span-12" title="Source Contract">
                <p className="panel-copy">{primary.performanceMethodology || "Performance is calculated from the reconstructed current lot cost basis."}</p>
                <p className="panel-copy">{primary.caveat || "Public tracker snapshot loaded through disclosure ingestion."}</p>
              </Panel>
            </div>
          </div>
        ) : (
          <div className="filings-grid trader-card-grid">
            {fallbackCards.length ? fallbackCards.map((card) => <TraderFilingCardView key={card.investor} card={card} onOpenTicker={onOpenTicker} />) : (
              <Panel className="span-12" title="No matching traders">
              <EmptyState title="No trader filing rows loaded" detail="Run the disclosure refresh or trader backfill job to build trader portfolios." />
              </Panel>
            )}
            <Panel className="span-12" title="About Filings">
              <p className="panel-copy">Disclosure portfolios are reconstructed from public records. They are source-limited models, not live brokerage accounts.</p>
            </Panel>
          </div>
        )}
      </PageFrame>
    </div>
  );
}

export function CalendarPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [activeView, setActiveView] = useState("Calendar");
  const monthLabel = model.calendar[0]?.monthLabel ?? "Source Calendar";
  const upcoming = nextDaysEvents(model.calendar, 7);
  return (
    <PageFrame title="Calendar" subtitle={`${model.calendar.length} dated source events`}>
      <SourceNotice items={[["Calendar", model.sources.calendar]]} />
      <div className="calendar-actions">
        <TabBar tabs={["Timeline", "Calendar", "By Ticker"]} active={activeView} onSelect={setActiveView} />
        <GhostButton>All Events</GhostButton>
        <GhostButton>All Tickers</GhostButton>
      </div>
      <div className="calendar-grid-wrap">
        <Panel className="calendar-panel" title={monthLabel}>
          {activeView === "Timeline" ? <CatalystList events={model.calendar} onOpenTicker={onOpenTicker} /> : activeView === "By Ticker" ? <GenericRows rows={eventsByTickerRows(model.calendar)} emptyTitle="No ticker events" emptyDetail="No ticker-specific calendar rows are loaded." onOpenTicker={onOpenTicker} /> : <CalendarMonth events={model.calendar} onOpenTicker={onOpenTicker} />}
        </Panel>
        <Panel title="Upcoming (Next 7 Days)">
          <CatalystList events={upcoming.slice(0, 8)} onOpenTicker={onOpenTicker} />
          <TextLink>View full calendar</TextLink>
        </Panel>
      </div>
    </PageFrame>
  );
}

export function HealthPage({ model, data }: { model: AppModel; data: PanelData }) {
  const showBrokerSurface = brokerProviderSurfaceEnabled(model);
  const visibleHealthRows = showBrokerSurface ? model.healthRows : model.healthRows.filter((row) => !isBrokerHealthRow(row));
  const visibleSourceHealthRows = showBrokerSurface ? model.sourceHealthRows : model.sourceHealthRows.filter((row) => !isBrokerHealthRow(row));
  const liveProviderRows = visibleSourceHealthRows.filter((row) => row.kind !== "documentation");
  const providerRows = liveProviderRows.length ? liveProviderRows : model.providerRunRows;
  const freshnessRows = (showBrokerSurface ? model.freshnessHealthRows : model.freshnessHealthRows.filter((row) => !isBrokerHealthRow(row))).filter((row) => row.kind !== "documentation");
  const jobRows = model.providerRunRows;
  const operationalRows = visibleHealthRows.filter((row) => row.kind !== "documentation");
  const degradedCount = operationalRows.filter((row) => row.status === "Degraded").length;
  const warningCount = operationalRows.filter((row) => row.status === "Warning").length;
  const ready = (data.dashboard.status?.ready ?? data.settings.status?.ready ?? true) && degradedCount === 0;
  const documentationRows = visibleHealthRows.filter((row) => row.kind === "documentation");
  const sourceItems: Array<[string, DataSourceState]> = [["Source Health", model.sources.health]];
  if (showBrokerSurface) {
    sourceItems.push(["Broker", "live"]);
  }
  const metrics: Array<[string, string, string, Tone | string]> = [
    [ready ? "All Systems Operational" : "System Needs Attention", ready ? "Ready" : "Degraded", `Last check ${model.latestHealthCheck}`, ready ? "good" : "bad"],
    ["Providers", String(providerRows.length), "source-health rows", "info"],
    ["Warnings", String(warningCount), "not documentation", "warn"],
    ["Critical", String(degradedCount), "not documentation", "bad"],
  ];
  if (showBrokerSurface) {
    metrics.splice(2, 0, ["Broker Rows", String(model.brokerStatusRows.length), model.brokerStatusRows[0] ? stringField(model.brokerStatusRows[0], ["detail"]) : "No configured broker status row", model.brokerStatusRows.some((row) => stringField(row, ["status"]) === "ok") ? "good" : "warn"]);
  }
  return (
    <PageFrame title="Health" subtitle="All times in ET">
      <SourceNotice items={sourceItems} />
      <MetricStrip metrics={metrics} />
      <div className="health-grid">
        {showBrokerSurface && <Panel className="span-12" title="Broker / Advisory Safety">
          <SummaryList rows={[...brokerStatusSummaryRows(model.brokerStatusRows), ...agentRecommendationSummaryRows(model.agentRecommendationRows).slice(0, 4)]} />
        </Panel>}
        <Panel className="span-8" title="Provider Health">
          <HealthTable rows={providerRows} />
        </Panel>
        <Panel className="span-4" title="Active Alerts">
          <AlertList rows={operationalRows} />
        </Panel>
        <Panel className="span-6" title="Recent Job Runs">
          <JobRuns rows={jobRows} />
        </Panel>
        <Panel className="span-6" title="Freshness Overview">
          <FreshnessGrid rows={freshnessRows} />
        </Panel>
        <Panel className="span-12" title="Documentation Rows">
          <HealthTable rows={documentationRows} />
        </Panel>
      </div>
    </PageFrame>
  );
}

function TickerTabContent({ activeTab, ticker, data, decisionBrief }: { activeTab: string; ticker: TickerPayload | null; data: PanelData; decisionBrief: RowRecord }) {
  const keyByTab: Record<string, string[]> = {
    "Evidence Stack": ["symbol_decision_snapshot", "decision_snapshot", "decision_queue", "opportunities_ranked", "opportunity_sources", "signals", "technicals", "sepa", "liquidity", "correlations", "valuations"],
    Broker: ["broker_status", "broker_accounts", "broker_positions", "broker_market_snapshots", "broker_scanner_signals", "agent_recommendations", "paper_orders"],
    Fundamentals: ["fundamentals"],
    Estimates: ["analyst_estimates", "earnings", "earnings_setups"],
    Financials: ["fundamentals", "valuations"],
    Options: ["options_expiries", "options_chain", "options_payoff_scenarios"],
    News: ["news"],
    Filings: ["disclosures"],
    Memos: ["research_packets", "memos", "theses"],
  };
  const keys = keyByTab[activeTab] ?? [];
  const sourceRows = [
    ...(activeTab === "Evidence Stack" && ticker?.decision_snapshot ? [ticker.decision_snapshot] : []),
    ...keys.flatMap((key) => ticker?.tables?.[key] ?? []),
  ];
  const displayRows = sourceRows;
  const summaryRows = tabSummaryRows(decisionBrief, activeTab);
  return (
    <Panel title={activeTab}>
      <div className="ticker-tab-workbench">
        {activeTab === "Evidence Stack" && <EvidenceTriad brief={decisionBrief} />}
        {summaryRows.length > 0 && <SummaryList rows={summaryRows} />}
        {activeTab === "Options" && <OptionsEvidence rows={displayRows} />}
        {activeTab !== "Options" && (
          <div className="ticker-source-drilldown">
            <strong>Loaded Source Rows</strong>
            <GenericRows
              rows={displayRows}
              emptyTitle={`No ${activeTab.toLowerCase()} rows`}
              emptyDetail={`No ticker-specific rows are available for ${activeTab}.`}
              onOpenTicker={() => undefined}
            />
          </div>
        )}
      </div>
    </Panel>
  );
}

export function ResearchPage({ data, model, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [selectedSymbol, setSelectedSymbol] = useState(model.opportunities[0]?.ticker ?? symbolFromRow(rows(data.signals)[0]) ?? "");
  const thesisRows = rows(data.theses);
  const newsRows = rows(data.news);
  const fundamentalsRows = rows(data.fundamentals);
  const memoRows = rows(data.memos);
  const signalRows = rows(data.signals);
  const packetRows = rows(data.researchPackets);
  const selected = selectedSymbol || model.opportunities[0]?.ticker || "";
  const selectedOpportunity = model.opportunities.find((item) => item.ticker === selected);
  const selectedReadiness = model.decisionReadinessRows.find((row) => symbolFromRow(row) === selected);
  const selectedPacket = packetRows.find((row) => symbolFromRow(row) === selected);
  const relatedTheses = thesisRows.filter((row) => symbolFromRow(row) === selected);
  const relatedNews = newsRows.filter((row) => symbolFromRow(row) === selected);
  const relatedFundamentals = fundamentalsRows.filter((row) => symbolFromRow(row) === selected);
  const relatedMemos = memoRows.filter((row) => symbolFromRow(row) === selected);
  const relatedSignals = signalRows.filter((row) => symbolFromRow(row) === selected);
  const researchUniverse = model.opportunities.slice(0, 12);
  return (
    <PageFrame title="Research Queue" subtitle="Evidence, theses, memos, and source-backed notes">
      <SourceNotice items={[["Theses", thesisRows.length ? "live" : "empty"], ["News", newsRows.length ? "live" : "empty"], ["Fundamentals", fundamentalsRows.length ? "live" : "empty"]]} />
      <div className="research-workbench">
        <Panel title="Research Universe">
          <div className="research-picker">
            {researchUniverse.length ? researchUniverse.map((item) => (
              <button key={item.ticker} className={item.ticker === selected ? "active" : ""} type="button" onClick={() => setSelectedSymbol(item.ticker)}>
                <strong>{item.ticker}</strong>
                <span>{item.grade}</span>
                <small>{item.decision}</small>
              </button>
            )) : <EmptyState title="No research universe" detail="Signals and candidates have no rows yet." />}
          </div>
        </Panel>
        <Panel title={selected ? `${selected} Deep Analysis` : "Deep Analysis"}>
          {selectedOpportunity || selectedPacket ? (
            <div className="deep-analysis">
              <div className="analysis-score">
                <strong>{selectedOpportunity?.score ?? "-"}</strong>
                <span>{selectedOpportunity ? `${selectedOpportunity.grade} · ${selectedOpportunity.confidence}% confidence` : `${displayValue(selectedPacket?.conviction)} conviction`}</span>
                <DecisionBadge value={selectedOpportunity?.decision ?? displayValue(selectedPacket?.decision)} />
              </div>
              <DetailRows rows={[
                ["Why Now", selectedOpportunity?.whyNow ?? displayValue(selectedPacket?.why_now)],
                ["Next Action", selectedOpportunity?.nextAction ?? displayValue(selectedPacket?.entry_plan)],
                ["Invalidation", selectedOpportunity?.invalidation ?? displayValue(selectedPacket?.invalidation)],
                ["Evidence", selectedPacket ? `${displayValue(selectedPacket.evidence_count)} thesis items · ${displayValue(selectedPacket.price_rows)} price rows` : `${selectedOpportunity?.evidenceCount ?? 0} signal citations`],
                ["Freshness", selectedOpportunity?.freshness ?? displayValue(selectedPacket?.created_at)],
              ]} />
              <BarList rows={selectedOpportunity?.components ?? []} />
              <BulletList tone="warn" items={researchChecklist(selectedReadiness, selectedOpportunity)} />
              {selectedPacket && <ResearchPacketSummary packet={selectedPacket} />}
              <button className="primary-button" type="button" onClick={() => selected && onOpenTicker(selected)}>Open Ticker Dossier</button>
            </div>
          ) : (
            <EmptyState title="No selected analysis" detail="Pick a ticker from the research universe to build a source-backed dossier." />
          )}
        </Panel>
      </div>
      <div className="research-grid">
        <Panel className="span-6" title="Ticker Thesis">
          <GenericRows rows={relatedTheses} emptyTitle="No thesis rows for selected ticker" emptyDetail="Add or refresh ticker-specific thesis evidence before using this dossier." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-6" title="Evidence Feed">
          <GenericRows rows={relatedNews} emptyTitle="No ticker-specific evidence feed rows" emptyDetail="Unrelated market news is hidden from selected ticker research." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Fundamental Watch">
          <GenericRows rows={relatedFundamentals} emptyTitle="No selected ticker fundamental rows" emptyDetail="Run fundamentals ingestion or add valuation evidence for this ticker." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Invalidation Queue">
          <GenericRows rows={relatedSignals.map((row) => ({ ...row, title: row.invalidation ?? row.next_action ?? row.why_now }))} emptyTitle="No selected ticker invalidation rows" emptyDetail="Signals have not produced selected ticker invalidation text." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Memo Queue">
          <GenericRows rows={relatedMemos} emptyTitle="No selected ticker memo rows" emptyDetail="Run research_candidate or weekly_portfolio_review to create selected ticker memos." onOpenTicker={onOpenTicker} />
        </Panel>
      </div>
    </PageFrame>
  );
}

export function ThesisMonitorPage({ data, model, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const monitorRows = rows(data.thesisMonitor);
  const thesisRows = rows(data.theses);
  const memoRows = rows(data.memos);
  const signalRows = rows(data.signals);
  const needsReviewRows = monitorRows.filter((row) => booleanField(row, ["needs_review"]));
  const staleRows = monitorRows.filter((row) => booleanField(row, ["stale_thesis"]));
  const contradictionRows = monitorRows.filter((row) => listField(row, ["contradiction_flags"]).length);
  const stateRows: SummaryItem[] = monitorRows.slice(0, 16).map((row) => thesisSummaryItem(row));
  const reviewRows: SummaryItem[] = needsReviewRows.slice(0, 12).map((row) => ({
    label: stringField(row, ["symbol"]) || "Ticker",
    value: listField(row, ["contradiction_flags"])[0] ? titleLabel(listField(row, ["contradiction_flags"])[0]) : "Stale",
    caption: stringField(row, ["review_reason"]) || stringField(row, ["stale_reason"]) || "Needs review",
    tone: listField(row, ["contradiction_flags"]).some((flag) => flag.includes("breached")) ? "bad" : "warn",
    symbol: stringField(row, ["symbol"]),
  }));
  const invalidationRows = model.opportunities
    .filter((item) => item.invalidation || item.blockingGates.length)
    .slice(0, 8)
    .map((item) => ({
      label: item.ticker,
      value: item.blockingGates[0] ? "blocked" : "watch",
      caption: item.blockingGates[0] ? formatGateLabel(item.blockingGates[0]) : item.invalidation,
      tone: item.blockingGates.length ? "warn" as Tone : "info" as Tone,
      symbol: item.ticker,
    }));
  const sourceRows = monitorRows.length ? monitorRows : thesisRows;
  return (
    <PageFrame title="Thesis Monitor" subtitle={`${monitorRows.length} auditable thesis states, ${needsReviewRows.length} need review`}>
      <SourceNotice items={[["Thesis Monitor", monitorRows.length ? "live" : "empty"], ["Raw Theses", thesisRows.length ? "live" : "empty"], ["Decision Memory", memoRows.length ? "live" : "empty"], ["Signals", signalRows.length ? "live" : "empty"]]} />
      <MetricStrip metrics={[
        ["Needs Review", String(needsReviewRows.length), "stale or contradicted", needsReviewRows.length ? "warn" : "good"],
        ["Stale Thesis", String(staleRows.length), "missing or old review", staleRows.length ? "warn" : "good"],
        ["Contradictions", String(contradictionRows.length), "decision or invalidation conflict", contradictionRows.length ? "bad" : "good"],
        ["Owned/Watched", String(monitorRows.filter((row) => booleanField(row, ["owned"]) || booleanField(row, ["watched"])).length), "auditable symbols", monitorRows.length ? "info" : "muted"],
      ]} />
      <div className="research-grid">
        <Panel className="span-6" title="Thesis State">
          <SummaryList rows={stateRows.length ? stateRows : [{ label: "Thesis state", value: "No rows", caption: "Run Arco/Market thesis refresh before treating this page as complete.", tone: "warn" }]} onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-6" title="Review Queue">
          <SummaryList rows={reviewRows.length ? reviewRows : [{ label: "Review queue", value: "Clear", caption: "No stale thesis or contradiction flags are active.", tone: "good" }]} onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-6" title="Invalidation Watch">
          <SummaryList rows={invalidationRows.length ? invalidationRows : [{ label: "Invalidation", value: "No rows", caption: "No source-backed invalidation rows are loaded.", tone: "muted" }]} onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-6" title="Structured Fields">
          <GenericRows rows={sourceRows} emptyTitle="No structured thesis fields" emptyDetail="Owned and watched symbols will appear here after the thesis monitor read model loads." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-12" title="Decision Memory">
          <GenericRows rows={memoRows} emptyTitle="No decision memory rows" emptyDetail="Run research_candidate or weekly_portfolio_review to persist decision memory." onOpenTicker={onOpenTicker} />
        </Panel>
      </div>
    </PageFrame>
  );
}

function thesisSummaryItem(row: RowRecord): SummaryItem {
  const flags = listField(row, ["contradiction_flags"]);
  const stale = booleanField(row, ["stale_thesis"]);
  const needsReview = booleanField(row, ["needs_review"]);
  const age = numberField(row, ["last_reviewed_age_days"], Number.NaN);
  const status = titleLabel(stringField(row, ["status"]) || "monitor");
  return {
    label: stringField(row, ["symbol"]) || "Ticker",
    value: needsReview ? "Review" : status,
    caption: needsReview
      ? stringField(row, ["review_reason"]) || flags.map(titleLabel).join(", ") || stringField(row, ["stale_reason"]) || "Needs review"
      : `${status}${Number.isFinite(age) ? ` · reviewed ${Math.round(age)}d ago` : ""}`,
    tone: flags.some((flag) => flag.includes("breached")) ? "bad" : needsReview || stale ? "warn" : "good",
    symbol: stringField(row, ["symbol"]),
  };
}

export function SettingsPage({ data }: { data: PanelData }) {
  const config = data.settings.config ?? {};
  const integration = data.settings.integration ?? {};
  const hasSettings = Object.keys(config).length > 0 || Object.keys(integration).length > 0;
  return (
    <PageFrame title="Settings" subtitle={hasSettings ? "Local app configuration and source wiring" : "Waiting for /api/settings metadata"}>
      <SourceNotice items={[["Settings API", hasSettings ? "live" : "empty"]]} />
      <div className="settings-grid">
        <Panel title="Configuration">
          {Object.keys(config).length ? <DetailRows rows={Object.entries(config).slice(0, 8).map(([key, value]) => [key, displayValue(value)])} /> : <EmptyState title="No configuration metadata" detail="The frontend will populate this panel after /api/settings returns config fields." />}
        </Panel>
        <Panel title="Integration">
          {Object.keys(integration).length ? <DetailRows rows={Object.entries(integration).map(([key, value]) => [key, displayValue(value)])} /> : <EmptyState title="No integration metadata" detail="DuckDB, Arco, and Birdclaw wiring appear here when settings metadata is loaded." />}
        </Panel>
        <Panel title="Source Rules">
          <BulletList tone="info" items={["No secrets are displayed in this UI.", "Arco evidence is consumed from the durable brain raw source path.", "Investment logic remains in the Python backend.", "Frontend pages only format and group source-backed rows."]} />
        </Panel>
      </div>
    </PageFrame>
  );
}

export type AppModel = {
  watchlist: WatchItem[];
  opportunities: Opportunity[];
  decisionQueues: Record<DecisionBucket, Opportunity[]>;
  holdings: Holding[];
  filings: Filing[];
  traderPortfolios: TraderPortfolio[];
  traderFilingCards: TraderFilingCard[];
  calendar: CalendarEvent[];
  healthRows: HealthRow[];
  freshnessHealthRows: HealthRow[];
  sourceHealthRows: HealthRow[];
  providerRunRows: HealthRow[];
  brokerStatusRows: RowRecord[];
  brokerAccountRows: RowRecord[];
  brokerPositionRows: RowRecord[];
  brokerSignalRows: RowRecord[];
  agentRecommendationRows: RowRecord[];
  paperOrderRows: RowRecord[];
  dailyBriefRows: DailyBriefItem[];
  exposureClusterRows: RowRecord[];
  correlationEdgeRows: RowRecord[];
  portfolioRiskCardRows: RowRecord[];
  reviewActionRows: RowRecord[];
  thesisMonitorRows: RowRecord[];
  portfolioValue: number;
  sectors: SummaryItem[];
  setupRows: SummaryItem[];
  liquidityRows: SummaryItem[];
  correlationRows: SummaryItem[];
  valuationRows: SummaryItem[];
  technicalRows: SummaryItem[];
  signalSources: SignalSourcePanel[];
  signalCoverage: SourceCoverage[];
  signalMatrix: SignalMatrixRow[];
  financeAnalyses: FinanceAnalysis[];
  memoRows: RowRecord[];
  decisionReadinessRows: RowRecord[];
  discoveredUniverseCount: number;
  latestHealthCheck: string;
  sources: {
    watchlist: DataSourceState;
    opportunities: DataSourceState;
    holdings: DataSourceState;
    filings: DataSourceState;
    calendar: DataSourceState;
    health: DataSourceState;
  };
};

export function buildModel(data: PanelData): AppModel {
  const watchlist = buildWatchlist(rows(data.quotes));
  const holdings = buildHoldings(rows(data.portfolio), rows(data.quotes), rows(data.decisionReadiness));
  const opportunities = buildOpportunities(
    rows(data.decisionQueue),
    rows(data.opportunitiesRanked),
    rows(data.signals),
    rows(data.candidates),
    rows(data.quotes),
    rows(data.catalysts),
    rows(data.earnings),
    rows(data.liquidity),
    rows(data.portfolio),
    rows(data.opportunitySources),
    rows(data.discoveredUniverse),
  );
  const filings = buildFilings(rows(data.disclosures));
  const traderPortfolios = buildTraderPortfolios(rows(data.disclosures));
  const calendar = buildCalendar(rows(data.catalysts), rows(data.earnings));
  const freshnessHealthRows = buildFreshnessHealthRows(rows(data.sourceFreshness));
  const sourceHealthRows = buildSourceHealthRows(rows(data.sourceHealth));
  const providerRunRows = buildProviderRunRows(rows(data.providerRuns));
  const brokerStatusRows = rows(data.brokerStatus);
  const brokerAccountRows = rows(data.brokerAccounts);
  const brokerPositionRows = rows(data.brokerPositions);
  const brokerSignalRows = rows(data.brokerScannerSignals);
  const agentRecommendationRows = rows(data.agentRecommendations);
  const paperOrderRows = rows(data.paperOrders);
  const dailyBriefRows = buildDailyBriefRows(rows(data.dailyBrief));
  const exposureClusterRows = rows(data.exposureClusters);
  const correlationEdgeRows = rows(data.correlationEdges);
  const portfolioRiskCardRows = rows(data.portfolioRiskCards);
  const reviewActionRows = rows(data.reviewActions);
  const thesisMonitorRows = rows(data.thesisMonitor);
  const brokerHealthRows = buildBrokerHealthRows(brokerStatusRows);
  const healthRows = [...freshnessHealthRows, ...sourceHealthRows, ...providerRunRows, ...brokerHealthRows];
  const ownedSymbols = new Set(holdings.map((holding) => holding.ticker));
  const portfolioValue = holdings.reduce((total, holding) => total + (holding.hasMarketValue ? holding.marketValue : 0), 0);
  const latestHealthCheck = newestDateLabel(healthRows.map((row) => row.freshness));
  const signalDefinitions = buildSignalDefinitions(data);
  const signalMatrix = buildSignalMatrix(opportunities, signalDefinitions);
  return {
    watchlist,
    opportunities,
    decisionQueues: bucketOpportunities(opportunities),
    holdings,
    filings,
    traderPortfolios,
    traderFilingCards: buildTraderFilingCards(filings),
    calendar,
    healthRows,
    freshnessHealthRows,
    sourceHealthRows,
    providerRunRows,
    brokerStatusRows,
    brokerAccountRows,
    brokerPositionRows,
    brokerSignalRows,
    agentRecommendationRows,
    paperOrderRows,
    dailyBriefRows,
    exposureClusterRows,
    correlationEdgeRows,
    portfolioRiskCardRows,
    reviewActionRows,
    thesisMonitorRows,
    portfolioValue,
    sectors: buildSectorRows(rows(data.screener)),
    setupRows: buildSetupRows(rows(data.sepa), rows(data.liquidity)),
    liquidityRows: buildLiquidityRows(rows(data.liquidity), ownedSymbols),
    correlationRows: buildCorrelationRows(rows(data.correlations), ownedSymbols),
    valuationRows: buildValuationRows(rows(data.valuations)),
    technicalRows: buildTechnicalRows(rows(data.technicals)),
    signalSources: buildSignalSourcePanels(data),
    signalCoverage: buildSignalCoverage(signalDefinitions),
    signalMatrix,
    financeAnalyses: buildFinanceAnalyses(opportunities, data, signalMatrix),
    memoRows: rows(data.memos),
    decisionReadinessRows: rows(data.decisionReadiness),
    discoveredUniverseCount: rows(data.discoveredUniverse).length,
    latestHealthCheck,
    sources: {
      watchlist: watchlist.length ? "live" : "empty",
      opportunities: opportunities.length ? "live" : "empty",
      holdings: holdings.length ? "live" : "empty",
      filings: filings.length ? "live" : "empty",
      calendar: calendar.length ? "live" : "empty",
      health: healthRows.length ? "live" : "empty",
    },
  };
}

function panelDataWithTickerTables(base: PanelData, ticker: TickerPayload): PanelData {
  const next: PanelData = { ...base };
  const tableMap: Record<string, keyof PanelData> = {
    candidates: "candidates",
    decision_queue: "decisionQueue",
    decision_readiness: "decisionReadiness",
    discovered_universe: "discoveredUniverse",
    symbol_decision_snapshots: "symbolDecisionSnapshots",
    symbol_decision_snapshot: "symbolDecisionSnapshots",
    opportunities_ranked: "opportunitiesRanked",
    opportunity_sources: "opportunitySources",
    portfolio: "portfolio",
    theses: "theses",
    thesis_monitor: "thesisMonitor",
    catalysts: "catalysts",
    signals: "signals",
    fundamentals: "fundamentals",
    disclosures: "disclosures",
    quotes: "quotes",
    options_expiries: "optionsExpiries",
    options_chain: "optionsChain",
    options_payoff_scenarios: "optionsPayoffScenarios",
    news: "news",
    tradingview_symbol_search: "tradingviewSymbolSearch",
    tradingview_watchlists: "tradingviewWatchlists",
    tradingview_alerts: "tradingviewAlerts",
    tradingview_chart_state: "tradingviewChartState",
    sepa: "sepa",
    liquidity: "liquidity",
    correlations: "correlations",
    etf_premiums: "etfPremiums",
    analyst_estimates: "analystEstimates",
    earnings: "earnings",
    earnings_setups: "earningsSetups",
    valuations: "valuations",
    technicals: "technicals",
    research_packets: "researchPackets",
    broker_status: "brokerStatus",
    broker_accounts: "brokerAccounts",
    broker_positions: "brokerPositions",
    broker_market_snapshots: "brokerMarketSnapshots",
    broker_scanner_signals: "brokerScannerSignals",
    agent_recommendations: "agentRecommendations",
    paper_orders: "paperOrders",
    memos: "memos",
  };

  for (const [apiKey, tableRows] of Object.entries(ticker.tables ?? {})) {
    const dataKey = tableMap[apiKey];
    if (!dataKey || dataKey === "dashboard" || dataKey === "settings" || dataKey === "errors") continue;
    const payload: TablePayload = { rows: tableRows ?? [], count: tableRows?.length ?? 0 };
    (next[dataKey] as TablePayload) = payload;
  }

  if (ticker.decision_snapshot && !rows(next.symbolDecisionSnapshots).length) {
    next.symbolDecisionSnapshots = { rows: [ticker.decision_snapshot], count: 1 };
  }

  return next;
}

function buildDailyBriefRows(sourceRows: RowRecord[]): DailyBriefItem[] {
  return sourceRows.map((row, index) => {
    const category = stringField(row, ["category"]) || "top_opportunities";
    const symbol = stringField(row, ["symbol"]);
    return {
      itemId: stringField(row, ["item_id"]) || `${category}-${index}`,
      category,
      rank: numberField(row, ["rank"], index + 1),
      title: stringField(row, ["title"]) || "Daily brief item",
      symbol,
      symbols: listField(row, ["symbols"]).filter(Boolean),
      reason: stringField(row, ["reason"]) || "Backend read model selected this item.",
      evidence: listField(row, ["evidence"]),
      blocker: stringField(row, ["blocker"]) || "None",
      nextAction: stringField(row, ["next_action"]) || "Review the source rows before acting.",
      score: numberField(row, ["score"], 0),
      severity: stringField(row, ["severity"]) || "info",
      sourceModels: listField(row, ["source_models"]),
      asOf: stringField(row, ["as_of"]),
    };
  });
}

function buildWatchlist(quoteRows: RowRecord[]): WatchItem[] {
  return quoteRows.slice(0, 8).map((row) => ({
    symbol: stringField(row, ["symbol", "ticker"]).toUpperCase(),
    price: formatRawPrice(row.price ?? row.close ?? row.last),
    change: numberField(row, ["change_pct", "percent_change", "change"], 0),
  })).filter((item) => item.symbol);
}

function buildOpportunities(
  decisionRows: RowRecord[],
  rankedRows: RowRecord[],
  signalRows: RowRecord[],
  candidateRows: RowRecord[],
  quoteRows: RowRecord[],
  catalystRows: RowRecord[],
  earningsRows: RowRecord[],
  liquidityRows: RowRecord[],
  portfolioRows: RowRecord[],
  opportunitySourceRows: RowRecord[],
  universeRows: RowRecord[],
): Opportunity[] {
  const sourceRows = decisionRows.length ? decisionRows : rankedRows.length ? rankedRows : signalRows.length ? signalRows : candidateRows;
  const quotesBySymbol = mapRowsBySymbol(quoteRows);
  const catalystsBySymbol = mapRowsBySymbol([...catalystRows, ...earningsRows]);
  const liquidityBySymbol = mapRowsBySymbol(liquidityRows);
  const portfolioBySymbol = mapRowsBySymbol(portfolioRows);
  const universeBySymbol = mapRowsBySymbol(universeRows);
  const sourceRowsBySymbol = groupRowsBySymbol(opportunitySourceRows);

  return sourceRows.slice(0, 250).map((row, index) => {
    const ticker = stringField(row, ["symbol", "ticker", "security", "name"]).toUpperCase() || `ITEM-${index + 1}`;
    const quote = quotesBySymbol.get(ticker);
    const catalyst = catalystsBySymbol.get(ticker);
    const liquidity = liquidityBySymbol.get(ticker);
    const portfolio = portfolioBySymbol.get(ticker);
    const universe = universeBySymbol.get(ticker);
    const sourceRowsForSymbol = sourceRowsBySymbol.get(ticker) ?? [];
    const freshnessStatus = normalizeFreshnessStatus(stringField(row, ["freshness_status", "freshness", "source_freshness", "status"]));
    const rawDecision = stringField(row, ["action_grade", "decision", "action", "recommendation", "status"]) || "Watch";
    const actionGrade = actionGradeFromValue(rawDecision, freshnessStatus);
    const evidenceCount = Math.round(numberField(row, ["evidence_count", "source_count"], sourceRowsForSymbol.length));
    const sourceCount = Math.round(numberField(row, ["source_count", "independent_source_count"], sourceRowsForSymbol.length || evidenceCount));
    const inclusionReasons = uniqueText([
      ...listField(row, ["inclusion_reasons", "why_this_is_here", "reasons"]),
      ...listField(universe ?? {}, ["inclusion_reasons", "reasons"]),
      ...sourceRowsForSymbol.map((item) => stringField(item, ["inclusion_reason", "reason", "title", "source_key", "source_cluster"])).filter(Boolean),
      stringField(row, ["why_now", "rationale", "summary", "notes", "thesis"]),
    ]).slice(0, 5);
    const blockingGates = uniqueText([
      ...listField(row, ["blocking_gates", "failed_gates", "gates"]),
      stringField(row, ["blocking_gate", "gate_warning"]),
    ]).slice(0, 5);
    const isStale = freshnessStatus === "stale" || freshnessStatus === "degraded" || blockingGates.some((gate) => gate.toLowerCase().includes("stale"));
    const isSourceThin = sourceCount < 2 && evidenceCount < 2;
    const score = Math.round(numberField(row, ["score", "final_score", "decision_score"], 60));
    const decisionBasisObject = objectField(row.decision_basis);
    const decisionSummary = stringField(row, ["decision_basis", "basis", "rationale", "why_now"])
      || stringField(decisionBasisObject, ["summary"])
      || `${ticker} score ${score}; ${sourceCount} source rows and ${evidenceCount} primary evidence items loaded.`;
    const nextAction = stringField(row, ["next_action", "action_required", "next_step"])
      || (actionGrade === "Reject" ? "No new exposure until score, setup, or source evidence improves." : "Review ticker-specific setup, sizing, and invalidation.");
    return {
      rank: Math.round(numberField(row, ["rank"], index + 1)),
      ticker,
      name: stringField(row, ["name"]) || stringField(universe ?? {}, ["name"]) || ticker,
      assetClass: stringField(row, ["asset_class", "asset_type"]) || stringField(universe ?? {}, ["asset_class", "asset_type"]) || "unclassified",
      category: stringField(row, ["category", "eligibility_status"]) || stringField(universe ?? {}, ["eligibility_status", "category"]) || "uncategorized",
      score,
      grade: stringField(row, ["signal_grade", "grade"]) || gradeFromScore(numberField(row, ["score", "final_score", "decision_score"], 60)),
      actionGrade: isStale ? "Stale" : actionGrade,
      confidence: confidenceValue(row),
      decision: normalizeDecision(rawDecision),
      freshnessStatus,
      sourceCluster: stringField(row, ["source_cluster", "primary_source_cluster"]) || stringField(universe ?? {}, ["source_cluster", "primary_source_cluster"]) || sourceClusterFromRows(sourceRowsForSymbol),
      inclusionReasons: inclusionReasons.length ? inclusionReasons : ["Loaded from current candidate/signal row."],
      blockingGates,
      decisionBasis: decisionSummary,
      asOf: stringField(row, ["as_of", "updated_at", "run_date", "created_at"]) || stringField(universe ?? {}, ["latest_source_timestamp", "as_of"]) || "not timestamped",
      latestQuote: stringField(row, ["latest_quote", "quote"]) || quoteLabel(quote),
      catalystWindow: stringField(row, ["catalyst_window", "event_window"]) || catalystLabel(catalyst),
      liquidity: stringField(row, ["liquidity", "liquidity_grade"]) || liquidityLabel(liquidity),
      portfolioImpact: stringField(row, ["portfolio_impact", "portfolio_fit"]) || portfolioImpactLabel(portfolio),
      owned: Boolean(portfolio),
      sourceCount,
      isSourceThin,
      isStale,
      whyNow: stringField(row, ["why_now", "rationale", "summary", "notes", "thesis"]) || decisionSummary,
      nextAction,
      invalidation: stringField(row, ["invalidation", "invalidates_if", "risk", "bear_case"]) || "No ticker-specific invalidation row is loaded.",
      freshness: stringField(row, ["freshness", "freshness_status", "source_freshness", "updated_at", "as_of", "run_date"]) || "not_loaded",
      tags: decisionTags(freshnessStatus, isSourceThin, sourceRowsForSymbol),
      components: componentRows(row),
      evidenceCount,
    };
  });
}

function buildHoldings(portfolioRows: RowRecord[], quoteRows: RowRecord[], readinessRows: RowRecord[]): Holding[] {
  const quotesBySymbol = mapRowsBySymbol(quoteRows);
  const readinessBySymbol = mapRowsBySymbol(readinessRows);
  return portfolioRows.slice(0, 20).map((row) => {
    const ticker = stringField(row, ["ticker", "symbol", "name"]).toUpperCase() || "UNKNOWN";
    const quote = quotesBySymbol.get(ticker) ?? {};
    const readiness = readinessBySymbol.get(ticker) ?? {};
    const marketValue = optionalNumberField(row, ["market_value", "value", "position"]);
    const unrealizedPnl = optionalNumberField(row, ["pnl", "unrealized_pnl", "gain_loss"]);
    const unrealizedPnlPct = optionalNumberField(row, ["unrealized_pnl_pct"]);
    const quoteFreshness = stringField(row, ["quote_freshness"]) || "missing";
    const quantity = numberField(row, ["quantity"], 0);
    const changeAbs = optionalNumberField(row, ["change_abs"]) ?? optionalNumberField(quote, ["change_abs"]);
    const changePct = optionalNumberField(row, ["change_pct"]) ?? optionalNumberField(quote, ["change_pct"]);
    const rawSignal = stringField(row, ["signal", "thesis_status", "decision"]);
    const decisionScore = numberField(readiness, ["decision_score", "action_score"], Number.NaN);
    const addStance = holdingAddStance(rawSignal, quoteFreshness, decisionScore);
    return {
      ticker,
      quantity,
      assetClass: titleLabel(stringField(row, ["asset_class"]) || "unclassified"),
      category: titleLabel(stringField(row, ["category"]) || "owned-portfolio"),
      weight: numberField(row, ["weight", "portfolio_weight"], 0),
      price: numberField(row, ["price"], Number.NaN),
      marketValue: marketValue ?? 0,
      hasMarketValue: marketValue !== null,
      averageCost: numberField(row, ["cost_basis", "average_cost", "avg_cost"], 0),
      purchaseDate: stringField(row, ["purchase_date"]) || "",
      holdingDays: numberField(row, ["holding_days"], 0),
      taxLotTerm: normalizeTaxLotTerm(stringField(row, ["tax_lot_term"])),
      unrealizedPnl: unrealizedPnl ?? 0,
      unrealizedPnlPct: unrealizedPnlPct ?? 0,
      hasPnl: unrealizedPnl !== null && unrealizedPnlPct !== null,
      quoteFreshness,
      dayChangePct: changePct ?? Number.NaN,
      dayChangeValue: changeAbs !== null ? changeAbs * quantity : Number.NaN,
      addStance,
      nextStep: holdingNextStep(addStance, quoteFreshness, numberField(row, ["portfolio_weight", "weight"], 0), arrayField(readiness.blockers).map((item) => displayValue(item as JsonValue))),
      decisionScore,
      blockers: arrayField(readiness.blockers).map((item) => displayValue(item as JsonValue)).filter((item) => item && item !== "-"),
    };
  }).filter((row) => row.ticker !== "UNKNOWN");
}

function buildFilings(disclosureRows: RowRecord[]): Filing[] {
  const filings: Filing[] = [];
  for (const row of disclosureRows) {
    const sourceType = stringField(row, ["source_type"]) || stringField(objectField(row.raw), ["source_type"]);
    if (sourceType === "trader_portfolio_model") continue;
    const investor = stringField(row, ["trader_name", "filer_name", "investor"]) || "Tracked Investor";
    const filed = stringField(row, ["filed_date", "filing_date"]) || "-";
    const event = stringField(row, ["event_date", "period_end"]) || "-";
    const raw = objectField(row.raw);
    const holdings = (arrayField(row.holding_sample).length ? arrayField(row.holding_sample) : arrayField(raw?.holdings)).slice(0, 8);
    if (holdings.length) {
      for (const holding of holdings) {
        const holdingRow = objectField(holding);
        filings.push({
          investor,
          ticker: stringField(holdingRow, ["ticker", "symbol"]).toUpperCase(),
          security: stringField(holdingRow, ["name", "title", "cusip"]) || stringField(row, ["filer_name"]) || "13F holding",
          action: normalizeDecision(stringField(row, ["action", "change_type"]) || "Filed"),
          shares: numberField(holdingRow, ["shares_or_principal_amount", "shares"], 0),
          value: numberField(holdingRow, ["value_thousands", "value"], 0),
          filed,
          event,
        });
      }
      continue;
    }
    filings.push({
      investor,
      ticker: stringField(row, ["ticker", "symbol"]).toUpperCase(),
      security: stringField(row, ["security", "filer_name"]) || "13F filing",
      action: normalizeDecision(stringField(row, ["action", "change_type"]) || "Updated"),
      shares: numberField(row, ["shares", "holdings_count"], 0),
      value: numberField(row, ["value", "holdings_value_thousands"], 0),
      filed,
      event,
    });
  }
  return filings.slice(0, 25);
}

function buildTraderPortfolios(disclosureRows: RowRecord[]): TraderPortfolio[] {
  const latest13fRows = new Map<string, RowRecord>();
  const grouped13fRows = new Map<string, RowRecord[]>();
  for (const row of disclosureRows) {
    const raw = objectField(row.raw);
    const sourceType = stringField(row, ["source_type"]) || stringField(raw, ["source_type"]);
    if (sourceType !== "13f") continue;
    const investor = stringField(row, ["trader_name"]) || stringField(raw, ["tracker_name", "filer_name"]) || "13F Tracker";
    grouped13fRows.set(investor, [...(grouped13fRows.get(investor) ?? []), row]);
    const current = latest13fRows.get(investor);
    const currentDate = current ? stringField(current, ["event_date", "filed_date"]) : "";
    const rowDate = stringField(row, ["event_date", "filed_date"]);
    if (!current || rowDate > currentDate) latest13fRows.set(investor, row);
  }
  const rowsToBuild = [
    ...disclosureRows.filter((row) => {
      const raw = objectField(row.raw);
      return (stringField(row, ["source_type"]) || stringField(raw, ["source_type"])) !== "13f";
    }),
    ...latest13fRows.values(),
  ];

  return rowsToBuild
    .map((row) => {
      const raw = objectField(row.raw);
      const sourceType = stringField(row, ["source_type"]) || stringField(raw, ["source_type"]);
      if (sourceType === "13f") {
        const investor = stringField(row, ["trader_name"]) || stringField(raw, ["tracker_name", "filer_name"]) || "13F Tracker";
        return buildThirteenFPortfolio(row, raw, grouped13fRows.get(investor) ?? [row]);
      }
      if (sourceType !== "trader_portfolio_model") return null;
      const metadata = objectField(row.metadata ?? raw.metadata);
      const holdings = arrayField(row.holding_sample).length ? arrayField(row.holding_sample) : arrayField(raw.holdings);
      const transactions = arrayField(row.transactions).length ? arrayField(row.transactions) : arrayField(raw.transactions);
      const history = arrayField(row.portfolio_history).length ? arrayField(row.portfolio_history) : arrayField(raw.portfolio_history);
      const topSectors = arrayField(metadata.topSectors ?? metadata.top_sectors)
        .map((sector) => displayValue(sector as JsonValue))
        .filter((sector) => sector && sector !== "-");
      return {
        investor: stringField(row, ["trader_name"]) || stringField(raw, ["name"]) || "Tracked Portfolio",
        description: stringField(raw, ["description"]) || "Public tracker portfolio snapshot",
        category: titleLabel(stringField(raw, ["category"]) || "portfolio"),
        updated: stringField(raw, ["last_updated"]) || stringField(row, ["filed_date"]) || "recently",
        totalValue: numberField(row, ["total_value"], numberField(raw, ["total_value", "totalValue"], 0)),
        estimatedInvested: numberField(row, ["estimated_invested_usd"], numberField(raw, ["estimated_invested_usd"], 0)),
        performance: numberField(row, ["performance_percent"], numberField(raw, ["performance_percent"], 0)),
        holdingsCount: Math.round(numberField(row, ["holdings_count"], numberField(raw, ["total_holdings"], holdings.length))),
        riskLevel: titleLabel(stringField(metadata, ["riskLevel", "risk_level"]) || "unknown"),
        diversificationScore: Math.round(numberField(metadata, ["diversificationScore", "diversification_score"], 0)),
        topSectors,
        holdings: holdings.map((item) => {
          const holding = objectField(item);
          return {
            ticker: stringField(holding, ["symbol", "ticker"]).toUpperCase(),
            label: stringField(holding, ["symbol", "ticker"]).toUpperCase(),
            security: stringField(holding, ["symbol", "ticker"]).toUpperCase(),
            identifier: stringField(holding, ["symbol", "ticker"]).toUpperCase(),
            isTickerMapped: true,
            quantity: numberField(holding, ["quantity"], 0),
            price: numberField(holding, ["latest_price", "latestPrice", "price"], 0),
            marketValue: numberField(holding, ["market_value", "marketValue", "value"], 0),
            costBasis: numberField(holding, ["cost_basis", "costBasis"], 0),
            unrealizedPnl: numberField(holding, ["unrealized_pnl", "unrealizedPnl"], 0),
            weight: numberField(holding, ["weight"], 0),
          };
        }).filter((holding) => holding.ticker),
        transactions: transactions.map((item) => {
          const transaction = objectField(item);
          return {
            symbol: stringField(transaction, ["symbol"]).toUpperCase(),
            type: stringField(transaction, ["type"]).toUpperCase(),
            quantity: numberField(transaction, ["quantity"], 0),
            price: numberField(transaction, ["price"], 0),
            estimatedAmount: numberField(transaction, ["estimated_amount", "estimatedAmount"], 0),
            date: stringField(transaction, ["date"]) || stringField(transaction, ["created_at"]),
            filedDate: stringField(transaction, ["filed_date", "filedDate"]),
            weightBefore: numberField(transaction, ["weight_before", "weightBefore"], 0),
            weightAfter: numberField(transaction, ["weight_after", "weightAfter"], 0),
          };
        }).filter((transaction) => transaction.symbol),
        history: history.map((item) => {
          const point = objectField(item);
          return {
            date: stringField(point, ["date"]),
            value: numberField(point, ["value", "total_value"], 0),
            costBasis: numberField(point, ["cost_basis", "costBasis"], 0),
            performance: numberField(point, ["performance_percent", "performance"], 0),
          };
        }).filter((point) => point.date),
        sourceUrl: stringField(row, ["source_url"]),
        caveat: stringField(row, ["source_caveat"]) || stringField(raw, ["source_caveat"]),
        performanceMethodology: stringField(raw, ["performance_methodology"]),
        nextFilingDueDate: "",
      };
    })
    .filter((portfolio): portfolio is TraderPortfolio => Boolean(portfolio))
    .sort((a, b) => {
      const actionableA = a.category.toLowerCase().includes("13f") ? 0 : 1;
      const actionableB = b.category.toLowerCase().includes("13f") ? 0 : 1;
      if (actionableA !== actionableB) return actionableB - actionableA;
      return new Date(b.updated).getTime() - new Date(a.updated).getTime();
    });
}

function buildThirteenFPortfolio(row: RowRecord, raw: RowRecord, groupRows: RowRecord[] = [row]): TraderPortfolio | null {
  const holdings = arrayField(row.holding_sample).length ? arrayField(row.holding_sample) : arrayField(raw.holdings);
  const allocationHistory = arrayField(row.allocation_history).length
    ? arrayField(row.allocation_history)
    : arrayField(raw.allocation_history).length
      ? arrayField(raw.allocation_history)
      : buildThirteenFAllocationHistory(groupRows);
  if (!holdings.length) return null;
  const totalValue = numberField(row, ["amount", "holdings_value_thousands"], numberField(raw, ["holdings_value_thousands"], 0));
  const mappedHoldingsRaw = holdings.map((item) => {
    const holding = objectField(item);
    const marketValue = numberField(holding, ["market_value", "value_thousands", "value"], 0);
    const ticker = stringField(holding, ["ticker", "symbol"]).toUpperCase();
    const security = stringField(holding, ["name", "title"]) || "13F holding";
    const putCall = stringField(holding, ["put_call", "putCall"]).toUpperCase();
    const label = ticker ? `${ticker}${putCall ? ` ${putCall}` : ""}` : security;
    return {
      ticker,
      label,
      security,
      identifier: "",
      isTickerMapped: Boolean(ticker),
      quantity: numberField(holding, ["shares_or_principal_amount", "shares"], 0),
      price: 0,
      marketValue,
      costBasis: 0,
      unrealizedPnl: 0,
      weight: numberField(holding, ["weight"], totalValue ? (marketValue / totalValue) * 100 : 0),
    };
  }).filter((holding) => holding.label).sort((a, b) => b.weight - a.weight);
  const mappedHoldings = aggregateThirteenFHoldings(mappedHoldingsRaw);
  const filingHistory = buildThirteenFFilingHistory(groupRows, row, raw, totalValue);
  return {
    investor: stringField(row, ["trader_name"]) || stringField(raw, ["tracker_name", "filer_name"]) || "13F Tracker",
    description: "Latest Form 13F holdings disclosure. Positions are reported as of quarter end and may omit shorts, many derivatives, and current trade intent.",
    category: "13F Filing",
    updated: stringField(row, ["filed_date"]) || stringField(raw, ["filed_date"]) || "recently",
    totalValue,
    estimatedInvested: 0,
    performance: 0,
    holdingsCount: Math.round(numberField(row, ["holdings_count"], numberField(raw, ["holdings_count"], mappedHoldings.length))),
    riskLevel: "Disclosure-Lagged",
    diversificationScore: diversificationScoreFromWeights(mappedHoldings.map((holding) => holding.weight)),
    topSectors: [],
    holdings: mappedHoldings,
    transactions: allocationHistory.map((item) => {
      const change = objectField(item);
      const symbol = stringField(change, ["symbol"]).toUpperCase();
      const security = stringField(change, ["security"]);
      const putCall = stringField(change, ["put_call", "putCall"]).toUpperCase();
      const type = stringField(change, ["type"]).toUpperCase();
      return {
        symbol: symbol ? `${symbol}${putCall ? ` ${putCall}` : ""}` : security,
        type,
        quantity: numberField(change, ["quantity"], 0),
        price: numberField(change, ["price"], 0),
        estimatedAmount: numberField(change, ["estimated_amount", "estimatedAmount"], 0),
        date: stringField(change, ["date"]) || stringField(row, ["event_date"]),
        filedDate: stringField(change, ["filed_date", "filedDate"]) || stringField(row, ["filed_date"]),
        weightBefore: numberField(change, ["weight_before", "weightBefore"], 0),
        weightAfter: numberField(change, ["weight_after", "weightAfter"], 0),
      };
    }).filter((transaction) => transaction.symbol),
    history: filingHistory,
    sourceUrl: stringField(row, ["source_url"]) || stringField(raw, ["holdings_source_url"]),
    caveat: stringField(row, ["lag_caveat"]) || stringField(raw, ["lag_caveat"]),
    performanceMethodology: "13F rows are reported holdings snapshots, not reconstructed transaction ledgers; performance is not calculated for this source type.",
    nextFilingDueDate: stringField(row, ["next_filing_due_date"]) || stringField(raw, ["next_filing_due_date"]),
  };
}

function aggregateThirteenFHoldings<T extends TraderPortfolioHolding>(holdings: T[]): T[] {
  const byKey = new Map<string, T>();
  for (const holding of holdings) {
    const key = holding.ticker ? `${holding.ticker}:${holding.label.replace(holding.ticker, "")}` : `unmapped:${holding.security}`;
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, { ...holding, identifier: holding.ticker ? holding.identifier : "unresolved/multi-class" });
      continue;
    }
    existing.quantity += holding.quantity;
    existing.marketValue += holding.marketValue;
    existing.weight += holding.weight;
    if (!holding.ticker) existing.identifier = "unresolved/multi-class";
  }
  return [...byKey.values()].sort((a, b) => b.weight - a.weight);
}

function buildThirteenFFilingHistory(groupRows: RowRecord[], latestRow: RowRecord, latestRaw: RowRecord, latestValue: number): TraderPortfolioHistoryPoint[] {
  const points = groupRows
    .map((item) => {
      const raw = objectField(item.raw);
      const value = numberField(item, ["amount", "holdings_value_thousands"], numberField(raw, ["holdings_value_thousands"], 0));
      return {
        date: stringField(item, ["event_date"]) || stringField(raw, ["report_date"]) || stringField(item, ["filed_date"]),
        value,
        costBasis: 0,
        performance: 0,
        holdingsCount: Math.round(numberField(item, ["holdings_count"], numberField(raw, ["holdings_count"], 0))),
      };
    })
    .filter((point) => point.date && point.value > 0)
    .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
  if (points.length) return dedupeHistoryPoints(points);
  return [{
    date: stringField(latestRow, ["event_date"]) || stringField(latestRaw, ["report_date"]) || stringField(latestRow, ["filed_date"]),
    value: latestValue,
    costBasis: 0,
    performance: 0,
    holdingsCount: Math.round(numberField(latestRow, ["holdings_count"], numberField(latestRaw, ["holdings_count"], 0))),
  }];
}

function dedupeHistoryPoints(points: TraderPortfolioHistoryPoint[]): TraderPortfolioHistoryPoint[] {
  const byDate = new Map<string, TraderPortfolioHistoryPoint>();
  for (const point of points) byDate.set(point.date, point);
  return [...byDate.values()].sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
}

function buildThirteenFAllocationHistory(groupRows: RowRecord[]): RowRecord[] {
  const ordered = [...groupRows]
    .sort((a, b) => {
      const aDate = stringField(a, ["event_date", "filed_date"]);
      const bDate = stringField(b, ["event_date", "filed_date"]);
      return aDate.localeCompare(bDate);
    });
  const latest = ordered.at(-1);
  const previous = ordered.at(-2);
  if (!latest) return [];
  const latestHoldings = normalizedThirteenFHoldings(latest);
  const previousWeights = new Map(normalizedThirteenFHoldings(previous).map((holding) => [holding.key, holding.weight]));
  return latestHoldings.slice(0, 25).map((holding) => {
    const before = previousWeights.get(holding.key) ?? 0;
    const after = holding.weight;
    return {
      symbol: holding.symbol,
      security: holding.security,
      put_call: holding.putCall,
      date: stringField(latest, ["event_date"]),
      filed_date: stringField(latest, ["filed_date"]),
      type: before === 0 && after > 0 ? "ADD" : after > before ? "INCREASE" : after < before ? "DECREASE" : "UNCHANGED",
      quantity: holding.quantity,
      estimated_amount: holding.marketValue,
      price: null,
      weight_before: before,
      weight_after: after,
    };
  });
}

function normalizedThirteenFHoldings(row?: RowRecord): Array<{ key: string; symbol: string; security: string; putCall: string; quantity: number; marketValue: number; weight: number }> {
  if (!row) return [];
  const raw = objectField(row.raw);
  const holdings = arrayField(row.holding_sample).length ? arrayField(row.holding_sample) : arrayField(raw.holdings);
  const fallbackTotalValue = holdings.reduce<number>((sum, item) => sum + numberField(objectField(item), ["market_value", "value_thousands", "value"], 0), 0);
  const totalValue = numberField(row, ["amount", "holdings_value_thousands"], numberField(raw, ["holdings_value_thousands"], fallbackTotalValue));
  return holdings.map((item) => {
    const holding = objectField(item);
    const marketValue = numberField(holding, ["market_value", "value_thousands", "value"], 0);
    const symbol = stringField(holding, ["ticker", "symbol"]).toUpperCase();
    const security = stringField(holding, ["name", "title"]) || "13F holding";
    const putCall = stringField(holding, ["put_call", "putCall"]).toUpperCase();
    return {
      key: [symbol || security, putCall, stringField(holding, ["title"])].join(":"),
      symbol,
      security,
      putCall,
      quantity: numberField(holding, ["shares_or_principal_amount", "shares"], 0),
      marketValue,
      weight: numberField(holding, ["weight"], totalValue ? (marketValue / totalValue) * 100 : 0),
    };
  }).sort((a, b) => b.weight - a.weight);
}

function diversificationScoreFromWeights(weights: number[]): number {
  if (!weights.length) return 0;
  return Math.max(0, Math.min(100, Math.round(100 - Math.max(...weights))));
}

function isLikelyTicker(value: string): boolean {
  return /^[A-Z][A-Z0-9.-]{0,9}$/.test(value);
}

function buildTraderFilingCards(filings: Filing[]): TraderFilingCard[] {
  const grouped = new Map<string, Filing[]>();
  for (const filing of filings) {
    const existing = grouped.get(filing.investor) ?? [];
    existing.push(filing);
    grouped.set(filing.investor, existing);
  }
  return Array.from(grouped.entries()).map(([investor, holdings]) => {
    const sorted = [...holdings].sort((a, b) => b.value - a.value);
    const tickerCount = new Set(sorted.map((holding) => holding.ticker).filter(Boolean)).size;
    return {
      investor,
      filed: newestDateLabel(sorted.map((holding) => holding.filed)),
      event: newestDateLabel(sorted.map((holding) => holding.event)),
      holdings: sorted,
      totalValue: sorted.reduce((total, holding) => total + holding.value, 0),
      tickerCount,
      topTicker: sorted.find((holding) => holding.ticker)?.ticker ?? "",
    };
  }).sort((a, b) => b.totalValue - a.totalValue);
}

function buildSignalSourcePanels(data: PanelData): SignalSourcePanel[] {
  const backendRows = rows(data.opportunitySources);
  if (backendRows.length) {
    const grouped = new Map<string, { title: string; rows: RowRecord[] }>();
    for (const row of backendRows) {
      const key = stringField(row, ["source_key"]) || "source";
      const title = stringField(row, ["title"]) || titleLabel(key);
      const existing = grouped.get(key) ?? { title, rows: [] };
      existing.rows.push(row);
      grouped.set(key, existing);
    }
    return Array.from(grouped.entries()).map(([key, value]) => ({
      key,
      title: value.title,
      count: value.rows.length,
      state: value.rows.length ? "live" : "empty",
      leaders: value.rows.slice(0, 4).map((row) => sourceLeaderRow(row, ["score"])).filter((row): row is SummaryItem => row !== null),
    }));
  }
  const definitions: Array<[string, string, RowRecord[], string[]]> = [
    ["technicals", "Technical Setups", rows(data.sepa), ["score", "setup_score"]],
    ["liquidity", "Liquidity", rows(data.liquidity), ["avg_dollar_volume", "score"]],
    ["valuation", "Valuation", rows(data.valuations), ["upside_pct", "score"]],
    ["earnings_setup", "Earnings Setups", rows(data.earningsSetups), ["score", "revision_score"]],
    ["options_payoff", "Options Payoff", rows(data.optionsPayoffScenarios), ["max_profit", "net_premium"]],
    ["thesis", "Thesis / Memos", [...rows(data.thesisMonitor), ...rows(data.theses), ...rows(data.memos)], ["conviction", "score"]],
    ["filings", "Trader Filings", rows(data.disclosures), ["holdings_value_thousands", "value"]],
    ["news", "News / Catalysts", [...rows(data.news), ...rows(data.earnings), ...rows(data.catalysts)], ["score", "importance"]],
  ];
  return definitions.map(([key, title, sourceRows, scoreKeys]) => {
    const leaders = sourceRows
      .map((row) => sourceLeaderRow(row, scoreKeys))
      .filter((row): row is SummaryItem => row !== null)
      .sort((a, b) => Number(b.value.replace(/[^0-9.-]/g, "")) - Number(a.value.replace(/[^0-9.-]/g, "")))
      .slice(0, 4);
    return {
      key,
      title,
      count: sourceRows.length,
      state: sourceRows.length ? "live" : "empty",
      leaders,
    };
  });
}

function sourceLeaderRow(row: RowRecord, scoreKeys: string[]): SummaryItem | null {
  const symbol = symbolFromRow(row);
  if (!symbol) {
    return null;
  }
  const score = numberField(row, scoreKeys, Number.NaN);
  const scoreKey = scoreKeys.find((key) => Number.isFinite(numberField(row, [key], Number.NaN))) ?? "";
  const label = symbol;
  const value = Number.isFinite(score)
    ? formatSourceMetric(scoreKey, score, row)
    : titleLabel(stringField(row, ["grade", "verdict", "status", "event_type"]) || "Loaded");
  return {
    label,
    value,
    caption: displayValue(row.caption ?? row.summary ?? row.title ?? row.stage ?? row.method ?? row.source ?? row.event_date ?? row.filed_date),
    tone: Number.isFinite(score) ? score >= 0 ? "good" : "bad" : "info",
    symbol,
  };
}

function formatSourceMetric(key: string, value: number, row: RowRecord): string {
  if (!Number.isFinite(value)) return "-";
  const context = displayValue(row.caption ?? row.summary ?? row.title ?? row.stage ?? row.method ?? row.source).toLowerCase();
  if (key.includes("dollar") || key.includes("premium") || key.includes("profit") || key === "value" || context.includes("dollar") || context.includes("volume")) {
    return formatCompactMoney(value);
  }
  if (key.includes("thousands")) {
    return formatCompactMoney(value * 1000);
  }
  if (key.includes("pct") || context.includes("upside") || context.includes("%") || Math.abs(value) <= 1) {
    return formatPct(Math.abs(value) <= 1 ? value * 100 : value);
  }
  return formatCompact(value);
}

function buildSignalDefinitions(data: PanelData): SignalDefinition[] {
  return [
    { key: "quote", label: "Quote", rows: rows(data.quotes) },
    { key: "technical", label: "Technical", rows: rows(data.technicals) },
    { key: "sepa", label: "SEPA", rows: rows(data.sepa) },
    { key: "liquidity", label: "Liquidity", rows: rows(data.liquidity) },
    { key: "valuation", label: "Valuation", rows: valuationContextRows(data) },
    { key: "earnings", label: "Earnings", rows: [...rows(data.earningsSetups), ...rows(data.earnings), ...rows(data.analystEstimates)] },
    { key: "options", label: "Options", rows: [...rows(data.optionsPayoffScenarios), ...rows(data.optionsExpiries), ...rows(data.optionsChain)] },
    { key: "filings", label: "Filings", rows: rows(data.disclosures) },
    { key: "thesis", label: "Thesis", rows: [...rows(data.thesisMonitor), ...rows(data.theses)] },
    { key: "news", label: "News", rows: [...rows(data.news), ...rows(data.catalysts)] },
    {
      key: "tradingview",
      label: "TradingView",
      rows: [
        ...rows(data.screener),
        ...rows(data.tradingviewSymbolSearch),
        ...rows(data.tradingviewWatchlists),
        ...rows(data.tradingviewAlerts),
        ...rows(data.tradingviewChartState),
      ],
    },
  ];
}

function buildSignalCoverage(definitions: SignalDefinition[]): SourceCoverage[] {
  return definitions.map((definition) => {
    const symbols = uniqueText(definition.rows.flatMap(rowSymbols));
    const leaders = topSymbolsFromRows(definition.rows).slice(0, 3);
    return {
      key: definition.key,
      label: definition.label,
      count: definition.rows.length,
      symbolCount: symbols.length,
      leaders,
      tone: definition.rows.length ? definition.rows.length >= 10 || symbols.length >= 5 ? "good" : "info" : "muted",
    };
  });
}

function buildSignalMatrix(opportunities: Opportunity[], definitions: SignalDefinition[]): SignalMatrixRow[] {
  return opportunities
    .slice(0, 40)
    .map((opportunity) => {
      const signals = definitions.map((definition) => signalCellFor(opportunity.ticker, definition));
      return {
        ticker: opportunity.ticker,
        name: opportunity.name,
        actionGrade: opportunity.actionGrade,
        score: opportunity.score,
        freshnessStatus: opportunity.freshnessStatus,
        evidenceCount: opportunity.evidenceCount,
        sourceCount: opportunity.sourceCount,
        catalystWindow: opportunity.catalystWindow,
        primaryReason: opportunity.inclusionReasons[0] ?? opportunity.whyNow,
        blockingGates: opportunity.blockingGates,
        signals,
      };
    })
    .sort((a, b) => {
      const aFamilies = a.signals.filter((signal) => signal.count > 0).length;
      const bFamilies = b.signals.filter((signal) => signal.count > 0).length;
      return (b.actionGrade !== "Stale" ? 1 : 0) - (a.actionGrade !== "Stale" ? 1 : 0)
        || bFamilies - aFamilies
        || b.evidenceCount - a.evidenceCount
        || b.score - a.score;
    });
}

function buildFinanceAnalyses(opportunities: Opportunity[], data: PanelData, matrixRows: SignalMatrixRow[]): FinanceAnalysis[] {
  const valuationBySymbol = groupRowsBySymbol(valuationContextRows(data));
  const earningsBySymbol = groupRowsBySymbol([...rows(data.earningsSetups), ...rows(data.analystEstimates), ...rows(data.earnings)]);
  const optionsBySymbol = groupRowsBySymbol([...rows(data.optionsPayoffScenarios), ...rows(data.optionsExpiries)]);
  const tradingviewBySymbol = groupTradingViewRowsBySymbol(data);
  const matrixBySymbol = new Map(matrixRows.map((row) => [row.ticker, row]));

  return opportunities.slice(0, 40).map((opportunity) => {
    const symbol = opportunity.ticker;
    const valuationRows = valuationBySymbol.get(symbol) ?? [];
    const earningsRows = earningsBySymbol.get(symbol) ?? [];
    const optionsRows = optionsBySymbol.get(symbol) ?? [];
    const tradingviewRows = tradingviewBySymbol.get(symbol) ?? [];
    const rejectGate = opportunity.actionGrade === "Reject" ? ["decision_reject"] : [];
    const blockers = [...opportunity.blockingGates, ...rejectGate, ...(opportunity.isStale ? ["stale sources"] : [])];
    const valuation = financeValuationSummary(valuationRows);
    const earnings = financeEarningsSummary(earningsRows);
    const options = financeOptionsSummary(optionsRows, blockers.length > 0);
    const tradingview = financeTradingViewSummary(tradingviewRows);
    const summaries = [valuation, earnings, options, tradingview];
    const coverage = summaries.filter((item) => item.value !== "Not loaded").length;
    const signalRow = matrixBySymbol.get(symbol);
    const missingFamilies = summaries.filter((item) => item.value === "Not loaded").map((item) => item.label);
    const consumable = coverage >= 3 && blockers.length === 0 && ["Act", "Research", "Watch"].includes(opportunity.actionGrade);
    return {
      symbol,
      actionGrade: opportunity.actionGrade,
      score: opportunity.score,
      sourceCount: opportunity.sourceCount,
      evidenceCount: opportunity.evidenceCount,
      coverage,
      tone: consumable ? "good" : blockers.length || coverage <= 1 ? "warn" : "info",
      headline: `${opportunity.actionGrade} · score ${opportunity.score} · ${coverage}/4 loaded`,
      valuation,
      earnings,
      options,
      tradingview,
      missingFamilies,
      blockers,
      nextAction: opportunity.actionGrade === "Reject" ? "No new exposure until score, setup, or source evidence improves." : opportunity.nextAction || opportunity.invalidation || "Open ticker dossier",
      consumable,
      decisionText: financeDecisionText(opportunity, signalRow, coverage, missingFamilies),
    };
  });
}

function valuationContextRows(data: PanelData): RowRecord[] {
  return [
    ...rows(data.valuations),
    ...rows(data.etfPremiums),
    ...rows(data.fundamentals).filter((row) => stringField(row, ["asset_class"]) === "crypto"),
  ];
}

function groupTradingViewRowsBySymbol(data: PanelData): Map<string, RowRecord[]> {
  const grouped = new Map<string, RowRecord[]>();
  const directRows = [
    ...rows(data.screener),
    ...rows(data.tradingviewSymbolSearch),
    ...rows(data.tradingviewAlerts),
    ...rows(data.tradingviewChartState),
  ];
  for (const row of directRows) {
    for (const symbol of rowSymbols(row)) {
      grouped.set(symbol, [...(grouped.get(symbol) ?? []), row]);
    }
  }
  for (const row of rows(data.tradingviewWatchlists)) {
    for (const symbol of rowSymbols(row)) {
      grouped.set(symbol, [...(grouped.get(symbol) ?? []), row]);
    }
  }
  return grouped;
}

function financeValuationSummary(sourceRows: RowRecord[]): SummaryItem {
  if (!sourceRows.length) {
    return missingFinanceItem("Valuation", "No DCF/relative valuation row");
  }
  const etf = sourceRows.find((row) => row.premium_pct !== undefined);
  if (etf) {
    const premium = numberField(etf, ["premium_pct"], Number.NaN);
    return {
      label: "Valuation",
      value: Number.isFinite(premium) ? formatPct(premium) : "ETF context",
      caption: "ETF premium/discount to NAV",
      tone: Number.isFinite(premium) ? Math.abs(premium) <= 0.5 ? "good" : "warn" : "info",
    };
  }
  const crypto = sourceRows.find((row) => stringField(row, ["asset_class"]) === "crypto");
  if (crypto) {
    const metrics = objectField(crypto.metrics);
    const marketCap = numberField(metrics, ["market_cap"], Number.NaN);
    const fdv = numberField(metrics, ["fdv"], Number.NaN);
    return {
      label: "Valuation",
      value: Number.isFinite(marketCap) ? formatCompactMoney(marketCap) : "Crypto context",
      caption: Number.isFinite(fdv) ? `FDV ${formatCompactMoney(fdv)}` : "CoinGecko market snapshot",
      tone: "info",
    };
  }
  const sorted = [...sourceRows].sort((a, b) => numberField(b, ["upside_pct"], -999) - numberField(a, ["upside_pct"], -999));
  const row = sorted[0];
  const upside = numberField(row, ["upside_pct"], Number.NaN);
  return {
    label: "Valuation",
    value: Number.isFinite(upside) ? formatPct(upside) : titleLabel(stringField(row, ["method"]) || "Loaded"),
    caption: [titleLabel(stringField(row, ["method"]) || "model"), row.fair_value !== undefined ? `fair ${formatMoney(numberField(row, ["fair_value"], 0))}` : ""].filter(Boolean).join(" · "),
    tone: Number.isFinite(upside) ? upside >= 0 ? "good" : "bad" : "info",
  };
}

function financeEarningsSummary(sourceRows: RowRecord[]): SummaryItem {
  if (!sourceRows.length) {
    return missingFinanceItem("Earnings", "No setup, estimate, or event row");
  }
  const setup = sourceRows.find((row) => row.score !== undefined || row.verdict !== undefined) ?? sourceRows[0];
  const score = numberField(setup, ["score", "revision_score", "surprise_score"], Number.NaN);
  const verdict = titleLabel(stringField(setup, ["verdict", "event_type", "status"]) || "Loaded");
  return {
    label: "Earnings",
    value: Number.isFinite(score) ? `${Math.round(score)}` : verdict,
    caption: [verdict, formatDateLabel(stringField(setup, ["event_date", "as_of"]))].filter((item) => item && item !== "-").join(" · "),
    tone: verdict.toLowerCase().includes("risk") ? "bad" : Number.isFinite(score) && score >= 65 ? "good" : "info",
  };
}

function financeOptionsSummary(sourceRows: RowRecord[], evidenceOnly = false): SummaryItem {
  if (!sourceRows.length) {
    return missingFinanceItem("Options", "No payoff or expiry row");
  }
  const scenario = sourceRows.find(isOptionsPayoffScenario) ?? sourceRows[0];
  const strategy = titleLabel(stringField(scenario, ["strategy_type", "option_type", "expiry"]) || "Loaded");
  const maxLoss = numberField(scenario, ["max_loss"], Number.NaN);
  const netPremium = numberField(scenario, ["net_premium"], Number.NaN);
  return {
    label: "Options",
    value: strategy,
    caption: evidenceOnly ? "evidence only while blocked" : [
      Number.isFinite(maxLoss) ? `max loss ${formatMoney(Math.abs(maxLoss))}` : "",
      Number.isFinite(netPremium) ? `premium ${formatNetPremium(netPremium)}` : "",
      formatDateLabel(stringField(scenario, ["expiry", "as_of"])),
    ].filter((item) => item && item !== "-").join(" · "),
    tone: evidenceOnly ? "warn" : "info",
  };
}

function financeTradingViewSummary(sourceRows: RowRecord[]): SummaryItem {
  if (!sourceRows.length) {
    return missingFinanceItem("TradingView", "No search, watchlist, alert, chart, or screener row");
  }
  const alertCount = sourceRows.filter((row) => stringField(row, ["alert_type", "condition", "fired_at"])).length;
  const chart = sourceRows.find((row) => stringField(row, ["interval", "layout_id"]));
  return {
    label: "TradingView",
    value: alertCount ? `${alertCount} alerts` : `${sourceRows.length} rows`,
    caption: chart ? `chart ${stringField(chart, ["interval"]) || "loaded"}` : tradingViewCaption(sourceRows[0]),
    tone: "info",
  };
}

function missingFinanceItem(label: string, caption: string): SummaryItem {
  return { label, value: "Not loaded", caption, tone: "muted" };
}

function tradingViewCaption(row: RowRecord): string {
  const raw = stringField(row, ["exchange", "source", "name"]) || "session context";
  return raw.toLowerCase() === "tradingview" ? "TradingView" : titleLabel(raw);
}

function financeDecisionText(opportunity: Opportunity, signalRow: SignalMatrixRow | undefined, coverage: number, missingFamilies: string[]): string {
  const blockers = opportunity.blockingGates.length ? opportunity.blockingGates.join(" · ") : "";
  if (opportunity.isStale) return "Not ready: refresh stale sources before using this decision.";
  if (opportunity.actionGrade === "Reject") return "Rejected by the current score/setup; use this as evidence review, not a trade setup.";
  if (blockers) return `Decision gated by ${formatGateList(opportunity.blockingGates)}.`;
  if (coverage >= 3 && signalRow) return `${opportunity.actionGrade} review is ready: valuation, event/setup, and market context are present.`;
  if (coverage > 0) return `Needs ${missingFamilies.join(", ") || "more context"} before this deserves attention.`;
  return "No deterministic finance-skill analysis is loaded for this candidate yet.";
}

function signalCellFor(symbol: string, definition: SignalDefinition): SignalCell {
  const matches = definition.rows.filter((row) => rowMatchesSymbol(row, symbol));
  const value = signalCellValue(definition.key, matches[0]);
  return {
    key: definition.key,
    label: definition.label,
    count: matches.length,
    value,
    tone: signalTone(definition.key, matches[0], matches.length),
  };
}

function signalCellValue(key: SignalKey, row?: RowRecord): string {
  if (!row) return "No rows";
  if (key === "quote") return quoteLabel(row);
  if (key === "technical") return `${Math.round(numberField(row, ["technical_score", "score"], 0))}`;
  if (key === "sepa") return stringField(row, ["stage", "grade", "verdict"]) || `${Math.round(numberField(row, ["score", "setup_score"], 0))}`;
  if (key === "liquidity") return titleLabel(stringField(row, ["grade", "liquidity_grade"]) || "loaded");
  if (key === "valuation") {
    const premium = numberField(row, ["premium_pct"], Number.NaN);
    if (Number.isFinite(premium)) return formatPct(premium);
    if (stringField(row, ["asset_class"]) === "crypto") {
      const metrics = objectField(row.metrics);
      const marketCap = numberField(metrics, ["market_cap"], Number.NaN);
      return Number.isFinite(marketCap) ? formatCompactMoney(marketCap) : "Crypto";
    }
    const upside = numberField(row, ["upside_pct"], Number.NaN);
    return Number.isFinite(upside) ? formatPct(upside) : titleLabel(stringField(row, ["method"]) || "loaded");
  }
  if (key === "earnings") return titleLabel(stringField(row, ["verdict", "event_type", "status"]) || formatDateLabel(stringField(row, ["event_date", "as_of"])));
  if (key === "options") return titleLabel(stringField(row, ["strategy_type", "option_type", "expiry"]) || "loaded");
  if (key === "filings") return stringField(row, ["trader_name", "filer_name", "source_type"]) || "loaded";
  if (key === "thesis") return titleLabel(stringField(row, ["decision", "conviction", "status"]) || "loaded");
  if (key === "news") return stringField(row, ["event", "title", "event_type"]) || "loaded";
  return titleLabel(stringField(row, ["status", "source", "provider", "exchange"]) || "loaded");
}

function signalTone(key: SignalKey, row: RowRecord | undefined, count: number): Tone {
  if (!count || !row) return "muted";
  if (key === "valuation") {
    if (stringField(row, ["asset_class"]) === "crypto") return "info";
    const premium = numberField(row, ["premium_pct"], Number.NaN);
    if (Number.isFinite(premium)) return Math.abs(premium) <= 0.5 ? "good" : "warn";
    const upside = numberField(row, ["upside_pct"], Number.NaN);
    return Number.isFinite(upside) ? upside >= 0 ? "good" : "bad" : "info";
  }
  if (key === "quote") {
    const change = numberField(row, ["change_pct", "percent_change", "change"], Number.NaN);
    return Number.isFinite(change) ? change >= 0 ? "good" : "bad" : "info";
  }
  if (key === "earnings") {
    const verdict = stringField(row, ["verdict", "status"]).toLowerCase();
    if (verdict.includes("risk")) return "bad";
    if (verdict.includes("positive") || verdict.includes("constructive")) return "good";
  }
  if (key === "options" || key === "tradingview" || key === "technical" || key === "sepa") return "good";
  return "info";
}

function rowMatchesSymbol(row: RowRecord, symbol: string): boolean {
  return rowSymbols(row).includes(symbol);
}

function rowSymbols(row: RowRecord): string[] {
  const direct = symbolFromRow(row);
  return uniqueText([
    direct,
    ...listField(row, ["symbols", "related_symbols", "underlyings", "tickers"]).map(tickerSymbol),
  ].filter(Boolean));
}

function topSymbolsFromRows(sourceRows: RowRecord[]): string[] {
  const counts = new Map<string, number>();
  for (const symbol of sourceRows.flatMap(rowSymbols)) {
    counts.set(symbol, (counts.get(symbol) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([symbol]) => symbol);
}

function shortSignalLabel(key: SignalKey): string {
  return {
    quote: "Px",
    technical: "Tech",
    sepa: "SEPA",
    liquidity: "Liq",
    valuation: "Val",
    earnings: "Earn",
    options: "Opt",
    filings: "Fil",
    thesis: "Thesis",
    news: "News",
    tradingview: "TV",
  }[key];
}

function compactCellCount(count: number): string {
  return count > 9 ? "9+" : String(count);
}

function buildCalendar(catalystRows: RowRecord[], earningsRows: RowRecord[]): CalendarEvent[] {
  const combined = [...catalystRows, ...earningsRows].slice(0, 60);
  return combined.map((row, index) => {
    const rawDate = stringField(row, ["start_at", "event_date", "date", "due_date", "published_at"]);
    const parsed = parseCalendarDate(rawDate);
    const symbol = stringField(row, ["symbol", "ticker"]);
    const eventType = stringField(row, ["event_kind", "event_type", "type", "title"]) || "event";
    const label = stringField(row, ["event", "title", "event_type"]) || "Market Event";
    const eventDate = stringField(row, ["event_date", "date"]) || (parsed ? parsed.toISOString().slice(0, 10) : "");
    return {
      id: stringField(row, ["id"]) || `${eventDate}-${label}-${index}`,
      fullDate: eventDate,
      date: parsed && !Number.isNaN(parsed.getTime()) ? parsed.getDate() : 0,
      dateText: parsed && !Number.isNaN(parsed.getTime()) ? formatCalendarDate(parsed, stringField(row, ["start_at"]), stringField(row, ["end_at"])) : "-",
      monthLabel: parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleDateString(undefined, { month: "long", year: "numeric" }) : "Source Calendar",
      label: [symbol, label].filter(Boolean).join(" ") || "Market Event",
      symbol,
      sourceUrl: stringField(row, ["source_url"]),
      sourceName: stringField(row, ["source_name", "source"]) || "source",
      status: stringField(row, ["verification_status"]) || "confirmed",
      importance: stringField(row, ["importance"]) || "medium",
      type: calendarType(eventType),
    };
  }).filter((event) => event.date > 0);
}

function buildFreshnessHealthRows(freshnessRows: RowRecord[]): HealthRow[] {
  return freshnessRows.slice(0, 50).map((row) => {
    const status = normalizeHealthStatus(stringField(row, ["freshness_status", "status", "provider_status"]));
    const kind = status === "Documentation" || isDocumentationRow(row) ? "documentation" : "freshness";
    return {
      provider: stringField(row, ["source", "source_key", "provider", "capability", "source_name"]) || "Source",
      status: kind === "documentation" ? "Documentation" : status,
      freshness: stringField(row, ["last_observed_at", "as_of", "latest_source_timestamp", "checked_at", "finished_at", "freshness"]) || "unknown",
      lastRun: clearSourceState(row),
      sourceUrl: stringField(row, ["source_url", "documentation_url", "source", "source_key"]) || stringField(row, ["provider"]) || "local",
      kind,
      contract: stringField(row, ["freshness_contract", "contract", "cadence"]) || sourceFreshnessContract(row),
      staleAfter: stringField(row, ["stale_after", "stale_after_label", "ttl"]) || "-",
    };
  });
}

function buildSourceHealthRows(sourceRows: RowRecord[]): HealthRow[] {
  return sourceRows.slice(0, 50).map((row) => {
    const status = normalizeHealthStatus(stringField(row, ["status", "run_status", "provider_status"]));
    const kind = status === "Documentation" || isDocumentationRow(row) ? "documentation" : "provider";
    return {
      provider: stringField(row, ["source", "provider", "capability"]) || "Provider",
      status: kind === "documentation" ? "Documentation" : status,
      freshness: stringField(row, ["checked_at", "finished_at", "freshness", "as_of"]) || "recent",
      lastRun: stringField(row, ["detail", "message", "last_error"]) || (kind === "documentation" ? "Documentation row" : "OK"),
      sourceUrl: stringField(row, ["source_url", "documentation_url", "source"]) || stringField(row, ["provider"]) || "local",
      kind,
      contract: sourceFreshnessContract(row),
      staleAfter: stringField(row, ["stale_after", "ttl"]) || "-",
    };
  });
}

function buildProviderRunRows(providerRows: RowRecord[]): HealthRow[] {
  return providerRows.slice(0, 50).map((row) => {
    const status = normalizeHealthStatus(stringField(row, ["status", "run_status", "provider_status"]));
    return {
      provider: [stringField(row, ["provider"]), stringField(row, ["capability"])].filter(Boolean).join(" / ") || "Provider run",
      status,
      freshness: stringField(row, ["finished_at", "checked_at", "started_at", "freshness", "as_of"]) || "recent",
      lastRun: stringField(row, ["detail", "message", "last_error"]) || "OK",
      sourceUrl: stringField(row, ["source_url", "documentation_url", "source", "provider"]) || "local",
      kind: "run",
      contract: sourceFreshnessContract(row),
      staleAfter: stringField(row, ["stale_after", "ttl"]) || "-",
    };
  });
}

function buildBrokerHealthRows(statusRows: RowRecord[]): HealthRow[] {
  return statusRows.slice(0, 20).map((row) => {
    const status = normalizeHealthStatus(stringField(row, ["status", "health"]));
    return {
      provider: `Broker / ${stringField(row, ["provider"]) || "provider"}`,
      status,
      freshness: stringField(row, ["last_data_at", "checked_at"]) || "unknown",
      lastRun: stringField(row, ["detail"]) || "Broker gateway state",
      sourceUrl: stringField(row, ["provider"]) || "broker",
      kind: "provider",
      contract: "Broker/account sync must be healthy before recommendations can stage paper orders.",
      staleAfter: "15 minutes",
    };
  });
}

function clearSourceState(row: RowRecord): string {
  const freshness = stringField(row, ["freshness_status"]);
  const providerStatus = stringField(row, ["provider_status", "status", "run_status"]);
  const detail = stringField(row, ["detail", "message"]);
  const state = freshness && freshness !== providerStatus ? `${freshness}${providerStatus ? ` · provider ${providerStatus}` : ""}` : providerStatus || freshness;
  return [state, detail].filter(Boolean).join(" · ") || "No run status";
}

function nextDaysEvents(events: CalendarEvent[], days: number): CalendarEvent[] {
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const end = start + days * 24 * 60 * 60 * 1000;
  return events.filter((event) => {
    const date = parseCalendarDate(event.fullDate);
    if (!date) return false;
    const time = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    return time >= start && time < end;
  });
}

function eventsByTickerRows(events: CalendarEvent[]): RowRecord[] {
  const groups = new Map<string, CalendarEvent[]>();
  for (const event of events) {
    if (!event.symbol) continue;
    groups.set(event.symbol, [...(groups.get(event.symbol) ?? []), event]);
  }
  return [...groups.entries()].map(([symbol, items]) => ({
    symbol,
    events: items.length,
    next_event: items[0]?.dateText,
    title: `${symbol} · ${items.length} events`,
    detail: items.slice(0, 3).map((item) => item.label).join(" · "),
  }));
}

function parseCalendarDate(value: string): Date | null {
  if (!value) {
    return null;
  }
  const dateOnly = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dateOnly) {
    return new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]));
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatCalendarDate(date: Date, startAt: string, endAt: string): string {
  const dateText = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const start = startAt ? parseCalendarDate(startAt) : null;
  const end = endAt ? parseCalendarDate(endAt) : null;
  const timeText = start && !Number.isNaN(start.getTime()) && startAt.includes("T")
    ? ` ${start.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`
    : "";
  if (end && !Number.isNaN(end.getTime()) && end.toDateString() !== date.toDateString()) {
    return `${dateText}-${end.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  }
  return `${dateText}${timeText}`;
}

function buildSectorRows(screenerRows: RowRecord[]): SummaryItem[] {
  const counts = new Map<string, number>();
  for (const row of screenerRows) {
    const metrics = objectField(row.metrics);
    const sector = stringField(metrics, ["sector"]) || "Unclassified";
    counts.set(sector, (counts.get(sector) ?? 0) + 1);
  }
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([label, count]) => ({ label, value: String(count), caption: "TradingView screener rows", tone: "info" }));
}

function buildSetupRows(sepaRows: RowRecord[], liquidityRows: RowRecord[]): SummaryItem[] {
  const liquidityBySymbol = new Map(liquidityRows.map((row) => [stringField(row, ["symbol"]).toUpperCase(), row]));
  return sepaRows.slice(0, 9).map((row) => {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    const liquidity = liquidityBySymbol.get(symbol);
    const score = numberField(row, ["score"], 0);
    return {
      label: symbol || "Setup",
      value: `${Math.round(score)}`,
      caption: `${titleLabel(stringField(row, ["verdict", "stage"]) || "setup")} · ${stringField(liquidity ?? {}, ["grade"]) || "liquidity n/a"}`,
      tone: score >= 80 ? "good" : score >= 50 ? "warn" : "bad",
      symbol,
    };
  });
}

function buildLiquidityRows(liquidityRows: RowRecord[], symbols?: Set<string>): SummaryItem[] {
  const filtered = symbols?.size ? liquidityRows.filter((row) => symbols.has(stringField(row, ["symbol"]).toUpperCase())) : liquidityRows;
  return filtered.slice(0, 9).map((row) => {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    return {
      label: symbol || "Liquidity",
      value: titleLabel(stringField(row, ["grade"]) || "not loaded"),
      caption: `${formatMoney(numberField(row, ["avg_dollar_volume"], 0))} ADV`,
      tone: stringField(row, ["grade"]).includes("high") ? "good" : "info",
      symbol,
    };
  });
}

function buildCorrelationRows(correlationRows: RowRecord[], symbols?: Set<string>): SummaryItem[] {
  const filtered = symbols?.size ? correlationRows.filter((row) => symbols.has(stringField(row, ["symbol"]).toUpperCase())) : correlationRows;
  return filtered.slice(0, 9).map((row) => {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    const peers = arrayField(row.peers);
    const topPeer = objectField(peers[0]);
    return {
      label: symbol || "Correlation",
      value: stringField(topPeer, ["symbol"]) || "-",
      caption: `Top peer corr ${displayValue(topPeer.correlation)} over ${displayValue(row.lookback_days)}d`,
      tone: "info",
      symbol,
    };
  });
}

function buildValuationRows(valuationRows: RowRecord[]): SummaryItem[] {
  return valuationRows.slice(0, 9).map((row) => {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    const upside = numberField(row, ["upside_pct"], 0);
    return {
      label: symbol || "Valuation",
      value: formatPct(upside),
      caption: `${titleLabel(stringField(row, ["method"]) || "valuation")} fair ${formatMoney(numberField(row, ["fair_value"], 0))}`,
      tone: upside >= 0 ? "good" : "bad",
      symbol,
    };
  });
}

function buildTechnicalRows(technicalRows: RowRecord[]): SummaryItem[] {
  return technicalRows.slice(0, 12).map((row) => {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    const score = numberField(row, ["technical_score"], 0);
    const return20 = numberField(row, ["return_20d"], 0);
    const close = numberField(row, ["close"], 0);
    return {
      label: symbol || "Technical",
      value: `${Math.round(score)}`,
      caption: `20d ${formatPct(return20 * 100)} · close ${formatMoney(close)}`,
      tone: score >= 70 ? "good" : score >= 45 ? "warn" : "bad",
      symbol,
    };
  });
}

function holdingSummaryRows(holdings: Holding[]): SummaryItem[] {
  return holdings.slice(0, 6).map((holding) => ({
    label: holding.ticker,
    value: holding.weight ? `${holding.weight.toFixed(1)}%` : holding.hasMarketValue ? formatMoney(holding.marketValue) : "Price not loaded",
    caption: holding.hasPnl ? `${holding.taxLotTerm} · ${formatMoney(holding.unrealizedPnl)} P/L` : `${holding.taxLotTerm} · quote ${holding.quoteFreshness}`,
    tone: !holding.hasMarketValue ? "warn" : holding.unrealizedPnl >= 0 ? "good" : "bad",
    symbol: holding.ticker,
  }));
}

function portfolioPositionReviewRows(holdings: Holding[], valuationRows: SummaryItem[], technicalRows: SummaryItem[]): SummaryItem[] {
  return holdings.map((holding) => {
    const valuation = findValuationForSymbol(valuationRows, holding.ticker);
    const technical = findSummaryForSymbol(technicalRows, holding.ticker);
    const facts = [
      holding.nextStep,
      valuation ? `valuation ${valuation.value}` : "",
      technical ? `technical ${technical.value}` : "",
      holding.blockers.length ? `blockers: ${holding.blockers.join(", ")}` : "",
    ].filter(Boolean);
    return {
      label: holding.ticker,
      value: holding.addStance,
      caption: facts.join(" · "),
      tone: holdingStanceTone(holding.addStance),
      symbol: holding.ticker,
    };
  });
}

const PORTFOLIO_CHART_COLORS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#0891b2", "#dc2626", "#4f46e5", "#64748b"];

function summarizePortfolio(holdings: Holding[]): PortfolioStats {
  const priced = holdings.filter((holding) => holding.hasMarketValue);
  const portfolioValue = priced.reduce((total, holding) => total + holding.marketValue, 0);
  const costBasis = holdings.reduce((total, holding) => total + holding.quantity * holding.averageCost, 0);
  const unrealizedPnl = holdings.reduce((total, holding) => total + (holding.hasPnl ? holding.unrealizedPnl : 0), 0);
  const dayChange = holdings.reduce((total, holding) => total + (Number.isFinite(holding.dayChangeValue) ? holding.dayChangeValue : 0), 0);
  const sortedWeights = priced.map((holding) => holding.weight).sort((a, b) => b - a);
  const top3Weight = sortedWeights.slice(0, 3).reduce((sum, weight) => sum + weight, 0);
  const largest = priced.slice().sort((a, b) => b.weight - a.weight)[0];
  const gainers = holdings.filter((holding) => holding.hasPnl && holding.unrealizedPnl >= 0).length;
  const losers = holdings.filter((holding) => holding.hasPnl && holding.unrealizedPnl < 0).length;
  const quoteGapCount = holdings.filter((holding) => !holding.hasMarketValue || ["missing", "stale", "failed"].includes(holding.quoteFreshness)).length;
  const shortTermWeight = priced.filter((holding) => holding.taxLotTerm.toLowerCase().includes("short")).reduce((sum, holding) => sum + holding.weight, 0);
  const longTermWeight = priced.filter((holding) => holding.taxLotTerm.toLowerCase().includes("long")).reduce((sum, holding) => sum + holding.weight, 0);
  const concentrationScore = Math.min(top3Weight / 80, 1) * 42;
  const quoteGapScore = holdings.length ? Math.min(quoteGapCount / holdings.length, 1) * 24 : 0;
  const loserScore = holdings.length ? Math.min(losers / holdings.length, 1) * 18 : 0;
  const shortTermScore = Math.min(shortTermWeight / 80, 1) * 16;
  return {
    totalCount: holdings.length,
    pricedCount: priced.length,
    unpricedCount: holdings.length - priced.length,
    portfolioValue,
    costBasis,
    unrealizedPnl,
    unrealizedPnlPct: costBasis > 0 ? (unrealizedPnl / costBasis) * 100 : 0,
    dayChange,
    dayChangePct: portfolioValue - dayChange > 0 ? (dayChange / (portfolioValue - dayChange)) * 100 : 0,
    top3Weight,
    largest,
    gainers,
    losers,
    shortTermWeight,
    longTermWeight,
    quoteGapCount,
    riskScore: concentrationScore + quoteGapScore + loserScore + shortTermScore,
  };
}

function portfolioAllocationBucket(holding: Holding): string {
  return holding.assetClass && holding.assetClass !== "Unclassified" ? holding.assetClass : holding.category || "Unclassified";
}

function portfolioAllocationBuckets(holdings: Holding[]): Array<{ name: string; value: number; marketValue: number }> {
  const grouped = new Map<string, { name: string; marketValue: number }>();
  const pricedValue = holdings.filter((holding) => holding.hasMarketValue).reduce((sum, holding) => sum + holding.marketValue, 0);
  if (!pricedValue) return [];
  for (const holding of holdings) {
    if (!holding.hasMarketValue) continue;
    const name = portfolioAllocationBucket(holding);
    const current = grouped.get(name) ?? { name, marketValue: 0 };
    current.marketValue += holding.marketValue;
    grouped.set(name, current);
  }
  const rows = [...grouped.values()]
    .map((bucket) => ({ ...bucket, value: (bucket.marketValue / pricedValue) * 100 }))
    .sort((a, b) => b.value - a.value);
  const leading = rows.slice(0, 7);
  const other = rows.slice(7).reduce((sum, bucket) => sum + bucket.marketValue, 0);
  if (other > 0) {
    leading.push({ name: "Other", marketValue: other, value: (other / pricedValue) * 100 });
  }
  return leading;
}

function portfolioHeatmapMetric(holding: Holding, mode: string): number {
  if (mode === "Weight") return holding.weight;
  if (mode === "Day") return Number.isFinite(holding.dayChangePct) ? holding.dayChangePct : 0;
  return holding.hasPnl ? holding.unrealizedPnlPct : 0;
}

function portfolioHeatmapTone(value: number, mode: string): "positive" | "negative" | "neutral" | "heavy" {
  if (mode === "Weight") return value >= 30 ? "heavy" : "neutral";
  if (value > 0.05) return "positive";
  if (value < -0.05) return "negative";
  return "neutral";
}

function portfolioPerformancePoints(holdings: Holding[], stats: PortfolioStats): Array<{ label: string; value: number; costBasis: number }> {
  if (!holdings.length || stats.costBasis <= 0) return [];
  const previousValue = Math.max(0, stats.portfolioValue - stats.dayChange);
  const points = [
    { label: "Cost", value: stats.costBasis, costBasis: stats.costBasis },
    { label: "Prev Close", value: previousValue || stats.portfolioValue, costBasis: stats.costBasis },
    { label: "Current", value: stats.portfolioValue, costBasis: stats.costBasis },
  ];
  return points.filter((point) => Number.isFinite(point.value) && point.value > 0);
}

function portfolioCorrelationMatrixSymbols(holdings: Holding[], rows: RowRecord[]): string[] {
  const owned = holdings
    .filter((holding) => holding.hasMarketValue)
    .slice()
    .sort((a, b) => b.weight - a.weight)
    .map((holding) => holding.ticker);
  const ownedSet = new Set(owned);
  const peers = rows
    .filter((row) => ownedSet.has(stringField(row, ["symbol"]).toUpperCase()))
    .slice()
    .sort((a, b) => numberField(b, ["abs_correlation"], Math.abs(numberField(b, ["correlation"], 0))) - numberField(a, ["abs_correlation"], Math.abs(numberField(a, ["correlation"], 0))))
    .map((row) => stringField(row, ["peer_symbol"]).toUpperCase())
    .filter(Boolean);
  const symbols: string[] = [];
  for (const symbol of [...owned, ...peers]) {
    if (symbol && !symbols.includes(symbol)) {
      symbols.push(symbol);
    }
    if (symbols.length >= 8) break;
  }
  return symbols;
}

function portfolioCorrelationLookup(rows: RowRecord[]): Map<string, number> {
  const lookup = new Map<string, number>();
  for (const row of rows) {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    const peer = stringField(row, ["peer_symbol"]).toUpperCase();
    const correlation = optionalNumberField(row, ["correlation"]);
    if (!symbol || !peer || correlation === null) continue;
    lookup.set(`${symbol}:${peer}`, correlation);
    lookup.set(`${peer}:${symbol}`, correlation);
  }
  return lookup;
}

function portfolioCorrelationTone(value: number | undefined): "self" | "empty" | "negative" | "neutral" | "positive" | "strong" {
  if (value === undefined) return "empty";
  if (value === 1) return "self";
  if (value <= -0.35) return "negative";
  if (Math.abs(value) >= 0.7) return "strong";
  if (Math.abs(value) >= 0.35) return "positive";
  return "neutral";
}

function portfolioDisplayExposureClusters(rows: RowRecord[]): RowRecord[] {
  const actionable = rows.filter((row) => booleanField(row, ["is_actionable"]) || (stringField(row, ["cluster_type"]) !== "asset_class" && stringField(row, ["concentration_level"]) !== "normal"));
  const candidates = actionable.length ? actionable : rows.filter((row) => stringField(row, ["cluster_type"]) !== "asset_class");
  const output: RowRecord[] = [];
  const seen = new Set<string>();
  for (const row of candidates) {
    const symbols = arrayField(row.symbols).map((item) => displayValue(item as JsonValue)).filter(Boolean).sort();
    const key = `${symbols.join("/")}:${numberField(row, ["portfolio_weight"], 0).toFixed(1)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(row);
    if (output.length >= 6) break;
  }
  return output;
}

function portfolioRiskRows(holdings: Holding[], liquidityRows: SummaryItem[], correlationRows: SummaryItem[]): SummaryItem[] {
  const rows: SummaryItem[] = [];
  const largest = holdings.filter((holding) => holding.hasMarketValue).slice().sort((a, b) => b.weight - a.weight)[0];
  if (largest) {
    rows.push({
      label: "Largest position",
      value: `${largest.ticker} ${largest.weight.toFixed(1)}%`,
      caption: largest.weight > 50 ? "Concentration needs active sizing discipline" : "No single holding above 50%",
      tone: largest.weight > 50 ? "warn" : "info",
      symbol: largest.ticker,
    });
  }
  for (const row of liquidityRows.slice(0, 2)) {
    rows.push({ ...row, label: `${row.label} liquidity` });
  }
  for (const row of correlationRows.slice(0, 2)) {
    rows.push({ ...row, label: `${row.label} top corr` });
  }
  return rows;
}

function portfolioValuationRows(holdings: Holding[], valuationRows: SummaryItem[], technicalRows: SummaryItem[]): SummaryItem[] {
  const output: SummaryItem[] = [];
  for (const holding of holdings) {
    const valuation = findValuationForSymbol(valuationRows, holding.ticker);
    const technical = findSummaryForSymbol(technicalRows, holding.ticker);
    if (!valuation && !technical) continue;
    const value = valuation?.value ?? "No valuation";
    const caption = [
      valuation?.caption,
      technical ? `technical ${technical.value}: ${technical.caption}` : "",
    ].filter(Boolean).join(" · ");
    const upside = Number((valuation?.value ?? "").replace(/[%+]/g, ""));
    output.push({
      label: holding.ticker,
      value,
      caption,
      tone: Number.isFinite(upside) ? upside >= 0 ? "good" : "bad" : "info",
      symbol: holding.ticker,
    });
  }
  return output;
}

function portfolioTaxRows(holdings: Holding[]): SummaryItem[] {
  return holdings.map((holding) => {
    const daysToLongTerm = Math.max(0, 366 - holding.holdingDays);
    const isLongTerm = holding.taxLotTerm.toLowerCase().includes("long");
    return {
      label: holding.ticker,
      value: isLongTerm ? "Long term" : `${daysToLongTerm}d to LT`,
      caption: `${holding.purchaseDate || "No purchase date"} · ${holding.holdingDays || 0} days held`,
      tone: isLongTerm ? "good" : daysToLongTerm <= 90 ? "warn" : "info",
      symbol: holding.ticker,
    };
  });
}

function findSummaryForSymbol(rows: SummaryItem[], symbol: string): SummaryItem | undefined {
  return rows.find((row) => row.symbol === symbol || row.label === symbol);
}

function findValuationForSymbol(rows: SummaryItem[], symbol: string): SummaryItem | undefined {
  const matches = rows.filter((row) => row.symbol === symbol || row.label === symbol);
  return matches.find((row) => row.caption.toLowerCase().includes("blended")) ?? matches[0];
}

function holdingAddStance(rawSignal: string, quoteFreshness: string, decisionScore: number): string {
  const normalized = rawSignal.toLowerCase();
  if (quoteFreshness === "missing" || quoteFreshness === "stale") return "Refresh first";
  if (normalized.includes("act") || normalized.includes("research") || decisionScore >= 75) return "Consider add";
  if (normalized.includes("watch") || normalized.includes("monitor") || decisionScore >= 55) return "Hold / monitor";
  if (normalized.includes("reject") || normalized.includes("avoid") || decisionScore < 55) return "No new buys";
  return "Hold / monitor";
}

function holdingNextStep(addStance: string, quoteFreshness: string, weight: number, blockers: string[]): string {
  if (quoteFreshness === "missing" || quoteFreshness === "stale") return "Refresh quote before using this row";
  if (blockers.length) return `Clear ${blockers[0].replace(/_/g, " ")}`;
  if (addStance === "Consider add") return weight > 35 ? "Confirm sizing before increasing" : "Review thesis and entry price";
  if (addStance === "No new buys") return "Hold only; revisit thesis or valuation";
  if (weight > 50) return "Hold; watch concentration";
  return "Hold; monitor catalysts";
}

function holdingStanceTone(stance: string): Tone {
  const normalized = stance.toLowerCase();
  if (normalized.includes("consider")) return "good";
  if (normalized.includes("no new") || normalized.includes("do not") || normalized.includes("refresh")) return "warn";
  return "info";
}

function newestDateLabel(values: string[]): string {
  const latest = values
    .map((value) => new Date(value))
    .filter((value) => !Number.isNaN(value.getTime()))
    .sort((a, b) => b.getTime() - a.getTime())[0];
  return latest ? latest.toLocaleString() : "recently";
}

function calendarType(value: string): CalendarEvent["type"] {
  const normalized = value.toLowerCase();
  if (normalized.includes("earn")) return "earnings";
  if (normalized.includes("option")) return "options";
  if (normalized.includes("filing") || normalized.includes("13f")) return "filing";
  if (normalized.includes("cpi") || normalized.includes("fomc") || normalized.includes("economic")) return "economic";
  return "event";
}

function todayChangedRows(model: AppModel, lastRefresh: Date | null): SummaryItem[] {
  const movers = model.watchlist
    .filter((item) => Math.abs(item.change) >= 0.1)
    .slice()
    .sort((a, b) => Math.abs(b.change) - Math.abs(a.change))
    .slice(0, 4)
    .map((item) => ({
      label: item.symbol,
      value: formatPct(item.change),
      caption: `Latest quote ${item.price}`,
      tone: item.change >= 0 ? "good" as Tone : "bad" as Tone,
      symbol: item.symbol,
    }));
  const upcoming = nextDaysEvents(model.calendar, 7).slice(0, 2).map((event) => ({
    label: event.symbol || event.type,
    value: event.dateText,
    caption: event.label,
    tone: event.importance === "high" ? "warn" as Tone : "info" as Tone,
    symbol: event.symbol || undefined,
  }));
  const filing = model.filings[0] ? [{
    label: "Latest filing",
    value: model.filings[0].ticker || model.filings[0].investor,
    caption: `${model.filings[0].investor} ${model.filings[0].action} filed ${model.filings[0].filed}`,
    tone: "info" as Tone,
    symbol: model.filings[0].ticker,
  }] : [];
  const fallback: SummaryItem = {
    label: "Refresh state",
    value: lastRefresh ? lastRefresh.toLocaleTimeString() : "Not loaded",
    caption: "No material quote, filing, or calendar deltas are visible in the loaded rows.",
    tone: lastRefresh ? "muted" : "warn",
  };
  const rows = [...movers, ...upcoming, ...filing];
  return rows.length ? rows.slice(0, 6) : [fallback];
}

function todayMattersRows(model: AppModel, readyCount: number): SummaryItem[] {
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const largest = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  const topOpportunity = model.opportunities.find((item) => item.actionGrade === "Act" || item.actionGrade === "Research") ?? model.opportunities[0];
  const rows: SummaryItem[] = [];
  if (largest) {
    rows.push({
      label: "Largest exposure",
      value: `${largest.ticker} ${largest.weight.toFixed(1)}%`,
      caption: largest.nextStep,
      tone: largest.weight > 30 ? "warn" : "info",
      symbol: largest.ticker,
    });
  }
  if (model.correlationRows[0]) {
    rows.push({
      ...model.correlationRows[0],
      label: "Cluster risk",
    });
  }
  if (topOpportunity) {
    rows.push({
      label: "Decision candidate",
      value: `${topOpportunity.ticker} ${topOpportunity.score}`,
      caption: topOpportunity.nextAction || topOpportunity.whyNow || topOpportunity.decisionBasis,
      tone: topOpportunity.actionGrade === "Act" ? "good" : topOpportunity.blockingGates.length ? "warn" : "info",
      symbol: topOpportunity.ticker,
    });
  }
  rows.push({
    label: "Ready evidence",
    value: `${readyCount} ready`,
    caption: readyCount ? "Names have enough backend evidence to review." : "Decision rows are blocked or not loaded.",
    tone: readyCount ? "good" : "warn",
  });
  return rows.slice(0, 6);
}

function todayReviewRows(model: AppModel, attentionItems: AttentionItem[]): SummaryItem[] {
  const attentionRows = attentionItems.map((item) => ({
    label: item.label,
    value: item.value,
    caption: item.action,
    tone: item.tone,
    symbol: item.symbol,
  }));
  const readyRows = model.financeAnalyses
    .filter((analysis) => analysis.consumable)
    .slice(0, 3)
    .map((analysis) => ({
      label: "Review",
      value: analysis.symbol,
      caption: analysis.nextAction || analysis.decisionText,
      tone: analysis.actionGrade === "Act" ? "good" as Tone : "info" as Tone,
      symbol: analysis.symbol,
    }));
  return [...attentionRows, ...readyRows].slice(0, 6);
}

function todayIgnoreRows(model: AppModel): SummaryItem[] {
  const rows = model.opportunities
    .filter((item) => item.actionGrade === "Watch" || item.actionGrade === "Reject" || item.actionGrade === "Stale")
    .slice(0, 4)
    .map((item) => ({
      label: item.actionGrade === "Reject" ? "Pass" : "Monitor only",
      value: item.ticker,
      caption: item.blockingGates[0] ? formatGateLabel(item.blockingGates[0]) : item.invalidation || item.decisionBasis,
      tone: item.actionGrade === "Reject" || item.actionGrade === "Stale" ? "bad" as Tone : "muted" as Tone,
      symbol: item.ticker,
    }));
  return rows.length ? rows : [{ label: "Ignore list", value: "None", caption: "No watch/reject/stale opportunities are loaded.", tone: "muted" }];
}

function todayBlockedEvidenceRows(model: AppModel, blockedRows: RowRecord[]): SummaryItem[] {
  const decisionBlocks = blockedRows.slice(0, 4).map((row) => {
    const symbol = stringField(row, ["symbol"]);
    const blockers = listField(row, ["blockers", "blocking_gates"]);
    return {
      label: symbol || "Decision row",
      value: stringField(row, ["status"]) || "blocked",
      caption: stringField(row, ["next_action"]) || blockers.map(formatGateLabel).join(" · ") || "Missing readiness evidence",
      tone: "warn" as Tone,
      symbol,
    };
  });
  const staleSources = model.freshnessHealthRows
    .filter((row) => row.status === "Warning" || row.status === "Degraded")
    .slice(0, 3)
    .map((row) => ({
      label: row.provider,
      value: row.status,
      caption: row.contract || row.freshness,
      tone: row.status === "Degraded" ? "bad" as Tone : "warn" as Tone,
    }));
  const thinRows = model.opportunities
    .filter((item) => item.isSourceThin || item.isStale || item.blockingGates.length)
    .slice(0, 3)
    .map((item) => ({
      label: item.ticker,
      value: item.isStale ? "stale" : item.isSourceThin ? "source-thin" : "blocked",
      caption: item.blockingGates[0] ? formatGateLabel(item.blockingGates[0]) : item.nextAction,
      tone: "warn" as Tone,
      symbol: item.ticker,
    }));
  const rows = [...decisionBlocks, ...staleSources, ...thinRows];
  return rows.length ? rows.slice(0, 6) : [{ label: "Evidence gates", value: "Clear", caption: "No stale or missing evidence gates are visible in loaded rows.", tone: "good" }];
}

function buildAttentionItems(model: AppModel): AttentionItem[] {
  const items: AttentionItem[] = [];
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const largest = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  if (largest && largest.weight >= 35) {
    items.push({
      symbol: largest.ticker,
      label: "Concentration",
      value: `${largest.ticker} ${largest.weight.toFixed(1)}%`,
      detail: "Single-name exposure dominates portfolio risk.",
      action: "Review sizing before adding anything.",
      tone: "warn",
    });
  }

  const largestLoss = model.holdings
    .filter((holding) => holding.hasPnl && holding.unrealizedPnl < 0)
    .slice()
    .sort((a, b) => a.unrealizedPnl - b.unrealizedPnl)[0];
  if (largestLoss) {
    items.push({
      symbol: largestLoss.ticker,
      label: "Loss Review",
      value: `${largestLoss.ticker} ${formatMoney(largestLoss.unrealizedPnl)}`,
      detail: `${formatPct(largestLoss.unrealizedPnlPct)} unrealized P/L.`,
      action: "Check invalidation and trim rules.",
      tone: "bad",
    });
  }

  for (const analysis of model.financeAnalyses.filter((item) => !item.consumable).slice(0, 3)) {
    items.push({
      symbol: analysis.symbol,
      label: "Decision Work",
      value: analysis.symbol,
      detail: financeCaveat(analysis),
      action: analysis.blockers.length ? "Fix the blocker before acting." : "Open dossier only if this name still matters.",
      tone: "warn",
    });
  }

  const ready = model.financeAnalyses.find((analysis) => analysis.consumable);
  if (ready) {
    items.push({
      symbol: ready.symbol,
      label: "Ready Review",
      value: ready.symbol,
      detail: `${ready.actionGrade} setup has enough analysis context.`,
      action: "Open dossier and decide hold/add/pass.",
      tone: "good",
    });
  }

  return dedupeAttentionItems(items).slice(0, 5);
}

function dedupeAttentionItems(items: AttentionItem[]): AttentionItem[] {
  const seen = new Set<string>();
  const output: AttentionItem[] = [];
  for (const item of items) {
    const key = `${item.label}:${item.symbol ?? item.value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(item);
  }
  return output;
}

function AttentionQueue({ items, onOpenTicker }: { items: AttentionItem[]; onOpenTicker: (symbol: string) => void }) {
  if (!items.length) {
    return <EmptyState title="Nothing urgent" detail="No position, risk, or decision item currently requires attention." />;
  }
  return (
    <div className="attention-queue">
      {items.map((item, index) => (
        <button key={`${item.label}-${item.value}-${index}`} type="button" onClick={() => item.symbol && onOpenTicker(item.symbol)} disabled={!item.symbol}>
          <span className={`attention-index ${item.tone}`}>{String(index + 1).padStart(2, "0")}</span>
          <span>
            <b>{item.label}</b>
            <strong>{item.value}</strong>
            <small>{item.detail}</small>
          </span>
          <em>{item.action}</em>
        </button>
      ))}
    </div>
  );
}

function DecisionBrief({ model, readyCount, blockedRows, onOpenTicker }: { model: AppModel; readyCount: number; blockedRows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  const lead = model.signalMatrix.find((row) => row.actionGrade !== "Stale") ?? model.signalMatrix[0];
  const blockers = blockedRows
    .map((row) => [stringField(row, ["symbol"]), stringField(row, ["status"]), stringField(row, ["next_action"]) || listField(row, ["blockers"]).join(" · ")].filter(Boolean).join(" · "))
    .filter(Boolean);
  const loadedFamilies = model.signalCoverage.filter((item) => item.count > 0).length;
  const nextAction = lead?.blockingGates[0] || model.opportunities.find((item) => item.nextAction)?.nextAction || blockers[0] || "Load decision sources before acting.";
  return (
    <section className="decision-brief" aria-label="Decision brief">
      <div className="decision-brief-main">
        <span>Readiness</span>
        <strong>{readyCount ? `${readyCount} ready` : "Blocked"}</strong>
        <p>{nextAction}</p>
        <div className="decision-brief-actions">
          {lead && <button type="button" onClick={() => onOpenTicker(lead.ticker)}>Open {lead.ticker}<ChevronRight size={14} /></button>}
          <small>{loadedFamilies}/11 source families loaded</small>
        </div>
      </div>
      <div className="decision-brief-grid">
        <MetricBadge label="Lead Ticker" value={lead?.ticker ?? "-"} caption={lead?.primaryReason ?? "No ranked source row"} tone={lead ? "info" : "muted"} />
        <MetricBadge label="Blockers" value={String(blockers.length)} caption={blockers[0] ?? "No visible blockers"} tone={blockers.length ? "warn" : "good"} />
        <MetricBadge label="Health" value={model.sources.health === "live" ? "Loaded" : "No rows"} caption={model.latestHealthCheck} tone={model.sources.health === "live" ? "good" : "muted"} />
        <MetricBadge label="Portfolio" value={model.holdings.length ? `${model.holdings.length} rows` : "Empty"} caption={model.holdings.length ? formatMoney(model.portfolioValue) : "No owned exposure"} tone={model.holdings.length ? "info" : "muted"} />
      </div>
    </section>
  );
}

function SignalCommandCenter({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const lead = model.signalMatrix.find((row) => row.actionGrade !== "Stale") ?? model.signalMatrix[0];
  const sourceFamilies = model.signalCoverage.filter((item) => item.count > 0).length;
  const sourceRows = model.signalCoverage.reduce((total, item) => total + item.count, 0);
  const blocked = model.signalMatrix.filter((row) => row.actionGrade === "Stale" || row.blockingGates.length).length;
  const multiSource = model.signalMatrix.filter((row) => row.signals.filter((signal) => signal.count > 0).length >= 5).length;
  if (!lead) {
    return <EmptyState title="No signal cockpit rows" detail="Decision rows and source rows have not loaded into the dashboard model yet." />;
  }
  const liveSignals = lead.signals.filter((signal) => signal.count > 0);
  return (
    <section className="signal-command-center" aria-label="Signal command center">
      <div className="signal-command-head">
        <div>
          <span>Signal Cockpit</span>
          <strong>{lead.ticker}</strong>
          <p>{lead.primaryReason}</p>
        </div>
        <button type="button" onClick={() => onOpenTicker(lead.ticker)}>
          Open Dossier
          <ChevronRight size={14} />
        </button>
      </div>
      <div className="signal-command-metrics">
        <MetricBadge label="Signal Families" value={`${sourceFamilies}/11`} caption={`${sourceRows.toLocaleString()} source rows`} tone="info" />
        <MetricBadge label="Multi-Source Names" value={String(multiSource)} caption="5+ families present" tone={multiSource ? "good" : "warn"} />
        <MetricBadge label="Blocked Names" value={String(blocked)} caption="freshness/gate worklist" tone={blocked ? "warn" : "good"} />
        <MetricBadge label="Lead Breadth" value={`${liveSignals.length}/11`} caption={lead.actionGrade} tone={lead.actionGrade === "Act" ? "good" : lead.actionGrade === "Stale" ? "bad" : "warn"} />
      </div>
      <div className="signal-lead-stack">
        {liveSignals.slice(0, 8).map((signal) => (
          <span key={signal.key} className={`signal-chip ${signal.tone}`}>
            <b>{signal.label}</b>
            <small>{signal.value}</small>
          </span>
        ))}
      </div>
    </section>
  );
}

function SignalCoverageStrip({ coverage }: { coverage: SourceCoverage[] }) {
  const maxCount = Math.max(...coverage.map((item) => item.count), 1);
  return (
    <section className="signal-coverage-strip" aria-label="Signal source coverage">
      {coverage.map((item) => (
        <div key={item.key} className={`coverage-item ${item.tone}`}>
          <div>
            <strong>{item.label}</strong>
            <span>{item.symbolCount} symbols</span>
          </div>
          <i><b style={{ width: `${Math.max(4, (item.count / maxCount) * 100)}%` }} /></i>
          <small>{item.count.toLocaleString()} rows · {item.leaders.join(", ") || "no leaders"}</small>
        </div>
      ))}
    </section>
  );
}

function SignalMatrix({ rows: matrixRows, onOpenTicker }: { rows: SignalMatrixRow[]; onOpenTicker: (symbol: string) => void }) {
  if (!matrixRows.length) {
    return <EmptyState title="No signal matrix rows" detail="No opportunity rows are available to cross-map against source families." />;
  }
  const columns: SignalKey[] = ["quote", "technical", "sepa", "liquidity", "valuation", "earnings", "options", "filings", "thesis", "news", "tradingview"];
  return (
    <section className="signal-matrix-panel" aria-label="Cross-source signal matrix">
      <header>
        <div>
          <h2>Cross-Source Signal Matrix</h2>
          <p>Each row is a ticker; each cell is a loaded signal family from the old and newly codified sources.</p>
        </div>
        <span>{matrixRows.length} names</span>
      </header>
      <div className="signal-matrix-scroll">
        <table className="signal-matrix">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Action</th>
              <th>Score</th>
              {columns.map((key) => <th key={key}>{shortSignalLabel(key)}</th>)}
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {matrixRows.map((row) => (
              <tr key={row.ticker}>
                <td>
                  <button type="button" onClick={() => onOpenTicker(row.ticker)}>
                    <strong>{row.ticker}</strong>
                    <small>{row.sourceCount} src · {row.evidenceCount} ev</small>
                  </button>
                </td>
                <td><DecisionBadge value={row.actionGrade} /></td>
                <td><b>{row.score}</b></td>
                {columns.map((key) => {
                  const signal = row.signals.find((item) => item.key === key);
                  return (
                    <td key={`${row.ticker}-${key}`}>
                      <span className={`matrix-dot ${signal?.count ? signal.tone : "muted"}`} title={signal?.value ?? "No rows"}>
                        {signal?.count ? compactCellCount(signal.count) : "-"}
                      </span>
                    </td>
                  );
                })}
                <td><p>{row.primaryReason}</p></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TickerSignalRibbon({ row, brief }: { row: SignalMatrixRow; brief?: RowRecord }) {
  const optionsExpired = stringField(objectField(brief?.options_context), ["status"]).toLowerCase() === "expired";
  const activeSignalCount = row.signals.filter((signal) => signal.count > 0 && !(optionsExpired && signal.key === "options")).length;
  return (
    <section className="ticker-signal-ribbon" aria-label="Ticker signal stack">
      <div>
        <strong>Signal Stack</strong>
        <span>{activeSignalCount}/11 families · {row.sourceCount} sources</span>
      </div>
      <div>
        {row.signals.map((signal) => {
          const expiredOptionSignal = optionsExpired && signal.key === "options";
          return (
            <span key={signal.key} className={`signal-chip ${expiredOptionSignal ? "bad" : signal.count ? signal.tone : "muted"}`}>
              <b>{signal.label}</b>
              <small>{expiredOptionSignal ? "expired" : signal.count ? signal.value : "none"}</small>
            </span>
          );
        })}
      </div>
    </section>
  );
}

function FinanceAnalysisPanel({ analyses, onOpenTicker }: { analyses: FinanceAnalysis[]; onOpenTicker: (symbol: string) => void }) {
  if (!analyses.length) {
    return <EmptyState title="No finance-skill analysis rows" detail="Valuation, earnings setup, options payoff, and TradingView rows have not loaded into this scope." />;
  }
  const readyCount = analyses.filter((item) => item.consumable).length;
  const partialCount = analyses.filter((item) => item.coverage > 0 && !item.consumable).length;
  const bestUpside = bestFinanceUpside(analyses);
  const firstRisk = analyses.find((item) => item.blockers.length || item.missingFamilies.length);
  return (
    <section className="finance-analysis-panel" aria-label="Finance-skill decision analysis">
      <header>
        <div>
          <h2>Finance-Skill Decision Analysis</h2>
          <p>{readyCount} ready to review · {partialCount} need work</p>
        </div>
        <span>{readyCount ? "review list ready" : "no final call"}</span>
      </header>
      <div className="finance-analysis-ledger" aria-label="Finance analysis readiness summary">
        <MetricBadge label="Ready to Review" value={String(readyCount)} caption="Enough analysis to open the dossier" tone={readyCount ? "good" : "warn"} />
        <MetricBadge label="Needs Work" value={String(partialCount)} caption="Open thesis, options, or setup context" tone={partialCount ? "warn" : "good"} />
        <MetricBadge label="Best Upside" value={bestUpside.value} caption={bestUpside.caption} tone={bestUpside.tone} />
        <MetricBadge label="Main Caveat" value={firstRisk?.symbol ?? "None"} caption={firstRisk ? financeCaveat(firstRisk) : "No visible caveat"} tone={firstRisk ? "warn" : "good"} />
      </div>
      <div className="finance-analysis-grid">
        {analyses.map((analysis) => (
          <FinanceAnalysisCard key={analysis.symbol} analysis={analysis} onOpenTicker={onOpenTicker} />
        ))}
      </div>
    </section>
  );
}

function FinanceAnalysisCard({ analysis, onOpenTicker, expanded = false }: { analysis: FinanceAnalysis; onOpenTicker: (symbol: string) => void; expanded?: boolean }) {
  const items = [analysis.valuation, analysis.earnings, analysis.options, analysis.tradingview];
  return (
    <article className={`finance-analysis-card ${analysis.tone} ${expanded ? "expanded" : ""}`}>
      <button type="button" onClick={() => onOpenTicker(analysis.symbol)} disabled={expanded}>
        <span>
          <strong>{analysis.symbol}</strong>
          <small>{analysis.headline}</small>
        </span>
        <DecisionBadge value={analysis.consumable ? "Ready" : "Needs Work"} />
      </button>
      <div className="finance-analysis-meta">
        <span>{analysis.actionGrade} candidate</span>
        <span>{analysis.coverage}/4 analysis checks</span>
        <span>{financeCaveat(analysis)}</span>
      </div>
      <div className="finance-analysis-items">
        {items.map((item) => (
          <div key={item.label} className={`finance-analysis-item ${item.tone}`}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
            <small>{item.caption}</small>
          </div>
        ))}
      </div>
      <div className="finance-analysis-decision">
        <p>{analysis.decisionText}</p>
        <small>{analysis.blockers.length ? `Gate: ${formatGateLabel(analysis.blockers[0])}` : analysis.nextAction}</small>
      </div>
    </article>
  );
}

function financeCaveat(analysis: FinanceAnalysis): string {
  if (analysis.blockers.length) return formatGateLabel(analysis.blockers[0]);
  if (analysis.missingFamilies.length) return `Needs ${analysis.missingFamilies.join(", ")}`;
  return "No immediate caveat";
}

function bestFinanceUpside(analyses: FinanceAnalysis[]): SummaryItem {
  const ranked = analyses
    .map((analysis) => ({
      symbol: analysis.symbol,
      upside: parsePctLabel(analysis.valuation.value),
    }))
    .filter((item) => Number.isFinite(item.upside))
    .sort((a, b) => b.upside - a.upside);
  const best = ranked[0];
  if (!best) {
    return { label: "Best Upside", value: "-", caption: "No modeled upside", tone: "muted" };
  }
  return {
    label: "Best Upside",
    value: formatPct(best.upside),
    caption: best.symbol,
    tone: best.upside >= 0 ? "good" : "bad",
    symbol: best.symbol,
  };
}

function parsePctLabel(value: string): number {
  const parsed = Number(value.replace(/[+,%]/g, ""));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function medianCoverage(analyses: FinanceAnalysis[]): number {
  if (!analyses.length) return 0;
  const sorted = analyses.map((item) => item.coverage).sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)] ?? 0;
}

function TerminalHoldingsBook({
  holdings,
  opportunities,
  watchlist,
  onOpenTicker,
}: {
  holdings: Holding[];
  opportunities: Opportunity[];
  watchlist: WatchItem[];
  onOpenTicker: (symbol: string) => void;
}) {
  const holdingRows = holdings.map((holding) => {
    const quote = watchlist.find((item) => item.symbol === holding.ticker);
    return {
      symbol: holding.ticker,
      id: holding.taxLotTerm,
      price: quote?.price ?? formatMoney(holding.averageCost),
      change: quote ? formatPct(quote.change) : formatPct(holding.unrealizedPnlPct),
      changeTone: quote?.change ?? holding.unrealizedPnlPct,
      position: holding.weight ? `${holding.weight.toFixed(1)}%` : formatCompactMoney(holding.marketValue),
    };
  });
  const opportunityRows = opportunities.map((opportunity) => {
    const quote = watchlist.find((item) => item.symbol === opportunity.ticker);
    return {
      symbol: opportunity.ticker,
      id: opportunity.category || opportunity.assetClass,
      price: quote?.price ?? opportunity.latestQuote,
      change: quote ? formatPct(quote.change) : `Grade ${opportunity.grade}`,
      changeTone: quote?.change ?? (opportunity.actionGrade === "Act" ? 1 : opportunity.actionGrade === "Reject" || opportunity.actionGrade === "Stale" ? -1 : 0),
      position: `${opportunity.sourceCount} SRC`,
    };
  });
  const tableRows = holdingRows.length ? holdingRows : opportunityRows;
  if (!tableRows.length) {
    return <EmptyState title="No holdings or order rows" detail="Load portfolio rows or decision queue rows to populate the primary book." />;
  }
  return (
    <TableFrame>
      <table className="desk-table terminal-book-table">
        <thead>
          <tr>
            <th>Asset / ID</th>
            <th>7D Trend</th>
            <th>Last Price</th>
            <th>Change</th>
            <th>Position</th>
          </tr>
        </thead>
        <tbody>
          {tableRows.map((row) => (
            <tr key={`terminal-book-${row.symbol}`}>
              <td>
                <button className="ticker-link terminal-asset-cell" type="button" onClick={() => onOpenTicker(row.symbol)}>
                  <strong>{row.symbol}</strong>
                  <small>{titleLabel(row.id || "instrument")}</small>
                </button>
              </td>
              <td><MiniTrend seed={row.symbol} negative={row.changeTone < 0} /></td>
              <td>{row.price || "-"}</td>
              <td className={row.changeTone < 0 ? "negative" : "positive"}>{row.change}</td>
              <td>{row.position}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableFrame>
  );
}

function MiniTrend({ seed, negative }: { seed: string; negative: boolean }) {
  const seedValue = Array.from(seed).reduce((total, letter) => total + letter.charCodeAt(0), 0);
  const points = Array.from({ length: 6 }, (_, index) => {
    const x = index * 16;
    const drift = negative ? 12 + index * 3 : 25 - index * 3;
    const jitter = ((seedValue + index * 11) % 9) - 4;
    const y = Math.max(5, Math.min(33, drift + jitter));
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg className={`mini-trend ${negative ? "negative" : "positive"}`} viewBox="0 0 82 38" aria-hidden="true">
      <polyline points={points} />
    </svg>
  );
}

function AlgorithmicSignalFeed({ opportunities, blockedRows, onOpenTicker }: { opportunities: Opportunity[]; blockedRows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  if (!opportunities.length) {
    return <EmptyState title="No algorithmic signals" detail={blockedRows[0] ? stringField(blockedRows[0], ["next_action"]) || "Decision readiness is blocked." : "Load decision queue rows to populate the signal feed."} />;
  }
  return (
    <div className="terminal-signal-feed">
      {opportunities.map((item, index) => {
        const command = item.actionGrade === "Act" ? "ACCUMULATE" : item.isStale || item.blockingGates.length ? "REDUCE EXPOSURE" : "MONITOR";
        return (
          <article key={`terminal-signal-${item.ticker}-${index}`}>
            <header>
              <span>{terminalTimeLabel(item.asOf)}</span>
              <small>Score {item.score}</small>
            </header>
            <button type="button" onClick={() => onOpenTicker(item.ticker)}>
              <strong title={`${item.ticker}: ${item.inclusionReasons[0] ?? item.decisionBasis}`}>{item.ticker}: {item.inclusionReasons[0] ?? item.decisionBasis}</strong>
              <p>{item.nextAction || item.whyNow}</p>
            </button>
            <footer>
              <DecisionBadge value={command} />
              <small>{item.blockingGates[0] ? `NEEDS: ${formatGateLabel(item.blockingGates[0]).slice(0, 22)}` : item.invalidation ? `RISK: ${item.invalidation.slice(0, 22)}` : "NO EXTRA ACTION"}</small>
            </footer>
          </article>
        );
      })}
    </div>
  );
}

function RiskProfileTerminal({ model, readyCount }: { model: AppModel; readyCount: number }) {
  const loadedFamilies = model.signalCoverage.filter((item) => item.count > 0).length;
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const sortedByWeight = [...pricedHoldings].sort((a, b) => b.weight - a.weight);
  const largest = sortedByWeight[0];
  const largestWeight = largest?.weight ?? 0;
  const topThreeWeight = sortedByWeight.slice(0, 3).reduce((total, holding) => total + holding.weight, 0);
  const staleQuoteCount = model.holdings.filter((holding) => holding.quoteFreshness === "missing" || holding.quoteFreshness === "stale" || !holding.hasMarketValue).length;
  const negativePnl = model.holdings.filter((holding) => holding.hasPnl && holding.unrealizedPnl < 0).reduce((total, holding) => total + holding.unrealizedPnl, 0);
  const consumableAnalyses = model.financeAnalyses.filter((analysis) => analysis.consumable).length;
  const posture = riskPosture(largestWeight, topThreeWeight, staleQuoteCount, readyCount, loadedFamilies);
  const stressRows = riskStressScenarios(model, largest, topThreeWeight);
  const controlRows: SummaryItem[] = [
    {
      label: "Top correlation",
      value: model.correlationRows[0]?.value ?? "No row",
      caption: model.correlationRows[0]?.caption ?? "Correlation analysis missing",
      tone: model.correlationRows.length ? "info" : "muted",
    },
    {
      label: "Losing Lots",
      value: negativePnl ? formatMoney(negativePnl) : "-",
      caption: "Unrealized loss across imported holdings",
      tone: negativePnl < 0 ? "bad" : "muted",
    },
    {
      label: "Largest position",
      value: largest ? `${largest.ticker} ${largestWeight.toFixed(1)}%` : "-",
      caption: largestWeight > 25 ? "Sizing review required before adding" : "Inside single-name guardrail",
      tone: largestWeight > 25 ? "warn" : largestWeight ? "info" : "muted",
      symbol: largest?.ticker,
    },
    {
      label: "Ready reviews",
      value: consumableAnalyses ? `${consumableAnalyses} names` : "None",
      caption: consumableAnalyses ? "Open dossier and decide" : "No name is ready for action",
      tone: consumableAnalyses ? "good" : "warn",
    },
  ];
  return (
    <div className="risk-profile-workbench">
      <div className={`risk-posture ${posture.tone}`}>
        <span>Risk Posture</span>
        <strong>{posture.label}</strong>
        <p>{posture.detail}</p>
      </div>
      <div className="risk-control-grid">
        <MetricBadge label="Top 3 Weight" value={topThreeWeight ? `${topThreeWeight.toFixed(1)}%` : "-"} caption="priced holdings" tone={topThreeWeight > 65 ? "warn" : topThreeWeight ? "info" : "muted"} />
        <MetricBadge label="Quote Coverage" value={String(staleQuoteCount)} caption="positions without current quote coverage" tone={staleQuoteCount ? "warn" : "good"} />
        <MetricBadge label="Liquidity Check" value={model.liquidityRows.length >= pricedHoldings.length && pricedHoldings.length ? "Covered" : "Review"} caption={model.liquidityRows.length >= pricedHoldings.length && pricedHoldings.length ? "Owned names have liquidity context" : "Confirm exit capacity"} tone={model.liquidityRows.length >= pricedHoldings.length && pricedHoldings.length ? "good" : "warn"} />
        <MetricBadge label="Buy Setup" value={readyCount ? `${readyCount} ready` : "Review only"} caption={readyCount ? "There are names ready for decision" : "Hold/trim decisions only"} tone={readyCount ? "good" : "warn"} />
      </div>
      <div className="stress-test-table" aria-label="Portfolio stress scenarios">
        <div className="stress-test-head">
          <span>Scenario</span>
          <span>Shock</span>
          <span>Loss</span>
        </div>
        {stressRows.map((row) => (
          <div key={row.label} className={`stress-test-row ${row.tone}`}>
            <span><strong>{row.label}</strong><small>{row.caption}</small></span>
            <b>{row.shock}</b>
            <strong>{formatMoney(row.loss)}</strong>
          </div>
        ))}
      </div>
      <SummaryList rows={controlRows} />
    </div>
  );
}

function riskPosture(largestWeight: number, topThreeWeight: number, staleQuoteCount: number, readyCount: number, loadedFamilies: number): { label: string; detail: string; tone: Tone } {
  if (staleQuoteCount || loadedFamilies < 6) {
    return {
      label: "Review-only",
      detail: "Use this for sizing or trim work; do not add until quote coverage is current.",
      tone: "warn",
    };
  }
  if (largestWeight > 35 || topThreeWeight > 70) {
    return {
      label: "Concentrated",
      detail: "Position sizing is the dominant risk driver; stress before adding exposure.",
      tone: "warn",
    };
  }
  if (!readyCount) {
    return {
      label: "Trade-gated",
      detail: "Risk profile is usable; new buys remain blocked by decision readiness.",
      tone: "warn",
    };
  }
  return {
    label: "Ready",
    detail: "Portfolio rows, source coverage, and decision gates are sufficient for review.",
    tone: "good",
  };
}

function riskStressScenarios(model: AppModel, largest: Holding | undefined, topThreeWeight: number): StressScenario[] {
  const portfolioValue = model.portfolioValue;
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const topThreeValue = pricedHoldings
    .slice()
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 3)
    .reduce((total, holding) => total + holding.marketValue, 0);
  const uncoveredValue = pricedHoldings
    .filter((holding) => !model.liquidityRows.some((row) => row.symbol === holding.ticker))
    .reduce((total, holding) => total + holding.marketValue, 0);
  const correlationValue = topThreeWeight > 50 ? topThreeValue : portfolioValue * 0.35;
  return [
    {
      label: "Broad tape",
      shock: "-8.0%",
      loss: portfolioValue * -0.08,
      caption: "Priced market value",
      tone: portfolioValue ? "info" : "muted",
    },
    {
      label: "Largest gap",
      shock: largest ? `${largest.ticker} -20.0%` : "-20.0%",
      loss: (largest?.marketValue ?? 0) * -0.2,
      caption: largest ? `${largest.weight.toFixed(1)}% single name` : "No priced holding",
      tone: largest && largest.weight > 25 ? "warn" : largest ? "info" : "muted",
    },
    {
      label: "Corr cluster",
      shock: "-12.0%",
      loss: correlationValue * -0.12,
      caption: topThreeWeight ? `Top cluster ${topThreeWeight.toFixed(1)}%` : "Uses portfolio proxy",
      tone: topThreeWeight > 65 ? "warn" : "info",
    },
    {
      label: "Liquidity",
      shock: "-15.0%",
      loss: uncoveredValue * -0.15,
      caption: uncoveredValue ? "Holdings without liquidity rows" : "Liquidity rows cover priced holdings",
      tone: uncoveredValue ? "warn" : "good",
    },
  ];
}

function RiskRadar({ values }: { values: number[] }) {
  const size = 210;
  const center = size / 2;
  const radius = 78;
  const axes = values.length;
  const points = values.map((value, index) => radarPoint(center, radius * (Math.max(0, Math.min(100, value)) / 100), index, axes)).join(" ");
  const axisPoints = Array.from({ length: axes }, (_, index) => radarPoint(center, radius, index, axes));
  return (
    <svg className="risk-radar" viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      {[0.34, 0.67, 1].map((scale) => (
        <polygon key={scale} points={axisPoints.map((_, index) => radarPoint(center, radius * scale, index, axes)).join(" ")} className="radar-ring" />
      ))}
      {axisPoints.map((point, index) => {
        const [x, y] = point.split(",");
        return <line key={`axis-${index}`} x1={center} y1={center} x2={x} y2={y} className="radar-axis" />;
      })}
      <polygon points={points} className="radar-shape" />
      <circle cx={points.split(" ")[1]?.split(",")[0] ?? center} cy={points.split(" ")[1]?.split(",")[1] ?? center} r="8" className="radar-point" />
    </svg>
  );
}

function radarPoint(center: number, radius: number, index: number, count: number): string {
  const angle = -Math.PI / 2 + (index / count) * Math.PI * 2;
  const x = center + Math.cos(angle) * radius;
  const y = center + Math.sin(angle) * radius;
  return `${x.toFixed(2)},${y.toFixed(2)}`;
}

function terminalTimeLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:-- EST";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" });
}

function WatchlistStrip({ items, onOpenTicker }: { items: WatchItem[]; onOpenTicker: (symbol: string) => void }) {
  if (!items.length) {
    return <EmptyState title="No quote rows" detail="Watchlist cards are withheld until /api/quotes returns rows." />;
  }
  return (
    <section className="watchlist" aria-label="Watchlist">
      {items.map((item) => (
        <button key={item.symbol} type="button" onClick={() => onOpenTicker(item.symbol)}>
          <span>{item.symbol}</span>
          <strong className={item.change >= 0 ? "positive" : "negative"}>{formatPct(item.change)}</strong>
          <small>{item.price}</small>
        </button>
      ))}
    </section>
  );
}

function DecisionQueueBoard({ queues, onOpenTicker }: { queues: Record<DecisionBucket, Opportunity[]>; onOpenTicker: (symbol: string) => void }) {
  const order: DecisionBucket[] = ["Act", "Research", "Watch", "Reject", "Stale"];
  return (
    <section className="decision-queue-board" aria-label="Decision queue">
      {order.map((bucket) => (
        <div key={bucket} className={`decision-bucket ${bucket.toLowerCase()}`}>
          <header>
            <strong>{bucket}</strong>
            <span>{queues[bucket].length}</span>
          </header>
          <div>
            {queues[bucket].slice(0, 4).map((item) => (
              <DecisionTickerCard key={`${bucket}-${item.ticker}-${item.rank}`} item={item} onOpenTicker={onOpenTicker} compact />
            ))}
            {!queues[bucket].length && <EmptyState title={`No ${bucket.toLowerCase()} names`} detail="No rows currently map to this queue bucket." />}
          </div>
        </div>
      ))}
    </section>
  );
}

function DecisionTickerCard({ item, onOpenTicker, compact = false }: { item: Opportunity; onOpenTicker: (symbol: string) => void; compact?: boolean }) {
  return (
    <button className={`decision-card ${item.isStale ? "stale" : ""}`} type="button" onClick={() => onOpenTicker(item.ticker)}>
      <div className="decision-card-head">
        <strong>{item.ticker}</strong>
        <span>{item.score}</span>
      </div>
      <div className="decision-card-meta">
        <FreshnessBadge status={item.freshnessStatus} />
        <small>{item.evidenceCount} ev · {item.sourceCount} src</small>
      </div>
      <p>{item.inclusionReasons[0] ?? item.whyNow}</p>
      {!compact && <DecisionFactGrid item={item} />}
      {(item.isStale || item.isSourceThin || item.blockingGates.length > 0) && (
        <div className="decision-card-warning">
          {item.isStale && <span>Stale</span>}
          {item.isSourceThin && <span>Source-thin</span>}
          {item.blockingGates.slice(0, 1).map((gate) => <span key={gate}>{gate}</span>)}
        </div>
      )}
    </button>
  );
}

function DecisionFactGrid({ item }: { item: Opportunity }) {
  return (
    <dl className="decision-facts">
      <div><dt>Quote</dt><dd>{item.latestQuote}</dd></div>
      <div><dt>Catalyst</dt><dd>{item.catalystWindow}</dd></div>
      <div><dt>Liquidity</dt><dd>{item.liquidity}</dd></div>
      <div><dt>Portfolio</dt><dd>{item.portfolioImpact}</dd></div>
    </dl>
  );
}

function TopOpportunityTicker({ opportunity, onOpenTicker }: { opportunity: Opportunity; onOpenTicker: (symbol: string) => void }) {
  return (
    <section className="top-opportunity">
      <button type="button" onClick={() => onOpenTicker(opportunity.ticker)}>
        <span>Top Ranked Ticker</span>
        <strong>{opportunity.ticker}</strong>
        <small>{opportunity.name}</small>
      </button>
      <div className="top-opportunity-score">
        <MetricBadge label="Composite" value={`${opportunity.score}`} caption={opportunity.grade} tone="good" />
        <MetricBadge label="Action" value={opportunity.actionGrade} tone={opportunity.actionGrade === "Act" ? "good" : opportunity.actionGrade === "Stale" || opportunity.actionGrade === "Reject" ? "bad" : "warn"} />
        <MetricBadge label="Freshness" value={titleLabel(opportunity.freshnessStatus)} tone={freshnessTone(opportunity.freshnessStatus)} />
      </div>
      <div className="top-opportunity-copy">
        <p>{opportunity.inclusionReasons[0] ?? opportunity.whyNow}</p>
        <small>{opportunity.isStale || opportunity.isSourceThin ? warningText(opportunity) : opportunity.nextAction}</small>
      </div>
    </section>
  );
}

function TickerDossierHeader({
  symbol,
  decisionBrief,
  quote,
  opportunity,
  evidenceRows,
  foundTables,
  tickerFound,
}: {
  symbol: string;
  decisionBrief: RowRecord;
  quote?: WatchItem;
  opportunity?: Opportunity;
  evidenceRows: number;
  foundTables: string[];
  tickerFound: boolean;
}) {
  const verdict = objectField(decisionBrief.verdict);
  const blockers = listField(verdict, ["blockers"]);
  const blockerLabels = listField(verdict, ["blocker_labels"]);
  const canonicalQuote = objectField(decisionBrief.canonical_quote);
  const price = numberField(canonicalQuote, ["price"], Number.NaN);
  const change = numberField(canonicalQuote, ["change_pct"], Number.NaN);
  const action = stringField(verdict, ["action"]) || opportunity?.actionGrade || "-";
  const freshness = stringField(verdict, ["freshness"]) || (opportunity ? titleLabel(opportunity.freshnessStatus) : "-");
  const confidence = numberField(verdict, ["confidence"], Number.NaN);
  return (
    <section className="ticker-dossier-header" aria-label={`${symbol} ticker dossier summary`}>
      <div className="ticker-dossier-price">
        <span>{tickerFound ? stringField(canonicalQuote, ["label"]) || "Canonical quote" : "Ticker API empty"}</span>
        <strong>{Number.isFinite(price) ? formatRawPrice(price) : quote?.price ?? "-"}</strong>
        <p>
          {Number.isFinite(change) ? <b className={change >= 0 ? "positive" : "negative"}>{formatPct(change)}</b> : quote ? <b className={quote.change >= 0 ? "positive" : "negative"}>{formatPct(quote.change)}</b> : <b className="muted">No quote row</b>}
          <small>{canonicalQuoteCaption(decisionBrief) || opportunity?.asOf || "No signal freshness"}</small>
        </p>
      </div>
      <div className="ticker-dossier-context">
        <MetricBadge label="Verdict" value={action} tone={action === "Act" ? "good" : action === "Stale" || action === "Reject" ? "bad" : action === "Watch" ? "warn" : "info"} />
        <MetricBadge label="Freshness" value={titleLabel(freshness)} tone={freshnessTone(normalizeFreshnessStatus(freshness))} />
        <MetricBadge label="Confidence" value={Number.isFinite(confidence) ? `${Math.round(confidence)}` : "-"} caption="decision score" tone={Number.isFinite(confidence) && confidence >= 70 ? "good" : "warn"} />
        <MetricBadge label="Blockers" value={String(blockers.length)} caption={blockers.includes("decision_reject") ? "No new exposure" : (blockerLabels[0] || blockers[0] || "none").slice(0, 44)} tone={blockers.length ? "warn" : "good"} />
        <MetricBadge label="Evidence" value={String(opportunity?.evidenceCount ?? 0)} caption={`${opportunity?.sourceCount ?? 0} sources`} tone={(opportunity?.evidenceCount ?? 0) > 0 ? "good" : "warn"} />
        <MetricBadge label="API Tables" value={String(evidenceRows)} caption={foundTables.slice(0, 2).join(", ") || "No rows"} tone={evidenceRows ? "info" : "muted"} />
      </div>
      <p className="ticker-dossier-note">{stringField(verdict, ["summary"]) || opportunity?.decisionBasis || "Ticker context is derived from ticker-specific API tables for deep-link correctness."}</p>
      <p className="ticker-dossier-note next-action"><b>Next action:</b> {stringField(verdict, ["next_action"]) || opportunity?.nextAction || "Review ticker rows before action."}</p>
    </section>
  );
}

function DecisionTicket({ brief, opportunity }: { brief: RowRecord; opportunity?: Opportunity }) {
  const verdict = objectField(brief.verdict);
  const setup = objectField(brief.setup);
  const risk = objectField(brief.risk_plan);
  const options = objectField(brief.options_context);
  const tasks = arrayField(verdict.blocker_tasks).filter((item): item is RowRecord => Boolean(item && typeof item === "object" && !Array.isArray(item)));
  const blockers = listField(verdict, ["blocker_labels", "blockers"]);
  const action = stringField(verdict, ["action"]) || opportunity?.actionGrade || "Watch";
  const noTrade = blockers.length > 0 || action.toLowerCase().includes("reject") || stringField(options, ["status"]).toLowerCase() === "expired";
  const deskCall = noTrade ? "No Trade" : action.toLowerCase().includes("act") ? "Trade Candidate" : "Watch";
  return (
    <section className={`decision-ticket ${noTrade ? "blocked" : "open"}`} aria-label="Ticker decision ticket">
      <div className="decision-ticket-call">
        <span>Desk Call</span>
        <strong>{deskCall}</strong>
        <small>{noTrade ? "Do not initiate until blockers clear" : "Review sizing and execution before action"}</small>
      </div>
      <div className="decision-ticket-grid">
        <div>
          <span>Direction</span>
          <strong>{noTrade ? "No new exposure" : stringField(setup, ["stance"]) || "Undefined"}</strong>
        </div>
        <div>
          <span>Entry</span>
          <strong>{stringField(setup, ["entry_zone"]) || "-"}</strong>
        </div>
        <div>
          <span>Stop / Invalidation</span>
          <strong>{stringField(setup, ["invalidation_level"]) || stringField(risk, ["invalidation"]) || "-"}</strong>
        </div>
        <div>
          <span>Target / Edge</span>
          <strong>{stringField(setup, ["target_range"]) || "-"}</strong>
        </div>
        <div>
          <span>Max Loss</span>
          <strong>{stringField(risk, ["max_loss"]) || "-"}</strong>
        </div>
        <div>
          <span>Next Review</span>
          <strong>{stringField(setup, ["review_date"]) || "-"}</strong>
        </div>
      </div>
      <div className="blocker-task-list">
        {(tasks.length ? tasks : blockers.map((label, index) => ({ key: `blocker-${index}`, label, action: "Review blocker", detail: label, severity: "warn" }))).slice(0, 6).map((task) => (
          <div key={stringField(task, ["key"]) || stringField(task, ["label"])} className={`blocker-task ${stringField(task, ["severity"]) || "warn"}`}>
            <span>{stringField(task, ["label"]) || "Decision blocker"}</span>
            <strong>{stringField(task, ["action"]) || "Review"}</strong>
            <small>{stringField(task, ["detail"])}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function DecisionBriefOverview({ brief, opportunity }: { brief: RowRecord; opportunity?: Opportunity }) {
  const verdict = objectField(brief.verdict);
  const blockers = listField(verdict, ["blockers"]);
  const blockerLabels = listField(verdict, ["blocker_labels"]);
  return (
    <Panel className="span-12 decision-brief-panel" title="Decision Brief">
      <div className="decision-brief-layout">
        <div>
          <span>Trader Stance</span>
          <strong>{stringField(objectField(brief.setup), ["stance"]) || opportunity?.decisionBasis || "No structured stance loaded."}</strong>
          <p>{stringField(verdict, ["summary"]) || "No source-backed decision summary is loaded."}</p>
        </div>
        <div className="decision-brief-actions">
          <MetricBadge label="Verdict" value={stringField(verdict, ["action"]) || opportunity?.actionGrade || "-"} tone={opportunity?.actionGrade === "Act" ? "good" : blockers.length ? "warn" : "info"} />
          <MetricBadge label="Confidence" value={displayValue(verdict.confidence as JsonValue)} caption="0-100 decision score" tone={numberField(verdict, ["confidence"], 0) >= 70 ? "good" : "warn"} />
          <MetricBadge label="Next Action" value={shortActionLabel(stringField(verdict, ["next_action"]))} caption={(blockerLabels[0] || blockers.slice(0, 1).map(shortGateLabel).join(", ")) || "no blocker"} tone={blockers.length ? "warn" : "info"} />
        </div>
      </div>
      <EvidenceTriad brief={brief} compact />
      <ChangedSinceReview brief={brief} />
    </Panel>
  );
}

function ThesisStatePanel({ row }: { row?: RowRecord }) {
  if (!row) {
    return (
      <Panel className="span-12" title="Thesis State">
        <EmptyState title="No structured thesis state" detail="This ticker has no thesis monitor row yet." />
      </Panel>
    );
  }
  const flags = listField(row, ["contradiction_flags"]);
  const stale = booleanField(row, ["stale_thesis"]);
  const needsReview = booleanField(row, ["needs_review"]);
  const links = listField(row, ["evidence_links"]);
  const latestPrice = numberField(row, ["latest_price"], Number.NaN);
  const invalidationPrice = numberField(row, ["invalidation_price"], Number.NaN);
  const distance = numberField(row, ["invalidation_distance_pct"], Number.NaN);
  return (
    <Panel className="span-12" title="Thesis State">
      <SummaryList rows={[
        { label: "Status", value: titleLabel(stringField(row, ["status"]) || "monitor"), caption: needsReview ? stringField(row, ["review_reason"]) : "Auditable decision record", tone: needsReview ? "warn" : "good" },
        { label: "Last Reviewed", value: stringField(row, ["last_reviewed"]) ? formatDateLabel(stringField(row, ["last_reviewed"])) : "-", caption: stale ? stringField(row, ["stale_reason"]) || "stale" : "current", tone: stale ? "warn" : "good" },
        { label: "Invalidation", value: Number.isFinite(invalidationPrice) ? formatMoney(invalidationPrice) : "Text", caption: stringField(row, ["invalidation"]) || "No invalidation text", tone: flags.includes("invalidation_breached") ? "bad" : flags.includes("invalidation_near") ? "warn" : "info" },
        { label: "Distance", value: Number.isFinite(distance) ? formatPct(distance) : "-", caption: Number.isFinite(latestPrice) ? `latest ${formatMoney(latestPrice)}` : "no latest price", tone: flags.includes("invalidation_breached") ? "bad" : flags.includes("invalidation_near") ? "warn" : "muted" },
        { label: "Evidence", value: String(links.length), caption: links.slice(0, 1).join("") || "No linked evidence", tone: links.length ? "info" : "warn" },
      ]} />
      <DetailRows rows={[
        ["Thesis", stringField(row, ["thesis"]) || "-"],
        ["Why Owned/Watched", stringField(row, ["why_owned_watched"]) || "-"],
        ["Review Flags", flags.length ? flags.map(titleLabel).join(", ") : "None"],
      ]} />
    </Panel>
  );
}

function TradeSetupPanel({ brief }: { brief: RowRecord }) {
  const setup = objectField(brief.setup);
  return (
    <Panel className="span-12 trade-setup-panel" title="Trade Setup">
      <SummaryList rows={[
        { label: "Stance", value: stringField(setup, ["stance"]) || "-", caption: "advisory only", tone: "info" },
        { label: "Timeframe", value: stringField(setup, ["timeframe"]) || "-", caption: "review horizon", tone: "info" },
        { label: "Catalyst", value: stringField(setup, ["catalyst"]) || "-", caption: "why this could move", tone: "info" },
        { label: "Entry Zone", value: stringField(setup, ["entry_zone"]) || "-", caption: "avoid undefined entries", tone: "warn" },
        { label: "Invalidation", value: stringField(setup, ["invalidation_level"]) || "-", caption: "setup breaks if violated", tone: "bad" },
        { label: "Target", value: stringField(setup, ["target_range"]) || "-", caption: "valuation or setup target", tone: "info" },
        { label: "Risk/Reward", value: stringField(setup, ["risk_reward"]) || "-", caption: "rough source-derived estimate", tone: stringField(setup, ["risk_reward"]).startsWith("-") ? "bad" : "info" },
        { label: "Review", value: stringField(setup, ["review_date"]) || "-", caption: "next required look", tone: "info" },
      ]} />
    </Panel>
  );
}

function RiskPlanPanel({ brief }: { brief: RowRecord }) {
  const risk = objectField(brief.risk_plan);
  return (
    <Panel className="span-4" title="Risk Plan">
      <SummaryList rows={[
        { label: "Max Sizing", value: stringField(risk, ["max_sizing"]) || "-", caption: "portfolio-aware sizing guidance", tone: "warn" },
        { label: "Max Loss", value: stringField(risk, ["max_loss"]) || "-", caption: "selected options scenario", tone: "bad" },
        { label: "Liquidity", value: stringField(risk, ["liquidity_ceiling"]) || "-", caption: "capacity and impact", tone: "info" },
        { label: "Invalidation", value: stringField(risk, ["invalidation"]) || "-", caption: "do not ignore", tone: "bad" },
      ]} />
    </Panel>
  );
}

function PortfolioFitSummary({ brief }: { brief: RowRecord }) {
  const fit = objectField(brief.portfolio_fit);
  return (
    <SummaryList rows={[
      { label: "Exposure", value: stringField(fit, ["current_exposure"]) || "-", caption: fit.owned ? "already owned" : "unowned", tone: fit.owned ? "warn" : "muted" },
      { label: "Concentration", value: stringField(fit, ["theme_concentration"]) || "-", caption: "theme overlap", tone: "info" },
      { label: "Duplicate Risk", value: fit.duplicates_risk ? "Yes" : "No", caption: "existing position check", tone: fit.duplicates_risk ? "warn" : "good" },
    ]} />
  );
}

function ChartContextSummary({ brief }: { brief: RowRecord }) {
  const chart = objectField(brief.chart_context);
  return (
    <SummaryList rows={[
      { label: "20d / 50d / 200d", value: [chart.ma20, chart.ma50, chart.ma200].map((value) => formatMoney(numberField({ value }, ["value"], Number.NaN))).join(" / "), caption: "moving averages", tone: "info" },
      { label: "20d / 60d Return", value: `${formatPct(numberField(chart, ["return_20d"], 0) * 100)} / ${formatPct(numberField(chart, ["return_60d"], 0) * 100)}`, caption: stringField(chart, ["extension_warning"]) || "trend context", tone: stringField(chart, ["extension_warning"]) ? "warn" : "info" },
      { label: "High / Low", value: `${formatMoney(numberField(chart, ["high_52w"], Number.NaN))} / ${formatMoney(numberField(chart, ["low_52w"], Number.NaN))}`, caption: "52-week range", tone: "info" },
      { label: "Support / Resistance", value: `${formatMoney(numberField(chart, ["support"], Number.NaN))} / ${formatMoney(numberField(chart, ["resistance"], Number.NaN))}`, caption: "rough levels from source rows", tone: "info" },
    ]} />
  );
}

function OptionsContextSummary({ brief }: { brief: RowRecord }) {
  const options = objectField(brief.options_context);
  const verdict = objectField(brief.verdict);
  const noTrade = listField(verdict, ["blockers"]).length > 0;
  const decisionReject = listField(verdict, ["blockers"]).includes("decision_reject");
  const status = stringField(options, ["status"]);
  const expired = status.toLowerCase() === "expired";
  return (
    <SummaryList rows={[
      { label: "Status", value: noTrade ? "No Trade" : status ? titleLabel(status) : "Not loaded", caption: noTrade ? `${numberField(options, ["live_scenario_count"], 0)} live scenarios; ${decisionReject ? "decision is Reject" : "blocker active"}` : `${numberField(options, ["live_scenario_count"], 0)} live / ${numberField(options, ["expired_scenario_count"], 0)} expired`, tone: noTrade || expired ? "bad" : status === "live" ? "good" : "muted" },
      { label: "Scenario", value: noTrade ? "Evidence only while blocked" : stringField(options, ["summary"]) || "No options context", caption: `${numberField(options, ["scenario_count"], 0)} scenarios`, tone: noTrade || expired ? "bad" : numberField(options, ["scenario_count"], 0) ? "info" : "muted" },
      { label: "IV / DTE", value: expired || noTrade ? "-" : `${formatUnsignedPct(numberField(options, ["iv"], 0) * 100)} / ${Math.round(numberField(options, ["dte"], 0))}`, caption: expired ? "expired option data hidden from live setup" : noTrade ? "chain data is evidence only" : "implied vol and days to expiry", tone: expired || noTrade ? "bad" : "info" },
      { label: "Breakeven", value: expired || noTrade ? "-" : formatMoney(numberField(options, ["breakeven"], Number.NaN)), caption: expired ? "refresh options before using breakeven" : noTrade ? "breakeven hidden for no-trade state" : stringField(options, ["max_loss"]) ? `max loss ${stringField(options, ["max_loss"])}` : "no bounded loss row", tone: noTrade || expired ? "bad" : "warn" },
    ]} />
  );
}

function EvidenceTriad({ brief, compact = false }: { brief: RowRecord; compact?: boolean }) {
  return (
    <div className={`evidence-triad ${compact ? "compact" : ""}`}>
      <EvidenceColumn title="For" tone="good" items={listField(brief, ["evidence_for"])} />
      <EvidenceColumn title="Against" tone="bad" items={listField(brief, ["evidence_against"])} />
      <EvidenceColumn title="Open Inputs" tone="warn" items={listField(brief, ["unknowns"])} />
    </div>
  );
}

function EvidenceColumn({ title, tone, items }: { title: string; tone: Tone; items: string[] }) {
  return (
    <section className={`evidence-column ${tone}`}>
      <h3>{title}</h3>
      <BulletList tone={tone} items={items.length ? items.slice(0, 6) : ["No rows loaded."]} />
    </section>
  );
}

function ChangedSinceReview({ brief }: { brief: RowRecord }) {
  const items = listField(brief, ["changed_since_last_review"]);
  return (
    <div className="changed-review">
      <strong>What Changed</strong>
      <BulletList tone="info" items={items.length ? items.slice(0, 6) : ["No change summary is available."]} />
    </div>
  );
}

function tabSummaryRows(brief: RowRecord, activeTab: string): SummaryItem[] {
  const summaries = objectField(brief.tab_summaries);
  const rawRows = Array.isArray(summaries[activeTab]) ? summaries[activeTab] as RowRecord[] : [];
  return rawRows.map((row) => ({
    label: stringField(row, ["label"]) || "Summary",
    value: stringField(row, ["value"]) || "-",
    caption: stringField(row, ["caption"]),
    tone: summaryTone(row),
  }));
}

function summaryTone(row: RowRecord): Tone {
  const value = stringField(row, ["value"]).toLowerCase();
  if (value.includes("missing") || value.includes("blocked") || value.includes("reject")) return "warn";
  if (value.includes("-")) return "bad";
  if (value.includes("loaded") || value.includes("constructive") || value.includes("embedded")) return "good";
  return "info";
}

function canonicalQuoteLabel(brief: RowRecord): string {
  const quote = objectField(brief.canonical_quote);
  const price = numberField(quote, ["price"], Number.NaN);
  return Number.isFinite(price) ? formatRawPrice(price) : "";
}

function canonicalQuoteCaption(brief: RowRecord): string {
  const quote = objectField(brief.canonical_quote);
  return [stringField(quote, ["label"]), stringField(quote, ["source"]), stringField(quote, ["observed_at"])].filter(Boolean).join(" · ");
}

function shortActionLabel(value: string): string {
  const normalized = value.toLowerCase();
  if (!value) return "Review";
  if (normalized.includes("thesis")) return "Research thesis";
  if (normalized.includes("refresh")) return "Refresh data";
  if (normalized.includes("clear")) return "Clear gates";
  if (normalized.includes("monitor")) return "Monitor";
  return value.length > 22 ? `${value.slice(0, 19)}...` : value;
}

function shortGateLabel(value: string): string {
  if (!value) return "";
  return titleLabel(value.replaceAll("_", " ")).slice(0, 28);
}

function DashboardQueueTable({ rows, onOpenTicker }: { rows: Opportunity[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No actionable queue rows" detail="Decision queue rows have not loaded yet." />;
  }
  return (
    <TableFrame>
      <table className="desk-table dashboard-queue-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Ticker</th>
            <th>Score</th>
            <th>Grade</th>
            <th>Decision</th>
            <th>Why Now</th>
            <th>Next Action</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`dashboard-${row.rank}-${row.ticker}`}>
              <td className="rank">{row.rank}</td>
              <td><button className="ticker-link" type="button" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}</button></td>
              <td>{row.score}</td>
              <td>{row.grade}</td>
              <td><DecisionBadge value={row.actionGrade} /></td>
              <td className="clip">{row.inclusionReasons[0] ?? row.whyNow}</td>
              <td className="clip">{row.nextAction}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableFrame>
  );
}

function OpportunityTable({ rows, compact = false, onOpenTicker }: { rows: Opportunity[]; compact?: boolean; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No opportunities" detail="No signal or candidate rows matched this view." />;
  }
  return (
    <TableFrame>
      <table className={`desk-table opportunity-table ${compact ? "compact" : ""}`}>
        <thead>
          <tr>
            <th>{compact ? "#" : "Rank"}</th>
            <th>Ticker</th>
            <th>Score</th>
            <th>Action</th>
            <th>Fresh</th>
            <th>Evidence</th>
            <th>Quote</th>
            <th>Why This Is Here</th>
            <th>Gates</th>
            {!compact && <th>Freshness</th>}
            {!compact && <th>Context</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.rank}-${row.ticker}`}>
              <td className="rank">{row.rank}</td>
              <td><button className="ticker-link" type="button" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}</button></td>
              <td>{row.score}</td>
              <td><DecisionBadge value={row.actionGrade} /></td>
              <td><FreshnessBadge status={row.freshnessStatus} /></td>
              <td>{row.evidenceCount} / {row.sourceCount}</td>
              <td>{row.latestQuote}</td>
              <td className="clip">{row.inclusionReasons.join(" · ")}</td>
              <td className="clip">{row.blockingGates.length ? row.blockingGates.join(" · ") : row.isSourceThin ? "Source-thin" : "-"}</td>
              {!compact && <td>{row.freshness}</td>}
              {!compact && <td><DecisionFactGrid item={row} /></td>}
            </tr>
          ))}
        </tbody>
      </table>
    </TableFrame>
  );
}

function TraderPortfolioHero({ portfolio }: { portfolio: TraderPortfolio }) {
  const hasCostBasis = portfolio.estimatedInvested > 0;
  const lastMove = traderLastMoveSummary(portfolio);
  return (
    <section className="trader-hero">
      <div className="trader-hero-main">
        <span>{portfolio.category}</span>
        <h2>{portfolio.investor}</h2>
        <p>{portfolio.description}</p>
        <div className={`trader-last-move ${lastMove.tone}`}>
          <strong>Last move</strong>
          <span>{lastMove.headline}</span>
          <small>{lastMove.detail}</small>
        </div>
        <small>Updated {formatDateLabel(portfolio.updated)}</small>
      </div>
      <div className="trader-hero-metrics">
        <MetricBadge label={hasCostBasis ? "Current Value" : "Reported Value"} value={formatMoney(portfolio.totalValue)} caption={hasCostBasis ? `${formatMoney(portfolio.estimatedInvested)} cost basis` : "latest filing"} tone="info" />
        <MetricBadge label={hasCostBasis ? "Open-Lot Return" : "Performance"} value={hasCostBasis ? formatPct(portfolio.performance) : "N/A"} caption={hasCostBasis ? "current holdings" : "not reconstructed"} tone={hasCostBasis ? (portfolio.performance >= 0 ? "good" : "bad") : "muted"} />
        <MetricBadge label="Holdings" value={String(portfolio.holdingsCount)} caption={`${portfolio.riskLevel} risk`} />
        <MetricBadge label="Next Filing" value={portfolio.nextFilingDueDate ? formatDateLabel(portfolio.nextFilingDueDate) : "N/A"} caption="expected due date" tone="info" />
        <MetricBadge label="Largest Weight" value={`${Math.round(portfolio.holdings[0]?.weight ?? 0)}%`} caption={portfolio.holdings[0]?.label ?? "No holdings"} tone="warn" />
      </div>
      <div className="trader-sector-row">
        <strong>{portfolio.diversificationScore}/100</strong>
        <span>Diversification</span>
        {portfolio.topSectors.map((sector) => <i key={sector}>{sector}</i>)}
      </div>
    </section>
  );
}

function TraderPortfolioRow({ portfolio, active, onSelect }: { portfolio: TraderPortfolio; active: boolean; onSelect: () => void }) {
  const hasCostBasis = portfolio.estimatedInvested > 0;
  const lastMove = traderLastMoveSummary(portfolio);
  return (
    <button type="button" className={`trader-directory-row ${active ? "active" : ""}`} onClick={onSelect}>
      <div>
        <strong>{portfolio.investor}</strong>
        <small>{portfolio.holdingsCount} holdings</small>
        <small className={`trader-directory-move ${lastMove.tone}`}>{lastMove.headline}: {lastMove.detail}</small>
      </div>
      <div>
        <i className={hasCostBasis ? (portfolio.performance >= 0 ? "good" : "bad") : "muted"}>{hasCostBasis ? formatPct(portfolio.performance) : "N/A"}</i>
        <small>{formatCompactMoney(portfolio.totalValue)}</small>
      </div>
    </button>
  );
}

function traderLastMoveSummary(portfolio: TraderPortfolio): { headline: string; detail: string; tone: "good" | "bad" | "info" | "muted" } {
  const datedTransactions = portfolio.transactions
    .filter((transaction) => transaction.date || transaction.filedDate)
    .sort((a, b) => transactionTime(b) - transactionTime(a));
  const latest = datedTransactions[0];
  if (!latest) {
    const largest = portfolio.holdings[0];
    return {
      headline: "Latest filing",
      detail: largest ? `Top weight ${largest.label} at ${largest.weight.toFixed(1)}%` : "No allocation history loaded",
      tone: largest ? "info" : "muted",
    };
  }

  const latestKey = transactionDateKey(latest);
  const sameMove = datedTransactions
    .filter((transaction) => transactionDateKey(transaction) === latestKey)
    .filter((transaction) => !transaction.type.includes("UNCHANGED"));
  const moveRows = sameMove.length ? sameMove : datedTransactions.filter((transaction) => transactionDateKey(transaction) === latestKey);
  const buys = moveRows.filter((transaction) => isBuyLikeMove(transaction.type)).length;
  const sells = moveRows.filter((transaction) => isSellLikeMove(transaction.type)).length;
  const headlineAction = buys && sells ? "Rebalanced" : buys ? "Added" : sells ? "Trimmed" : "Held";
  const ranked = [...moveRows].sort((a, b) => transactionMoveMagnitude(b) - transactionMoveMagnitude(a));
  const detail = ranked.slice(0, 2).map(transactionMoveLabel).filter(Boolean).join(", ");
  const overflow = ranked.length > 2 ? ` +${ranked.length - 2}` : "";
  return {
    headline: `${headlineAction} ${formatDateLabel(latest.date || latest.filedDate)}`,
    detail: `${detail || `${portfolio.holdingsCount} reported holdings`}${overflow}`,
    tone: buys && !sells ? "good" : sells && !buys ? "bad" : headlineAction === "Held" ? "muted" : "info",
  };
}

function transactionTime(transaction: TraderPortfolioTransaction): number {
  const time = new Date(transaction.date || transaction.filedDate).getTime();
  return Number.isFinite(time) ? time : 0;
}

function transactionDateKey(transaction: TraderPortfolioTransaction): string {
  return (transaction.date || transaction.filedDate || "").slice(0, 10);
}

function isBuyLikeMove(type: string): boolean {
  return /ADD|BUY|INCREASE|NEW|OPEN/.test(type);
}

function isSellLikeMove(type: string): boolean {
  return /SELL|DECREASE|REDUCE|TRIM|EXIT|CLOSE/.test(type);
}

function transactionMoveMagnitude(transaction: TraderPortfolioTransaction): number {
  const delta = Math.abs(transaction.weightAfter - transaction.weightBefore);
  return delta || Math.abs(transaction.estimatedAmount);
}

function transactionMoveLabel(transaction: TraderPortfolioTransaction): string {
  const symbol = transaction.symbol || "Holding";
  const delta = transaction.weightAfter - transaction.weightBefore;
  if ((transaction.type.includes("ADD") || transaction.type.includes("NEW")) && transaction.weightBefore === 0 && transaction.weightAfter > 0) {
    return `${symbol} ${transaction.weightAfter.toFixed(1)}%`;
  }
  if (Math.abs(delta) >= 0.05) {
    return `${symbol} ${delta >= 0 ? "+" : ""}${delta.toFixed(1)} pts`;
  }
  if (transaction.estimatedAmount) {
    return `${symbol} ${formatCompactMoney(transaction.estimatedAmount)}`;
  }
  return symbol;
}

function TraderPerformanceChart({ portfolio }: { portfolio: TraderPortfolio }) {
  const [windowKey, setWindowKey] = useState<"1Y" | "3Y" | "5Y" | "ALL">("3Y");
  const hasCostBasis = portfolio.estimatedInvested > 0;
  const sourcePoints = portfolio.history.length ? portfolio.history : [{ date: portfolio.updated, value: portfolio.totalValue, costBasis: portfolio.estimatedInvested, performance: portfolio.performance }];
  const chartData = filterHistoryWindow(sourcePoints, windowKey).map((point) => ({
    date: point.date,
    performance: Number(point.performance.toFixed(2)),
    value: point.value,
    costBasis: point.costBasis,
    holdingsCount: point.holdingsCount,
  }));
  const last = chartData[chartData.length - 1];
  return (
    <div className="trader-performance">
      <div className="chart-toolbar">
        <div>
          <strong>{hasCostBasis ? formatPct(portfolio.performance) : formatMoney(last?.value ?? portfolio.totalValue)}</strong>
          <span>{hasCostBasis ? "Open-lot return on current reconstructed holdings" : `${chartData.length} reported filing snapshots`}</span>
        </div>
        <SegmentedControl
          options={["1Y", "3Y", "5Y", "ALL"]}
          value={windowKey}
          onChange={(value) => setWindowKey(value as "1Y" | "3Y" | "5Y" | "ALL")}
        />
      </div>
      <div className="performance-chart recharts-panel" aria-label="Portfolio performance over time">
        <ResponsiveContainer width="100%" height={320}>
          <LineChart data={chartData} margin={{ top: 12, right: 18, left: 6, bottom: 10 }}>
            <CartesianGrid stroke="#172434" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="date"
              minTickGap={28}
              tick={{ fill: "#8b99a8", fontSize: 11 }}
              tickFormatter={shortDateLabel}
              axisLine={{ stroke: "#223141" }}
              tickLine={{ stroke: "#223141" }}
            />
            <YAxis
              tick={{ fill: "#8b99a8", fontSize: 11 }}
              tickFormatter={(value) => hasCostBasis ? `${Number(value).toFixed(0)}%` : formatCompactMoney(Number(value))}
              width={52}
              domain={hasCostBasis ? ["dataMin - 5", "dataMax + 5"] : ["dataMin", "dataMax"]}
              axisLine={{ stroke: "#223141" }}
              tickLine={{ stroke: "#223141" }}
            />
            <Tooltip
              cursor={{ stroke: "#68a8ff", strokeDasharray: "4 4" }}
              contentStyle={{ background: "#ffffff", border: "1px solid #e5e7eb", borderRadius: 10, color: "#111827", boxShadow: "0 12px 30px rgba(15, 23, 42, 0.12)" }}
              labelFormatter={(label) => formatDateLabel(String(label))}
              formatter={(value, name) => [name === "performance" ? formatPct(Number(value)) : formatMoney(Number(value)), titleLabel(String(name))]}
            />
            {hasCostBasis
              ? <Line type="monotone" dataKey="performance" name="open-lot return" stroke="#43e58f" strokeWidth={2.4} dot={false} activeDot={{ r: 4 }} />
              : <Line type="monotone" dataKey="value" name="reported value" stroke="#68a8ff" strokeWidth={2.4} dot={{ r: 3 }} activeDot={{ r: 4 }} />}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="performance-compare">
        <MetricBadge label={hasCostBasis ? "Portfolio" : "Filings"} value={hasCostBasis ? formatPct(portfolio.performance) : String(chartData.length)} tone={hasCostBasis ? (portfolio.performance >= 0 ? "good" : "bad") : "info"} />
        <MetricBadge label="Reported Value" value={formatMoney(last?.value ?? portfolio.totalValue)} caption={last ? formatDateLabel(last.date) : formatDateLabel(portfolio.updated)} />
        <MetricBadge label={hasCostBasis ? "Cost Basis" : "Holdings"} value={hasCostBasis ? formatMoney(last?.costBasis ?? portfolio.estimatedInvested) : String(last?.holdingsCount ?? portfolio.holdingsCount)} caption={hasCostBasis ? `${chartData.length} visible points` : "latest filing"} />
      </div>
    </div>
  );
}

function TraderDistribution({ holdings }: { holdings: TraderPortfolioHolding[] }) {
  const ranked = [...holdings].sort((a, b) => b.weight - a.weight);
  const top = ranked.slice(0, 7);
  const restWeight = ranked.slice(7).reduce((total, holding) => total + holding.weight, 0);
  const largest = ranked[0];
  const data = [
    ...top.map((holding) => ({ name: holding.label, value: Number(holding.weight.toFixed(2)), marketValue: holding.marketValue })),
    ...(restWeight > 0 ? [{ name: "OTHER", value: Number(restWeight.toFixed(2)), marketValue: ranked.slice(7).reduce((total, holding) => total + holding.marketValue, 0) }] : []),
  ];
  return (
    <div className="trader-distribution">
      <div className="distribution-chart">
        <ResponsiveContainer width="100%" height={260}>
          <PieChart margin={{ top: 12, right: 12, bottom: 12, left: 12 }}>
            <Pie
              data={data}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              innerRadius="58%"
              outerRadius="84%"
              paddingAngle={3}
              cornerRadius={6}
              stroke="#ffffff"
              strokeWidth={4}
              isAnimationActive={false}
            >
              {data.map((entry, index) => <Cell key={entry.name} fill={CHART_COLORS[index % CHART_COLORS.length]} stroke="#ffffff" strokeWidth={4} />)}
            </Pie>
            <Tooltip
              cursor={false}
              contentStyle={{ background: "#ffffff", border: "1px solid #e5e7eb", borderRadius: 10, color: "#111827", boxShadow: "0 12px 30px rgba(15, 23, 42, 0.12)" }}
              formatter={(value, name, item) => [
                `${Number(value).toFixed(1)}% · ${formatMoney(Number((item.payload as { marketValue?: number }).marketValue ?? 0))}`,
                String(name),
              ]}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="distribution-center">
          <strong>{largest ? `${largest.weight.toFixed(0)}%` : "-"}</strong>
          <span>{largest?.label ?? "No holdings"}</span>
        </div>
      </div>
      <div className="distribution-legend">
        {data.map((holding, index) => (
          <div key={holding.name}>
            <span><i style={{ background: CHART_COLORS[index % CHART_COLORS.length] }} />{holding.name}</span>
            <strong>{holding.value.toFixed(1)}%</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

function TraderHoldingsTable({ holdings, onOpenTicker }: { holdings: TraderPortfolioHolding[]; onOpenTicker: (symbol: string) => void }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "weight", desc: true }]);
  const columns = useMemo(() => traderHoldingColumns(onOpenTicker), [onOpenTicker]);
  const table = useReactTable({
    data: holdings,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  return <DataTable table={table} emptyTitle="No current holdings" emptyDetail="This trader model has no reconstructed open positions." />;
}

function TraderTransactionsTable({ transactions, onOpenTicker }: { transactions: TraderPortfolioTransaction[]; onOpenTicker: (symbol: string) => void }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "date", desc: true }]);
  const [windowKey, setWindowKey] = useState<"6M" | "1Y" | "2Y" | "ALL">("1Y");
  const visible = useMemo(() => filterTransactionsWindow(transactions, windowKey), [transactions, windowKey]);
  const columns = useMemo(() => traderTransactionColumns(onOpenTicker), [onOpenTicker]);
  const table = useReactTable({
    data: visible,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 12 } },
  });
  if (!transactions.length) {
    return <EmptyState title="No allocation history" detail="The tracker snapshot did not include transaction rows." />;
  }
  return (
    <div className="allocation-table-wrap">
      <div className="chart-toolbar compact">
        <div>
          <strong>{visible.length}</strong>
          <span>recent allocation changes</span>
        </div>
        <SegmentedControl
          options={["6M", "1Y", "2Y", "ALL"]}
          value={windowKey}
          onChange={(value) => setWindowKey(value as "6M" | "1Y" | "2Y" | "ALL")}
        />
      </div>
      <DataTable table={table} emptyTitle="No recent allocation changes" emptyDetail="Use a longer window to inspect older disclosed transactions." />
    </div>
  );
}

const traderHoldingColumn = createColumnHelper<TraderPortfolioHolding>();
const traderTransactionColumn = createColumnHelper<TraderPortfolioTransaction>();

function traderHoldingColumns(onOpenTicker: (symbol: string) => void): ColumnDef<TraderPortfolioHolding, any>[] {
  return [
    traderHoldingColumn.accessor("label", {
      header: "Holding",
      cell: ({ row, getValue }) => {
        const holding = row.original;
        const label = String(getValue());
        if (holding.isTickerMapped && holding.ticker) {
          return <button className="ticker-link" type="button" onClick={() => onOpenTicker(holding.ticker)}>{label}</button>;
        }
        return (
          <div className="holding-label">
            <strong>{label}</strong>
            {holding.identifier && <small>{holding.identifier}</small>}
          </div>
        );
      },
    }),
    traderHoldingColumn.accessor("marketValue", {
      header: "Value",
      cell: ({ getValue }) => formatMoney(Number(getValue())),
    }),
    traderHoldingColumn.accessor("weight", {
      header: "Weight",
      cell: ({ getValue }) => <AllocationBar value={Number(getValue())} />,
    }),
    traderHoldingColumn.accessor("price", {
      header: "Last Price",
      cell: ({ getValue }) => Number(getValue()) > 0 ? formatMoney(Number(getValue())) : <span className="muted-cell">N/A</span>,
    }),
    traderHoldingColumn.accessor("costBasis", {
      header: "Cost Basis",
      cell: ({ row, getValue }) => row.original.costBasis > 0 ? formatMoney(Number(getValue())) : <span className="muted-cell">N/A</span>,
    }),
    traderHoldingColumn.accessor("unrealizedPnl", {
      header: "Open P/L",
      cell: ({ row, getValue }) => {
        if (row.original.costBasis <= 0) return <span className="muted-cell">N/A</span>;
        const value = Number(getValue());
        return <span className={value >= 0 ? "money-good" : "money-bad"}>{formatMoney(value)}</span>;
      },
    }),
  ];
}

function traderTransactionColumns(onOpenTicker: (symbol: string) => void): ColumnDef<TraderPortfolioTransaction, any>[] {
  return [
    traderTransactionColumn.accessor("date", {
      header: "Event Date",
      cell: ({ getValue }) => formatDateLabel(String(getValue())),
    }),
    traderTransactionColumn.accessor("symbol", {
      header: "Holding",
      cell: ({ row, getValue }) => {
        const value = String(getValue());
        return isLikelyTicker(value)
          ? <button className="ticker-link" type="button" onClick={() => onOpenTicker(row.original.symbol)}>{value}</button>
          : <span className="holding-name">{value}</span>;
      },
    }),
    traderTransactionColumn.accessor("type", {
      header: "Action",
      cell: ({ getValue }) => <span className={`action-chip ${String(getValue()).startsWith("S") ? "sell" : "buy"}`}>{String(getValue())}</span>,
    }),
    traderTransactionColumn.accessor("estimatedAmount", {
      header: "Est. Amount",
      cell: ({ getValue }) => formatMoney(Number(getValue())),
    }),
    traderTransactionColumn.accessor("price", {
      header: "Exec. Price",
      cell: ({ getValue }) => Number(getValue()) > 0 ? formatMoney(Number(getValue())) : <span className="muted-cell">N/A</span>,
    }),
    traderTransactionColumn.accessor("weightAfter", {
      header: "New Weight",
      cell: ({ row, getValue }) => {
        const next = Number(getValue());
        const delta = next - row.original.weightBefore;
        return <span>{next.toFixed(1)}% <small className={delta >= 0 ? "money-good" : "money-bad"}>{delta >= 0 ? "+" : ""}{delta.toFixed(1)}%</small></span>;
      },
    }),
    traderTransactionColumn.accessor("filedDate", {
      header: "Filed",
      cell: ({ getValue }) => formatDateLabel(String(getValue())),
    }),
  ];
}

function DataTable<T>({ table, emptyTitle, emptyDetail }: { table: Table<T>; emptyTitle: string; emptyDetail: string }) {
  const rows = table.getRowModel().rows;
  if (!rows.length) {
    return <EmptyState title={emptyTitle} detail={emptyDetail} />;
  }
  return (
    <div className="data-table-shell">
      <TableFrame>
        <table className="desk-table data-table">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id}>
                    <button type="button" onClick={header.column.getToggleSortingHandler()}>
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      <span>{header.column.getIsSorted() === "asc" ? "↑" : header.column.getIsSorted() === "desc" ? "↓" : ""}</span>
                    </button>
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </TableFrame>
      {table.getPageCount() > 1 && (
        <div className="table-pagination">
          <button type="button" onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()}>Prev</button>
          <span>Page {table.getState().pagination.pageIndex + 1} of {table.getPageCount()}</span>
          <button type="button" onClick={() => table.nextPage()} disabled={!table.getCanNextPage()}>Next</button>
        </div>
      )}
    </div>
  );
}

function AllocationBar({ value }: { value: number }) {
  return (
    <div className="allocation-cell">
      <div><i style={{ width: `${Math.min(100, Math.max(0, value))}%` }} /></div>
      <span>{value.toFixed(1)}%</span>
    </div>
  );
}

function TraderFilingCardView({ card, onOpenTicker }: { card: TraderFilingCard; onOpenTicker: (symbol: string) => void }) {
  return (
    <Panel className="span-4 trader-card" title={card.investor}>
      <div className="trader-card-metrics">
        <MetricBadge label="Holdings" value={String(card.tickerCount)} caption={card.event} />
        <MetricBadge label="Value (K)" value={formatMoney(card.totalValue)} caption={`Filed ${card.filed}`} tone="info" />
        <MetricBadge label="Top Ticker" value={card.topTicker || "-"} tone={card.topTicker ? "good" : "muted"} />
      </div>
      <div className="trader-holdings">
        {card.holdings.slice(0, 5).map((holding) => (
          <button key={`${holding.ticker}-${holding.security}`} type="button" disabled={!holding.ticker} onClick={() => holding.ticker && onOpenTicker(holding.ticker)}>
            <span>{holding.ticker || "CUSIP"}</span>
            <strong>{holding.security}</strong>
            <small>{formatMoney(holding.value)} · {formatNumber(holding.shares)} sh</small>
          </button>
        ))}
      </div>
    </Panel>
  );
}

function TradingViewChart({ symbol }: { symbol: string }) {
  const widgetSymbol = tradingViewSymbol(symbol);
  const params = new URLSearchParams({
    symbol: widgetSymbol,
    interval: "D",
    theme: "dark",
    style: "1",
    timezone: "America/New_York",
    withdateranges: "1",
    hide_side_toolbar: "0",
  });
  return (
    <div className="tradingview-frame">
      <iframe
        title={`${symbol} TradingView chart`}
        src={`https://s.tradingview.com/widgetembed/?${params.toString()}`}
        loading="lazy"
        referrerPolicy="no-referrer-when-downgrade"
      />
    </div>
  );
}

function PortfolioEntryForm({ onSaved }: { onSaved: () => Promise<void> }) {
  const [symbol, setSymbol] = useState("");
  const [quantity, setQuantity] = useState("");
  const [avgCost, setAvgCost] = useState("");
  const [purchaseDate, setPurchaseDate] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const canSave = symbol.trim() && Number(quantity) > 0 && Number(avgCost) >= 0;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setMessage("");
    try {
      await savePortfolioPosition({
        symbol,
        quantity: Number(quantity),
        avg_cost: Number(avgCost),
        purchase_date: purchaseDate || undefined,
        notes,
      });
      setSymbol("");
      setQuantity("");
      setAvgCost("");
      setPurchaseDate("");
      setNotes("");
      setMessage("Position saved");
      await onSaved();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="portfolio-form" onSubmit={submit}>
      <label>
        <span>Symbol</span>
        <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="NVDA" />
      </label>
      <label>
        <span>Shares</span>
        <input value={quantity} onChange={(event) => setQuantity(event.target.value)} inputMode="decimal" placeholder="10" />
      </label>
      <label>
        <span>Average Cost</span>
        <input value={avgCost} onChange={(event) => setAvgCost(event.target.value)} inputMode="decimal" placeholder="125.50" />
      </label>
      <label>
        <span>Purchase Date</span>
        <input value={purchaseDate} onChange={(event) => setPurchaseDate(event.target.value)} type="date" />
      </label>
      <label>
        <span>Notes</span>
        <input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Core AI exposure" />
      </label>
      <button className="primary-button" type="submit" disabled={!canSave || saving}>{saving ? "Saving..." : "Save Position"}</button>
      {message && <small>{message}</small>}
    </form>
  );
}

function HoldingsTable({ holdings, onOpenTicker, onDelete }: { holdings: Holding[]; onOpenTicker: (symbol: string) => void; onDelete: () => Promise<void> }) {
  const [sortKey, setSortKey] = useState<"ticker" | "weight" | "day" | "marketValue" | "pnl">("weight");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  if (!holdings.length) {
    return <EmptyState title="No holdings loaded" detail="Add your real positions with the form on this page." />;
  }
  const remove = async (symbol: string) => {
    await deletePortfolioPosition(symbol);
    await onDelete();
  };
  const setSort = (nextKey: typeof sortKey) => {
    if (nextKey === sortKey) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(nextKey);
      setSortDir(nextKey === "ticker" ? "asc" : "desc");
    }
  };
  const sortValue = (holding: Holding): string | number => {
    if (sortKey === "ticker") return holding.ticker;
    if (sortKey === "day") return Number.isFinite(holding.dayChangePct) ? holding.dayChangePct : Number.NEGATIVE_INFINITY;
    if (sortKey === "marketValue") return holding.hasMarketValue ? holding.marketValue : Number.NEGATIVE_INFINITY;
    if (sortKey === "pnl") return holding.hasPnl ? holding.unrealizedPnl : Number.NEGATIVE_INFINITY;
    return holding.weight;
  };
  const visible = holdings.slice().sort((a, b) => {
    const left = sortValue(a);
    const right = sortValue(b);
    if (typeof left === "string" || typeof right === "string") {
      return sortDir === "asc" ? String(left).localeCompare(String(right)) : String(right).localeCompare(String(left));
    }
    return sortDir === "asc" ? Number(left) - Number(right) : Number(right) - Number(left);
  });
  const sortMark = (key: typeof sortKey) => sortKey === key ? (sortDir === "asc" ? "↑" : "↓") : "";
  const SortHeader = ({ label, value }: { label: string; value: typeof sortKey }) => (
    <button className="sort-header-button" type="button" onClick={() => setSort(value)}>
      {label} <span>{sortMark(value)}</span>
    </button>
  );
  return (
    <TableFrame>
      <table className="desk-table">
        <thead>
          <tr>
            <th><SortHeader label="Symbol" value="ticker" /></th>
            <th><SortHeader label="Weight" value="weight" /></th>
            <th>Qty</th>
            <th>Last</th>
            <th><SortHeader label="Day" value="day" /></th>
            <th><SortHeader label="Market Value" value="marketValue" /></th>
            <th>Avg Cost</th>
            <th>Purchase Date</th>
            <th>Term</th>
            <th><SortHeader label="Unreal P/L" value="pnl" /></th>
            <th>Buy/Add Stance</th>
            <th>Next Step</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {visible.map((holding) => (
            <tr key={holding.ticker}>
              <td><button className="ticker-link" type="button" onClick={() => onOpenTicker(holding.ticker)}>{holding.ticker}</button></td>
              <td>{holding.weight ? `${holding.weight.toFixed(1)}%` : "-"}</td>
              <td>{formatNumber(holding.quantity)}</td>
              <td>{Number.isFinite(holding.price) ? formatMoney(holding.price) : <span className="muted-cell">Quote not loaded</span>}</td>
              <td className={holding.dayChangeValue >= 0 ? "positive" : "negative"}>{Number.isFinite(holding.dayChangeValue) ? `${formatMoney(holding.dayChangeValue)} · ${formatPct(holding.dayChangePct)}` : <span className="muted-cell">No change</span>}</td>
              <td>{holding.hasMarketValue ? formatMoney(holding.marketValue) : <span className="muted-cell">Quote not loaded</span>}</td>
              <td>{formatMoney(holding.averageCost)}</td>
              <td>{holding.purchaseDate || "Not set"}</td>
              <td>{holding.taxLotTerm}{holding.holdingDays ? ` (${holding.holdingDays}d)` : ""}</td>
              <td className={holding.hasPnl && holding.unrealizedPnl < 0 ? "negative" : "positive"}>{holding.hasPnl ? `${formatMoney(holding.unrealizedPnl)} · ${formatPct(holding.unrealizedPnlPct)}` : <span className="muted-cell">Needs quote</span>}</td>
              <td className="portfolio-stance-cell"><DecisionBadge value={holding.addStance} /></td>
              <td>{holding.nextStep}</td>
              <td><button className="text-link" type="button" onClick={() => void remove(holding.ticker)}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableFrame>
  );
}

function FilingsTable({ rows, onOpenTicker }: { rows: Filing[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No filing rows loaded" detail="13F/disclosure tables are empty for this run." />;
  }
  return (
    <TableFrame>
      <table className="desk-table">
        <thead>
          <tr>
            <th>Investor</th>
            <th>Security</th>
            <th>Action</th>
            <th>Shares</th>
            <th>Value (K)</th>
            <th>Event Date</th>
            <th>Filed Date</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.investor}-${row.ticker}-${row.filed}`}>
              <td>{row.investor}</td>
              <td>{row.ticker ? <button className="ticker-link" type="button" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}</button> : row.security}</td>
              <td><DecisionBadge value={row.action} /></td>
              <td>{formatNumber(row.shares)}</td>
              <td>{formatMoney(row.value)}</td>
              <td>{row.event}</td>
              <td>{row.filed}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableFrame>
  );
}

function HealthTable({ rows }: { rows: HealthRow[] }) {
  if (!rows.length) {
    return <EmptyState title="No source-health rows" detail="Run provider/source health jobs to populate this page." />;
  }
  return (
    <TableFrame>
      <table className="desk-table health-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Freshness</th>
            <th>Contract</th>
            <th>Stale After</th>
            <th>Last Run</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={healthRowKey(row)}>
              <td>{row.provider}</td>
              <td>{titleLabel(row.kind)}</td>
              <td><StatusDot status={row.status} /></td>
              <td>{row.freshness}</td>
              <td>{row.contract}</td>
              <td>{row.staleAfter}</td>
              <td>{row.lastRun}</td>
              <td>{row.sourceUrl}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableFrame>
  );
}

function DecisionBasisList({ rows, onOpenTicker }: { rows: Opportunity[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No decision rows" detail="Decision basis appears after the decision queue or candidate tables return rows." />;
  }
  return (
    <div className="decision-basis-list">
      {rows.map((item) => (
        <button key={`basis-${item.ticker}`} type="button" onClick={() => onOpenTicker(item.ticker)}>
          <div>
            <strong>{item.ticker}</strong>
            <DecisionBadge value={item.actionGrade} />
            <FreshnessBadge status={item.freshnessStatus} />
          </div>
          <p>{item.decisionBasis}</p>
          <small>{item.inclusionReasons.slice(0, 2).join(" · ")}</small>
        </button>
      ))}
    </div>
  );
}

function DecisionWarningList({ rows, onOpenTicker }: { rows: Opportunity[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No stale/source-thin warnings" detail="Current top rows do not have frontend-visible stale or source-thin gates." />;
  }
  return (
    <div className="decision-warning-list">
      {rows.map((item) => (
        <button key={`warning-${item.ticker}`} type="button" onClick={() => onOpenTicker(item.ticker)}>
          <AlertTriangle size={14} />
          <strong>{item.ticker}</strong>
          <span>{warningText(item)}</span>
        </button>
      ))}
    </div>
  );
}

function PulseList({ items, onOpenTicker }: { items: Opportunity[]; onOpenTicker: (symbol: string) => void }) {
  if (!items.length) {
    return <EmptyState title="No recent changes" detail="No signal rows are available for today's queue." />;
  }
  return (
    <div className="pulse-list">
      {items.map((item) => (
        <button key={item.ticker} type="button" onClick={() => onOpenTicker(item.ticker)}>
          <span className={toneClass(item.decision)} />
          <strong>{item.ticker}</strong>
          <small>{item.whyNow}</small>
        </button>
      ))}
      <TextLink>View all changes</TextLink>
    </div>
  );
}

function BarList({ rows, showValue = true }: { rows: Array<[string, number]>; showValue?: boolean }) {
  return (
    <div className="bar-list">
      {rows.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <div><i style={{ width: `${Math.max(4, Math.min(100, value))}%` }} /></div>
          {showValue && <strong>{value}%</strong>}
        </div>
      ))}
    </div>
  );
}

function CatalystList({ events, onOpenTicker }: { events: CalendarEvent[]; onOpenTicker?: (symbol: string) => void }) {
  if (!events.length) {
    return <EmptyState title="No events" detail="No catalyst or earnings rows are available." />;
  }
  return (
    <div className="catalyst-list">
      {events.map((event) => (
        <div key={event.id} className="calendar-event-row">
          <button type="button" onClick={() => event.symbol && onOpenTicker?.(event.symbol)}>
            <i className={event.type} />
            <span>{event.label}</span>
            <small>{event.dateText}</small>
          </button>
          <span className={`event-status ${event.status}`}>{titleLabel(event.status)}</span>
          {event.sourceUrl && <a href={event.sourceUrl} target="_blank" rel="noreferrer">{event.sourceName}</a>}
        </div>
      ))}
    </div>
  );
}

function InfoPanel({ title, items, tone }: { title: string; items: string[]; tone: Tone }) {
  return (
    <Panel className={`span-4 info-panel ${tone}`} title={title}>
      <BulletList tone={tone} items={items} />
    </Panel>
  );
}

function ResearchPacketSummary({ packet }: { packet: RowRecord }) {
  if (numberField(packet, ["evidence_count"], 0) <= 0) {
    return <EmptyState title="No source-backed packet sections" detail="Bull/bear sections are hidden until ticker-specific thesis or memo evidence exists." />;
  }
  return (
    <div className="packet-summary">
      <section>
        <h3>Bull Case</h3>
        <BulletList tone="good" items={arrayText(packet.bull_case)} />
      </section>
      <section>
        <h3>Bear Case</h3>
        <BulletList tone="bad" items={arrayText(packet.bear_case)} />
      </section>
    </div>
  );
}

function BulletList({ items, tone }: { items: string[]; tone: Tone }) {
  return (
    <ul className={`bullet-list ${tone}`}>
      {items.map((item) => <li key={item}>{item}</li>)}
    </ul>
  );
}

function DecisionBadge({ value }: { value: string }) {
  return <span className={`decision-badge ${toneClass(value)}`}>{value}</span>;
}

function FreshnessBadge({ status }: { status: FreshnessStatus }) {
  return <span className={`freshness-badge ${status}`}>{titleLabel(status)}</span>;
}

function StatusDot({ status }: { status: HealthRow["status"] }) {
  return <span className={`status-dot ${status.toLowerCase()}`}><i />{status}</span>;
}

function CalendarMonth({ events, onOpenTicker }: { events: CalendarEvent[]; onOpenTicker: (symbol: string) => void }) {
  const firstDate = events[0]?.fullDate ? parseCalendarDate(events[0].fullDate) : null;
  const year = firstDate?.getFullYear() ?? new Date().getFullYear();
  const month = firstDate?.getMonth() ?? new Date().getMonth();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const firstWeekday = new Date(year, month, 1).getDay();
  const today = new Date();
  const cells = [
    ...Array.from({ length: firstWeekday }, (_, index) => ({ day: index + 1, muted: true })),
    ...Array.from({ length: daysInMonth }, (_, index) => ({ day: index + 1, muted: false })),
  ];
  return (
    <div className="calendar-month">
      {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => <strong key={day}>{day}</strong>)}
      {cells.map((cell, index) => {
        const dayEvents = cell.muted ? [] : events.filter((item) => {
          const eventDate = parseCalendarDate(item.fullDate);
          return eventDate && eventDate.getFullYear() === year && eventDate.getMonth() === month && eventDate.getDate() === cell.day;
        });
        const isToday = today.getFullYear() === year && today.getMonth() === month && today.getDate() === cell.day;
        return (
          <button key={`${cell.day}-${index}`} className={`${isToday && !cell.muted ? "today" : ""} ${cell.muted ? "muted-day" : ""}`} type="button">
            <span>{cell.day}</span>
            {dayEvents.slice(0, 3).map((event) => (
              <em key={event.id} className={`${event.type} ${event.status}`} onClick={() => event.symbol && onOpenTicker(event.symbol)}>{event.label}</em>
            ))}
            {dayEvents.length > 3 && <small>+{dayEvents.length - 3} more</small>}
          </button>
        );
      })}
    </div>
  );
}

function OptionsEvidence({ rows: sourceRows }: { rows: RowRecord[] }) {
  if (!sourceRows.length) {
    return <EmptyState title="No options rows" detail="No ticker-specific or global options rows are available." />;
  }
  const scenarioRows = sourceRows.filter(isOptionsPayoffScenario).slice(0, 8);
  if (!scenarioRows.length) {
    return <GenericRows rows={sourceRows} emptyTitle="No options rows" emptyDetail="No ticker-specific or global options rows are available." onOpenTicker={() => undefined} />;
  }
  return (
    <div className="options-evidence">
      <div className="options-list">
        {scenarioRows.map((row, index) => {
          const symbol = stringField(row, ["symbol", "ticker"]) || "MARKET";
          const strategy = titleLabel(stringField(row, ["strategy_type", "strategy"]) || "Option Scenario");
          const expiry = stringField(row, ["expiry", "expiration"]) || "-";
          const spot = numberField(row, ["spot", "underlying"], Number.NaN);
          const dte = numberField(row, ["dte", "days_to_expiry"], Number.NaN);
          const iv = numberField(row, ["iv", "implied_volatility"], Number.NaN);
          const netPremium = numberField(row, ["net_premium", "premium"], Number.NaN);
          const maxProfit = row.max_profit === null || row.max_profit === undefined ? "Unlimited" : formatMoney(numberField(row, ["max_profit"], Number.NaN));
          const maxLoss = formatMoney(numberField(row, ["max_loss"], Number.NaN));
          const breakevens = formatStrikeList(listField(row, ["breakevens"]));
          const legCount = Array.isArray(row.legs) ? row.legs.length : Math.round(numberField(row, ["leg_count"], 0));
          const expired = isExpiredOptionRow(row);
          return (
            <section key={String(row.id ?? `${symbol}-${strategy}-${expiry}-${index}`)} className={`option-scenario ${expired ? "expired" : ""}`}>
              <div className="option-scenario-head">
                <span>{symbol}</span>
                <strong>{strategy}</strong>
                <small>{expired ? `Expired ${expiry}` : expiry}</small>
              </div>
              <div className="option-metrics">
                <div>
                  <span>Net Premium</span>
                  <strong>{formatNetPremium(netPremium)}</strong>
                </div>
                <div>
                  <span>Max Profit</span>
                  <strong>{maxProfit}</strong>
                </div>
                <div>
                  <span>Max Loss</span>
                  <strong className="negative">{maxLoss}</strong>
                </div>
                <div>
                  <span>Breakeven</span>
                  <strong>{breakevens || "-"}</strong>
                </div>
              </div>
              <p>{expired ? "Expired option context. Historical only; refresh the chain before using for trade setup." : [
                Number.isFinite(spot) ? `spot ${formatMoney(spot)}` : "",
                Number.isFinite(dte) ? `${Math.round(dte)} DTE` : "",
                Number.isFinite(iv) ? `IV ${formatUnsignedPct(iv > 0 && iv <= 1 ? iv * 100 : iv)}` : "",
                legCount ? `${legCount} ${legCount === 1 ? "leg" : "legs"}` : "",
              ].filter(Boolean).join(" · ")}</p>
            </section>
          );
        })}
      </div>
      {sourceRows.length > scenarioRows.length && <small className="panel-footnote">{sourceRows.length - scenarioRows.length} supporting chain or expiry rows also loaded for this ticker.</small>}
    </div>
  );
}

function GenericRows({ rows: sourceRows, emptyTitle, emptyDetail, onOpenTicker }: { rows: RowRecord[]; emptyTitle: string; emptyDetail: string; onOpenTicker: (symbol: string) => void }) {
  if (!sourceRows.length) {
    return <EmptyState title={emptyTitle} detail={emptyDetail} />;
  }
  return (
    <SourceRowsTable rows={sourceRows} onOpenTicker={onOpenTicker} />
  );
}

function SourceRowsTable({ rows: sourceRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  const displayRows = sourceRows.slice(0, 30);
  return (
    <TableFrame>
      <table className="desk-table source-row-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Source</th>
            <th>As Of</th>
            <th>Field</th>
            <th>Value</th>
            <th>Freshness</th>
          </tr>
        </thead>
        <tbody>
          {displayRows.map((row, index) => {
            const symbol = symbolFromRow(row);
            const source = sourceNameForRow(row);
            const asOf = asOfForRow(row);
            const field = sourceFieldForRow(row);
            const value = readableRowText(row);
            const freshness = freshnessForSourceRow(row);
            return (
              <tr key={`${source}-${asOf}-${field}-${value}-${index}`}>
                <td>{symbol ? <button className="ticker-link" type="button" onClick={() => onOpenTicker(symbol)}>{symbol}</button> : "MARKET"}</td>
                <td>{source}</td>
                <td>{asOf || "-"}</td>
                <td>{field}</td>
                <td className="clip">{value}</td>
                <td><span className={`row-freshness ${freshnessTone(normalizeFreshnessStatus(freshness))}`}>{freshness}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {sourceRows.length > displayRows.length && <small className="panel-footnote">{sourceRows.length - displayRows.length} additional source rows hidden.</small>}
    </TableFrame>
  );
}

function readableRowText(row: RowRecord): string {
  const explicit = displayValue(
    row.title ??
    row.review_reason ??
    row.thesis ??
    row.thesis_summary ??
    row.report_markdown ??
    row.event ??
    row.event_type ??
    row.summary ??
    row.notes ??
    row.status ??
    row.detail ??
    row.form_type ??
    row.source,
  );
  if (explicit && explicit !== "-") return explicit;
  const decisionBasis = objectField(row.decision_basis);
  if (stringField(decisionBasis, ["summary"])) return stringField(decisionBasis, ["summary"]);
  if (stringField(row, ["action_grade", "freshness_status"])) {
    return [
      stringField(row, ["action_grade"]) ? `action ${stringField(row, ["action_grade"])}` : "",
      stringField(row, ["freshness_status"]) ? `freshness ${stringField(row, ["freshness_status"])}` : "",
      stringField(row, ["source_cluster"]) ? `source ${stringField(row, ["source_cluster"])}` : "",
    ].filter(Boolean).join(" · ");
  }
  if (row.metrics !== undefined) {
    const metrics = objectField(row.metrics);
    const parts = [
      stringField(row, ["verdict", "stage", "method", "source"]),
      numberField(metrics, ["close", "current", "last_close"], Number.NaN),
      numberField(metrics, ["score", "conditions_passed"], Number.NaN),
    ];
    const metricText = [
      parts[0] ? titleLabel(String(parts[0])) : "",
      Number.isFinite(parts[1] as number) ? `price ${formatMoney(parts[1] as number)}` : "",
      Number.isFinite(parts[2] as number) ? `metric ${parts[2]}` : "",
    ].filter(Boolean).join(" · ");
    if (metricText) return metricText;
  }
  if (row.estimates !== undefined) return "Analyst estimate snapshot loaded.";
  if (row.report_json !== undefined) return "Research report JSON loaded.";
  return `${Object.keys(row).length} source fields loaded.`;
}

function sourceNameForRow(row: RowRecord): string {
  return titleLabel(stringField(row, ["source", "provider", "source_key", "method", "form_type"]) || "source");
}

function asOfForRow(row: RowRecord): string {
  const value = stringField(row, ["as_of", "observed_at", "last_reviewed", "updated_at", "date", "event_date", "filing_date", "created_at", "published_at", "expiry"]);
  return value ? formatDateLabel(value) : "";
}

function sourceFieldForRow(row: RowRecord): string {
  if (stringField(row, ["action_grade"])) return "Decision";
  if (stringField(row, ["review_reason", "why_owned_watched"])) return "Thesis";
  if (stringField(row, ["strategy_type", "option_type"])) return "Options";
  if (stringField(row, ["verdict", "stage"])) return "Setup";
  if (stringField(row, ["method"])) return "Valuation";
  if (row.metrics !== undefined) return "Metrics";
  if (row.estimates !== undefined) return "Estimates";
  if (stringField(row, ["form_type"])) return "Filing";
  if (stringField(row, ["event", "event_type"])) return "Catalyst";
  return Object.keys(row).slice(0, 2).map(titleLabel).join(" / ") || "Row";
}

function freshnessForSourceRow(row: RowRecord): string {
  if (isExpiredOptionRow(row)) return "Expired";
  const explicit = stringField(row, ["freshness_status", "overall_decision_freshness", "source_freshness", "status"]);
  if (explicit) return titleLabel(explicit);
  return asOfForRow(row) ? "Loaded" : "Not loaded";
}

function isExpiredOptionRow(row: RowRecord): boolean {
  if (!isOptionsPayoffScenario(row) && !stringField(row, ["expiry", "expiration"])) return false;
  const expiry = stringField(row, ["expiry", "expiration"]);
  if (expiry) {
    const parsed = new Date(`${expiry.slice(0, 10)}T00:00:00`);
    if (!Number.isNaN(parsed.getTime())) {
      const today = new Date();
      const todayLocal = new Date(today.getFullYear(), today.getMonth(), today.getDate());
      return parsed.getTime() < todayLocal.getTime();
    }
  }
  const dte = numberField(row, ["dte", "days_to_expiry"], Number.NaN);
  return Number.isFinite(dte) && dte < 0;
}

function SummaryList({ rows, onOpenTicker }: { rows: SummaryItem[]; onOpenTicker?: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No source rows" detail="The backing source table has no rows for this panel." />;
  }
  return (
    <div className="summary-list">
      {rows.map((row, index) => (
        <button key={`${row.label}-${row.value}-${row.caption}-${index}`} type="button" disabled={!row.symbol || !onOpenTicker} onClick={() => row.symbol && onOpenTicker?.(row.symbol)}>
          <span>{row.label}</span>
          <strong className={row.tone === "good" ? "positive" : row.tone === "bad" ? "negative" : ""}>{row.value}</strong>
          <small>{row.caption}</small>
        </button>
      ))}
    </div>
  );
}

function DetailRows({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="detail-rows">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function AlertList({ rows }: { rows: HealthRow[] }) {
  const alerts = rows.filter((row) => row.status !== "Healthy");
  if (!alerts.length) {
    return (
      <div className="alert-list">
        <div><HeartPulse size={15} /><strong>All clear</strong><small>No degraded or warning health rows are loaded.</small></div>
      </div>
    );
  }
  return (
    <div className="alert-list">
      {alerts.slice(0, 8).map((row) => (
        <div key={healthRowKey(row)}><AlertTriangle size={15} /><strong>{row.provider}</strong><small>{row.status}: {row.lastRun}</small></div>
      ))}
      {alerts.length > 8 && <TextLink>{alerts.length - 8} more alerts</TextLink>}
    </div>
  );
}

function JobRuns({ rows }: { rows: HealthRow[] }) {
  if (!rows.length) {
    return <EmptyState title="No recent job rows" detail="Provider run rows are empty." />;
  }
  return (
    <div className="job-runs">
      {rows.slice(0, 6).map((row) => (
        <div key={healthRowKey(row)}>
          <span>{row.provider}</span>
          <DecisionBadge value={row.status === "Healthy" ? "Success" : row.status} />
          <small>{row.freshness}</small>
          <small>{row.sourceUrl}</small>
        </div>
      ))}
    </div>
  );
}

function FreshnessGrid({ rows }: { rows: HealthRow[] }) {
  if (!rows.length) {
    return <EmptyState title="No freshness rows" detail="Source-freshness checks have not produced rows." />;
  }
  return (
    <div className="freshness-grid">
      {rows.slice(0, 5).map((row) => (
        <div key={healthRowKey(row)}>
          <span>{row.provider}</span>
          {Array.from({ length: 6 }, (_, index) => <i key={index} className={row.status === "Healthy" || index < 4 ? "good" : "warn"} />)}
        </div>
      ))}
    </div>
  );
}

function healthRowKey(row: HealthRow): string {
  return `${row.kind}:${row.provider}:${row.freshness}:${row.status}`;
}

function TagRow({ tags }: { tags: string[] }) {
  return <span className="tag-row">{tags.map((tag) => <i key={tag}>{tag}</i>)}</span>;
}

function bucketOpportunities(opportunities: Opportunity[]): Record<DecisionBucket, Opportunity[]> {
  const buckets: Record<DecisionBucket, Opportunity[]> = {
    Act: [],
    Research: [],
    Watch: [],
    Reject: [],
    Stale: [],
  };
  for (const opportunity of opportunities) {
    buckets[opportunity.actionGrade].push(opportunity);
  }
  return buckets;
}

function mapRowsBySymbol(sourceRows: RowRecord[]): Map<string, RowRecord> {
  const mapped = new Map<string, RowRecord>();
  for (const row of sourceRows) {
    const symbol = stringField(row, ["symbol", "ticker", "security", "name"]).toUpperCase();
    if (symbol && !mapped.has(symbol)) {
      mapped.set(symbol, row);
    }
  }
  return mapped;
}

function groupRowsBySymbol(sourceRows: RowRecord[]): Map<string, RowRecord[]> {
  const grouped = new Map<string, RowRecord[]>();
  for (const row of sourceRows) {
    const symbol = stringField(row, ["symbol", "ticker", "security", "name"]).toUpperCase();
    if (!symbol) continue;
    grouped.set(symbol, [...(grouped.get(symbol) ?? []), row]);
  }
  return grouped;
}

function listField(row: RowRecord, keys: string[]): string[] {
  for (const key of keys) {
    const value = row[key];
    if (Array.isArray(value)) {
      return value.map((item) => displayValue(item as JsonValue)).filter((item) => item && item !== "-");
    }
    if (value && typeof value === "object") {
      return Object.values(value).map((item) => displayValue(item as JsonValue)).filter((item) => item && item !== "-");
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = parseDelimitedList(value);
      return parsed.length ? parsed : [value.trim()];
    }
  }
  return [];
}

function parseDelimitedList(value: string): string[] {
  const trimmed = value.trim();
  if ((trimmed.startsWith("[") && trimmed.endsWith("]")) || (trimmed.startsWith("{") && trimmed.endsWith("}"))) {
    try {
      const parsed = JSON.parse(trimmed) as JsonValue;
      if (Array.isArray(parsed)) {
        return parsed.map(displayValue).filter((item) => item && item !== "-");
      }
      if (parsed && typeof parsed === "object") {
        return Object.values(parsed).map((item) => displayValue(item)).filter((item) => item && item !== "-");
      }
    } catch {
      // Fall through to delimiter parsing.
    }
  }
  return trimmed
    .split(/\n|;|\|/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function uniqueText(values: string[]): string[] {
  const seen = new Set<string>();
  const output: string[] = [];
  for (const value of values) {
    const normalized = value.replace(/\s+/g, " ").trim();
    if (!normalized || normalized === "-" || seen.has(normalized.toLowerCase())) continue;
    seen.add(normalized.toLowerCase());
    output.push(normalized);
  }
  return output;
}

function normalizeFreshnessStatus(value: string): FreshnessStatus {
  const normalized = value.toLowerCase();
  if (normalized.includes("stale") || normalized.includes("expired") || normalized.includes("old")) return "stale";
  if (!normalized || normalized.includes("not_loaded") || normalized.includes("not loaded") || normalized.includes("degrad") || normalized.includes("fail") || normalized.includes("warn")) return "degraded";
  if (normalized.includes("fresh") || normalized.includes("current") || normalized.includes("healthy") || normalized.includes("recent") || normalized.includes("live")) return "fresh";
  return "unknown";
}

function actionGradeFromValue(value: string, freshness: FreshnessStatus): DecisionBucket {
  if (freshness === "stale" || freshness === "degraded") return "Stale";
  const normalized = value.toLowerCase();
  if (normalized.includes("act") || normalized.includes("buy") || normalized.includes("accumulate")) return "Act";
  if (normalized.includes("research") || normalized.includes("investigate") || normalized.includes("review")) return "Research";
  if (normalized.includes("reject") || normalized.includes("avoid") || normalized.includes("pass")) return "Reject";
  if (normalized.includes("stale")) return "Stale";
  return "Watch";
}

function quoteLabel(row: RowRecord | undefined): string {
  if (!row) return "-";
  const price = row.price ?? row.close ?? row.last;
  const change = numberField(row, ["change_pct", "percent_change", "change"], Number.NaN);
  const priceLabel = formatRawPrice(price);
  if (Number.isFinite(change)) {
    return `${priceLabel} (${formatPct(change)})`;
  }
  return priceLabel;
}

function catalystLabel(row: RowRecord | undefined): string {
  if (!row) return "-";
  const date = stringField(row, ["start_at", "event_date", "date", "due_date", "published_at"]);
  const label = stringField(row, ["event", "title", "event_type", "type"]) || "event";
  return [date ? formatDateLabel(date) : "", label].filter(Boolean).join(" · ") || "-";
}

function liquidityLabel(row: RowRecord | undefined): string {
  if (!row) return "-";
  const grade = titleLabel(stringField(row, ["grade", "liquidity_grade"]) || "not loaded");
  const adv = numberField(row, ["avg_dollar_volume", "dollar_volume"], 0);
  return adv ? `${grade} · ${formatCompactMoney(adv)} ADV` : grade;
}

function portfolioImpactLabel(row: RowRecord | undefined): string {
  if (!row) return "Unowned";
  const weight = numberField(row, ["weight", "portfolio_weight"], 0);
  const value = numberField(row, ["market_value", "value", "position"], 0);
  return weight ? `${weight.toFixed(1)}% owned` : value ? `${formatMoney(value)} owned` : "Owned";
}

function sourceClusterFromRows(sourceRows: RowRecord[]): string {
  const clusters = uniqueText(sourceRows.map((row) => stringField(row, ["source_cluster", "source_key", "source", "provider"])));
  return clusters.length ? clusters.slice(0, 2).join(" + ") : "-";
}

function decisionTags(freshness: FreshnessStatus, sourceThin: boolean, sourceRows: RowRecord[]): string[] {
  const tags = [freshness === "fresh" ? "Fresh" : freshness === "unknown" ? "Freshness not loaded" : titleLabel(freshness)];
  if (sourceThin) tags.push("Thin");
  for (const row of sourceRows.slice(0, 2)) {
    const source = stringField(row, ["source_key", "source", "provider"]);
    if (source) tags.push(titleLabel(source).slice(0, 10));
  }
  return tags;
}

function freshnessTone(status: FreshnessStatus): Tone {
  if (status === "fresh") return "good";
  if (status === "unknown") return "muted";
  return status === "stale" ? "bad" : "warn";
}

function warningText(item: Opportunity): string {
  const warnings = [
    item.isStale ? "stale data gate" : "",
    item.isSourceThin ? "source-thin evidence" : "",
    ...item.blockingGates,
  ].filter(Boolean);
  return warnings.length ? warnings.join(" · ") : "No blocking gate";
}

function researchChecklist(readiness: RowRecord | undefined, opportunity: Opportunity | undefined): string[] {
  const missing = new Set(listField(readiness ?? {}, ["missing_inputs"]));
  if (!opportunity || opportunity.evidenceCount < 2) missing.add("primary evidence");
  const items = [
    missing.has("thesis") ? "Thesis row not loaded" : "",
    missing.has("valuation") ? "Valuation row not loaded" : "",
    opportunity?.catalystWindow && opportunity.catalystWindow !== "-" ? "" : "Catalyst row not loaded",
    missing.has("primary evidence") ? "Primary evidence row not loaded" : "",
    stringField(readiness ?? {}, ["next_action"]) || "Next research action: refresh ticker-specific packet",
  ].filter(Boolean);
  return items.length ? items : ["Ticker-specific research packet has the required source-backed sections."];
}

function normalizeHealthStatus(value: string): HealthRow["status"] {
  const normalized = value.toLowerCase();
  if (normalized.includes("doc")) return "Documentation";
  if (normalized.includes("degrad") || normalized.includes("fail") || normalized.includes("error") || normalized.includes("stale") || normalized.includes("offline") || normalized.includes("missing")) return "Degraded";
  if (normalized.includes("warn") || normalized.includes("partial") || normalized.includes("disabled")) return "Warning";
  return "Healthy";
}

function isDocumentationRow(row: RowRecord): boolean {
  const combined = [stringField(row, ["kind", "type", "status", "source", "provider", "capability"]), stringField(row, ["detail", "message"])].join(" ").toLowerCase();
  return combined.includes("documentation") || combined.includes("docs-only") || combined.includes("docs only");
}

function sourceFreshnessContract(row: RowRecord): string {
  const source = [stringField(row, ["source", "provider", "capability"]), stringField(row, ["source_type", "kind"])].join(" ").toLowerCase();
  if (source.includes("quote") || source.includes("option") || source.includes("news") || source.includes("intraday")) return "Intraday source freshness";
  if (source.includes("price") || source.includes("technical") || source.includes("sepa") || source.includes("liquidity") || source.includes("correlation")) return "Daily market freshness";
  if (source.includes("fundamental") || source.includes("13f") || source.includes("disclosure") || source.includes("filing")) return "Filing cadence freshness";
  if (source.includes("arco") || source.includes("thesis")) return "Arco thesis freshness";
  return "-";
}

function stringField(row: RowRecord, keys: string[]): string {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    if (typeof value === "number") {
      return String(value);
    }
  }
  return "";
}

function isOptionsPayoffScenario(row: RowRecord): boolean {
  return Boolean(
    stringField(row, ["strategy_type", "strategy"]) ||
    row.breakevens !== undefined ||
    row.max_profit !== undefined ||
    row.max_loss !== undefined ||
    row.net_premium !== undefined,
  );
}

function formatNetPremium(value: number): string {
  if (!Number.isFinite(value)) {
    return "-";
  }
  if (value > 0) {
    return `Debit ${formatMoney(value)}`;
  }
  if (value < 0) {
    return `Credit ${formatMoney(Math.abs(value))}`;
  }
  return "Even";
}

function formatUnsignedPct(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(2)}%` : "-";
}

function formatStrikeList(values: string[]): string {
  return values
    .slice(0, 3)
    .map((value) => {
      const parsed = Number(value.replace(/[$,]/g, ""));
      return Number.isFinite(parsed) ? formatMoney(parsed) : value;
    })
    .join(", ");
}

function objectField(value: unknown): RowRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as RowRecord : {};
}

function arrayField(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function arrayText(value: unknown): string[] {
  const items = arrayField(value).map((item) => displayValue(item as JsonValue)).filter((item) => item && item !== "-");
  return items.length ? items : ["No source-backed packet row available."];
}

function numberField(row: RowRecord, keys: string[], fallback: number): number {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value > 0 && value <= 1 && key.includes("confidence") ? value * 100 : value;
    }
    if (typeof value === "string") {
      const parsed = Number(value.replace(/[$,%]/g, ""));
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return fallback;
}

function booleanField(row: RowRecord, keys: string[]): boolean {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    if (typeof value === "string") {
      const normalized = value.trim().toLowerCase();
      if (["true", "1", "yes"].includes(normalized)) return true;
      if (["false", "0", "no", ""].includes(normalized)) return false;
    }
  }
  return false;
}

function optionalNumberField(row: RowRecord, keys: string[]): number | null {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value.replace(/[$,%]/g, ""));
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return null;
}

function confidenceValue(row: RowRecord): number {
  const numeric = numberField(row, ["confidence_score", "conviction"], Number.NaN);
  if (Number.isFinite(numeric)) {
    return Math.round(numeric);
  }
  const label = stringField(row, ["confidence"]).toLowerCase();
  if (label.includes("high")) return 85;
  if (label.includes("medium")) return 65;
  if (label.includes("low")) return 35;
  return 50;
}

function componentRows(row: RowRecord): Array<[string, number]> {
  const raw = row.components ?? row.score_breakdown;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return [];
  }
  return Object.entries(raw)
    .map(([key, value]) => [titleLabel(key), typeof value === "number" ? Math.round(value) : Number(value)] as [string, number])
    .filter(([, value]) => Number.isFinite(value));
}

function componentValue(opportunity: Opportunity | undefined, key: string): number {
  const found = opportunity?.components.find(([label]) => label.toLowerCase().replace(/\s+/g, "_") === key);
  return found ? found[1] : 0;
}

function splitSignalText(value: string | undefined): string[] {
  if (!value) {
    return ["No source-backed row available."];
  }
  return value
    .split(/;|\n/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 5);
}

function titleLabel(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
    .replace(/\bDcf\b/g, "DCF")
    .replace(/\bEtf\b/g, "ETF")
    .replace(/\bSec\b/g, "SEC")
    .replace(/\bFcf\b/g, "FCF");
}

function formatGateLabel(value: string): string {
  if (value === "decision_reject") return "Decision is Reject";
  return titleLabel(value).replace(/\bTv\b/g, "TV").replace(/\bAi\b/g, "AI");
}

function formatGateList(values: string[]): string {
  return values.map(formatGateLabel).join(" · ");
}

function normalizeDecision(value: string): string {
  const normalized = value.toLowerCase();
  if (normalized.includes("accumulate")) return "Accumulate";
  if (normalized.includes("buy")) return "Buy";
  if (normalized.includes("avoid")) return "Avoid";
  if (normalized.includes("success")) return "Success";
  if (normalized.includes("warn")) return "Warning";
  if (normalized.includes("hold")) return "Hold";
  if (normalized.includes("monitor") || normalized.includes("watch")) return "Watch";
  return value ? value[0].toUpperCase() + value.slice(1).toLowerCase() : "Watch";
}

function normalizeTaxLotTerm(value: string): string {
  const normalized = value.toLowerCase();
  if (normalized.includes("long")) return "Long term";
  if (normalized.includes("short")) return "Short term";
  return "Unknown term";
}

function gradeFromScore(score: number): string {
  if (score >= 90) return "A+";
  if (score >= 85) return "A";
  if (score >= 80) return "A-";
  if (score >= 75) return "B+";
  if (score >= 65) return "B";
  return "C";
}

function toneClass(value: string): string {
  const normalized = value.toLowerCase();
  if (normalized.includes("act") || normalized.includes("accumulate") || normalized.includes("buy") || normalized.includes("success")) return "good";
  if (normalized.includes("reject") || normalized.includes("avoid") || normalized.includes("no new") || normalized.includes("do not") || normalized.includes("degraded") || normalized.includes("stale")) return "bad";
  if (normalized.includes("research") || normalized.includes("watch") || normalized.includes("warning")) return "warn";
  return "info";
}

function companyName(symbol: string): string {
  return {
    NVDA: "NVIDIA Corporation",
    AMD: "Advanced Micro Devices",
    COIN: "Coinbase Global",
    TSLA: "Tesla",
    AAPL: "Apple",
    SPY: "SPDR S&P 500 ETF",
    QQQ: "Invesco QQQ Trust",
  }[symbol] ?? "Market Instrument";
}

function tradingViewSymbol(symbol: string): string {
  const normalized = symbol.toUpperCase();
  if (normalized.includes(".")) {
    return normalized.replace(".", ":");
  }
  const exchange = TRADINGVIEW_EXCHANGES[normalized] ?? "NYSE";
  const suffix = exchange === "COINBASE" ? "USD" : "";
  return `${exchange}:${normalized}${suffix}`;
}

function formatPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function filterHistoryWindow(points: TraderPortfolioHistoryPoint[], windowKey: "1Y" | "3Y" | "5Y" | "ALL"): TraderPortfolioHistoryPoint[] {
  if (windowKey === "ALL" || points.length <= 1) return points;
  const years = windowKey === "1Y" ? 1 : windowKey === "3Y" ? 3 : 5;
  const latest = Math.max(...points.map((point) => new Date(point.date).getTime()).filter(Number.isFinite));
  const cutoff = latest - years * 365 * 24 * 60 * 60 * 1000;
  const filtered = points.filter((point) => new Date(point.date).getTime() >= cutoff);
  return filtered.length >= 2 ? filtered : points.slice(-2);
}

function filterTransactionsWindow(transactions: TraderPortfolioTransaction[], windowKey: "6M" | "1Y" | "2Y" | "ALL"): TraderPortfolioTransaction[] {
  const sorted = [...transactions].sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
  if (windowKey === "ALL") return sorted;
  const months = windowKey === "6M" ? 6 : windowKey === "1Y" ? 12 : 24;
  const latest = Math.max(...sorted.map((transaction) => new Date(transaction.date).getTime()).filter(Number.isFinite));
  const cutoff = latest - months * 30 * 24 * 60 * 60 * 1000;
  return sorted.filter((transaction) => new Date(transaction.date).getTime() >= cutoff);
}

function shortDateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
}

function formatMoney(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value > 1000 ? 0 : 2 });
}

function formatCompactMoney(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return `$${Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value)}`;
}

function formatNumber(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString() : "-";
}

function formatCompact(value: number): string {
  return Number.isFinite(value) ? Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value) : "-";
}

function formatDateLabel(value: string): string {
  const dateOnly = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const date = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function formatRawPrice(value: unknown): string {
  if (typeof value === "number") {
    return value.toLocaleString(undefined, { maximumFractionDigits: value > 1000 ? 0 : 2 });
  }
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return "-";
}
