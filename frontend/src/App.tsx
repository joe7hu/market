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
  Settings,
  Sparkles,
  Star,
  Sun,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
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
import { deletePortfolioPosition, loadPanelData, loadTicker, savePortfolioPosition } from "./api";
import type { JsonValue, PanelData, RowRecord, TickerPayload } from "./types";
import { displayValue, rows, symbolFromRow } from "./utils";

const initialData: PanelData = {
  dashboard: {},
  signals: { rows: [], count: 0 },
  opportunitiesRanked: { rows: [], count: 0 },
  opportunitySources: { rows: [], count: 0 },
  candidates: { rows: [], count: 0 },
  portfolio: { rows: [], count: 0 },
  theses: { rows: [], count: 0 },
  traderTwins: { rows: [], count: 0 },
  catalysts: { rows: [], count: 0 },
  fundamentals: { rows: [], count: 0 },
  disclosures: { rows: [], count: 0 },
  quotes: { rows: [], count: 0 },
  screener: { rows: [], count: 0 },
  optionsExpiries: { rows: [], count: 0 },
  optionsChain: { rows: [], count: 0 },
  news: { rows: [], count: 0 },
  sepa: { rows: [], count: 0 },
  liquidity: { rows: [], count: 0 },
  correlations: { rows: [], count: 0 },
  etfPremiums: { rows: [], count: 0 },
  analystEstimates: { rows: [], count: 0 },
  earnings: { rows: [], count: 0 },
  valuations: { rows: [], count: 0 },
  technicals: { rows: [], count: 0 },
  researchPackets: { rows: [], count: 0 },
  memos: { rows: [], count: 0 },
  providerRuns: { rows: [], count: 0 },
  sourceHealth: { rows: [], count: 0 },
  settings: {},
  errors: {},
};

type PageKey = "dashboard" | "opportunities" | "portfolio" | "research" | "filings" | "calendar" | "health" | "settings" | "ticker";
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
  confidence: number;
  decision: string;
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
  weight: number;
  marketValue: number;
  averageCost: number;
  purchaseDate: string;
  holdingDays: number;
  taxLotTerm: string;
  unrealizedPnl: number;
  unrealizedPnlPct: number;
  signal: string;
  action: string;
};

type CalendarEvent = {
  date: number;
  dateText: string;
  monthLabel: string;
  label: string;
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
};

type TraderPortfolioHistoryPoint = {
  date: string;
  value: number;
  costBasis: number;
  performance: number;
};

type SignalSourcePanel = {
  key: string;
  title: string;
  state: DataSourceState;
  count: number;
  leaders: SummaryItem[];
};

type HealthRow = {
  provider: string;
  status: "Healthy" | "Warning" | "Degraded";
  freshness: string;
  lastRun: string;
  sourceUrl: string;
};

type SummaryItem = {
  label: string;
  value: string;
  caption: string;
  tone: Tone;
  symbol?: string;
};

type DataSourceState = "live" | "empty";
const CHART_COLORS = ["#43e58f", "#68a8ff", "#f3bd45", "#ff9b4b", "#ff6b5f", "#9fc2ff", "#7ce0b5", "#d7a4ff", "#b5c7d8", "#5fd4ff"];
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

const navItems: Array<{ key: PageKey; label: string; icon: ReactNode }> = [
  { key: "dashboard", label: "Dashboard", icon: <Home size={15} /> },
  { key: "opportunities", label: "Opportunities", icon: <Sparkles size={15} /> },
  { key: "portfolio", label: "Portfolio", icon: <Layers3 size={15} /> },
  { key: "research", label: "Research", icon: <FileSearch size={15} /> },
  { key: "filings", label: "Trader Filings", icon: <ClipboardList size={15} /> },
  { key: "calendar", label: "Calendar", icon: <CalendarDays size={15} /> },
  { key: "health", label: "Health", icon: <HeartPulse size={15} /> },
  { key: "settings", label: "Settings", icon: <Settings size={15} /> },
];


export function App() {
  const [activePage, setActivePage] = useState<PageKey>("dashboard");
  const [data, setData] = useState<PanelData>(initialData);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [selectedTicker, setSelectedTicker] = useState("NVDA");
  const [ticker, setTicker] = useState<TickerPayload | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const nextData = await loadPanelData();
      setData(nextData);
      const firstSymbol = symbolFromRow(rows(nextData.signals)[0] ?? rows(nextData.candidates)[0] ?? rows(nextData.portfolio)[0]);
      if (firstSymbol && selectedTicker === "NVDA") {
        setSelectedTicker(firstSymbol);
      }
      setLastRefresh(new Date());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    let cancelled = false;
    void loadTicker(selectedTicker)
      .then((payload) => {
        if (!cancelled) {
          setTicker(payload);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setTicker(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTicker]);

  const model = useMemo(() => buildModel(data), [data]);
  const openTicker = (symbol: string) => {
    setSelectedTicker(symbol);
    setActivePage("ticker");
  };

  return (
    <div className="terminal-shell">
      <Sidebar activePage={activePage} onNavigate={setActivePage} />
      <main className="desk-main">
        {activePage === "dashboard" && <DashboardPage model={model} lastRefresh={lastRefresh} loading={loading} onRefresh={refresh} onOpenTicker={openTicker} />}
        {activePage === "opportunities" && <OpportunitiesPage model={model} onOpenTicker={openTicker} />}
        {activePage === "portfolio" && <PortfolioPage model={model} onOpenTicker={openTicker} onRefresh={refresh} />}
        {activePage === "research" && <ResearchPage data={data} model={model} onOpenTicker={openTicker} />}
        {activePage === "filings" && <FilingsPage model={model} onOpenTicker={openTicker} />}
        {activePage === "calendar" && <CalendarPage model={model} onOpenTicker={openTicker} />}
        {activePage === "health" && <HealthPage model={model} data={data} />}
        {activePage === "settings" && <SettingsPage data={data} />}
        {activePage === "ticker" && <TickerPage symbol={selectedTicker} ticker={ticker} model={model} data={data} onOpenTicker={openTicker} />}
      </main>
    </div>
  );
}

function Sidebar({ activePage, onNavigate }: { activePage: PageKey; onNavigate: (page: PageKey) => void }) {
  return (
    <aside className="sidebar">
      <button className="brand" type="button" onClick={() => onNavigate("dashboard")}>
        <span className="brand-mark">M</span>
        <span>
          <strong>market</strong>
          <small>Decision Desk</small>
        </span>
      </button>
      <nav className="side-nav" aria-label="Main navigation">
        {navItems.map((item) => (
          <button key={item.key} className={activePage === item.key ? "active" : ""} type="button" onClick={() => onNavigate(item.key)}>
            {item.icon}
            <span>{item.label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <button type="button">
          <Sun size={15} />
          <span>Light</span>
        </button>
        <button type="button">
          <UserRound size={15} />
          <span>Joe Hu</span>
          <ChevronRight size={13} />
        </button>
      </div>
    </aside>
  );
}

function DashboardPage({
  model,
  lastRefresh,
  loading,
  onRefresh,
  onOpenTicker,
}: {
  model: AppModel;
  lastRefresh: Date | null;
  loading: boolean;
  onRefresh: () => void;
  onOpenTicker: (symbol: string) => void;
}) {
  return (
    <PageFrame
      title="Good morning, Joe"
      subtitle={lastRefresh ? `Refreshed ${lastRefresh.toLocaleTimeString()}` : loading ? "Loading market data..." : "No data loaded"}
      action={
        <div className="button-row">
          <IconButton label="Refresh" onClick={onRefresh}>
            <RefreshCw size={15} className={loading ? "spin" : ""} />
          </IconButton>
          <GhostButton>
            <Settings size={14} /> Customize
          </GhostButton>
        </div>
      }
    >
      <SourceNotice
        items={[
          ["Watchlist", model.sources.watchlist],
          ["Signals", model.sources.opportunities],
          ["Portfolio", model.sources.holdings],
          ["Calendar", model.sources.calendar],
        ]}
      />
      <WatchlistStrip items={model.watchlist} onOpenTicker={onOpenTicker} />
      <div className="dashboard-layout">
        <Panel className="span-8" title="Actionable Queue">
          <OpportunityTable rows={model.opportunities.slice(0, 5)} compact onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Why Today / Recent Changes" headerAction={<X size={14} />}>
          <PulseList items={model.opportunities.slice(0, 5)} onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Portfolio Exposure" headerAction={<SourcePill state={model.sources.holdings} />}>
          <SummaryList rows={model.holdings.length ? holdingSummaryRows(model.holdings) : model.setupRows.slice(0, 4)} />
          <TextLink>View full portfolio</TextLink>
        </Panel>
        <Panel className="span-3" title="Top Sectors">
          <SummaryList rows={model.sectors.slice(0, 5)} />
        </Panel>
        <Panel className="span-5" title="Upcoming Catalysts">
          <CatalystList events={model.calendar.slice(0, 5)} />
          <TextLink>View calendar</TextLink>
        </Panel>
      </div>
    </PageFrame>
  );
}

function TickerPage({ symbol, ticker, model, data }: { symbol: string; ticker: TickerPayload | null; model: AppModel; data: PanelData; onOpenTicker: (symbol: string) => void }) {
  const [activeTab, setActiveTab] = useState("Overview");
  const opportunity = model.opportunities.find((item) => item.ticker === symbol);
  const quote = model.watchlist.find((item) => item.symbol === symbol);
  const setup = model.setupRows.find((item) => item.symbol === symbol);
  const liquidity = model.liquidityRows.find((item) => item.symbol === symbol);
  const valuation = model.valuationRows.find((item) => item.symbol === symbol);
  const technical = model.technicalRows.find((item) => item.symbol === symbol);
  const evidenceRows = ticker?.tables ? Object.entries(ticker.tables).filter(([, tableRows]) => tableRows?.length).length : 0;
  const foundTables = ticker?.tables ? Object.entries(ticker.tables).filter(([, tableRows]) => tableRows?.length).map(([name]) => name) : [];

  return (
    <PageFrame
      eyebrow="Ticker Detail / Evidence Dossier"
      title={symbol}
      subtitle={[opportunity?.name || companyName(symbol), titleLabel(opportunity?.assetClass ?? "instrument"), titleLabel(opportunity?.category ?? "watchlist")].filter(Boolean).join(" · ")}
      action={
        <div className="ticker-actions">
          <MetricBadge label="Grade" value={opportunity?.grade ?? "-"} tone={opportunity ? "good" : "muted"} />
          <MetricBadge label="Confidence" value={opportunity ? `${opportunity.confidence}%` : "-"} />
          <DecisionBadge value={opportunity?.decision ?? "No Signal"} />
          <IconButton label="Watch">
            <Star size={15} />
          </IconButton>
        </div>
      }
    >
      <SourceNotice
        items={[
          ["Ticker API", ticker?.found ? "live" : "empty"],
          ["Quote", model.watchlist.some((item) => item.symbol === symbol) ? model.sources.watchlist : "empty"],
          ["Signal", opportunity ? model.sources.opportunities : "empty"],
        ]}
      />
      <div className="ticker-price-row">
        <strong>{quote?.price ?? "-"}</strong>
        {quote && <span className={quote.change >= 0 ? "positive" : "negative"}>{formatPct(quote.change)}</span>}
        <span className="muted">{quote ? "Latest quote snapshot" : "No latest quote row"} · {opportunity?.freshness ?? "No signal freshness"}</span>
      </div>
      <TabBar tabs={["Overview", "Evidence Stack", "Fundamentals", "Estimates", "Financials", "News", "Filings", "Memos"]} active={activeTab} onSelect={setActiveTab} />
      {activeTab === "Overview" ? <div className="ticker-grid">
        <Panel className="span-7" title="TradingView Technical Chart">
          <TradingViewChart symbol={symbol} />
        </Panel>
        <Panel className="span-5" title="Price & Setup">
          <SummaryList rows={[
            { label: "Latest Quote", value: quote?.price ?? "-", caption: quote ? `Move ${formatPct(quote.change)}` : "No quote row", tone: quote ? "info" : "warn" },
            technical ? { ...technical, label: "Technical Score" } : { label: "Technical Score", value: "-", caption: "No technical feature row", tone: "warn" },
            setup ?? { label: "SEPA Setup", value: "-", caption: "No setup row", tone: "warn" },
            liquidity ?? { label: "Liquidity", value: "-", caption: "No liquidity row", tone: "warn" },
            valuation ?? { label: "Valuation", value: "-", caption: "No valuation row", tone: "warn" },
          ]} />
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
        <InfoPanel tone="info" title="Next Action" items={splitSignalText(opportunity?.nextAction)} />
        <Panel className="span-12" title="Evidence Snapshot">
          <div className="snapshot-grid">
            <MetricBadge label="Evidence Count" value={String(opportunity?.evidenceCount ?? 0)} caption="Signal citations" tone={(opportunity?.evidenceCount ?? 0) > 0 ? "good" : "warn"} />
            <MetricBadge label="API Tables" value={String(evidenceRows)} caption={foundTables.slice(0, 2).join(", ") || "No rows"} tone={evidenceRows ? "good" : "warn"} />
            <MetricBadge label="Technical" value={`${componentValue(opportunity, "technical")}%`} caption="Source score" tone="info" />
            <MetricBadge label="Thesis" value={`${componentValue(opportunity, "thesis")}%`} caption="Source score" tone={componentValue(opportunity, "thesis") >= 50 ? "good" : "warn"} />
            <MetricBadge label="Trader" value={`${componentValue(opportunity, "trader")}%`} caption="Source score" tone={componentValue(opportunity, "trader") >= 50 ? "good" : "warn"} />
            <MetricBadge label="Decision" value={opportunity?.decision ?? "None"} caption="Backend signal" tone="info" />
          </div>
        </Panel>
      </div> : <TickerTabContent activeTab={activeTab} ticker={ticker} data={data} />}
    </PageFrame>
  );
}

function PortfolioPage({ model, onOpenTicker, onRefresh }: { model: AppModel; onOpenTicker: (symbol: string) => void; onRefresh: () => Promise<void> }) {
  const hasHoldings = model.holdings.length > 0;
  return (
    <PageFrame
      title="Portfolio Overview"
      subtitle={hasHoldings ? `As of ${new Date().toLocaleDateString()}` : "Enter your real positions to enable portfolio analytics"}
    >
      <SourceNotice items={[["Holdings", model.sources.holdings], ["Signals", model.sources.opportunities], ["Quotes", model.sources.watchlist]]} />
      <MetricStrip
        metrics={[
          ["Net Liquidity", hasHoldings ? formatMoney(model.portfolioValue) : "Not imported", hasHoldings ? "Derived from imported holdings" : "Portfolio CSV absent", hasHoldings ? "good" : "warn"],
          ["Total Value", hasHoldings ? formatMoney(model.portfolioValue) : "Awaiting positions", hasHoldings ? "Imported portfolio rows" : "Manual entry below", hasHoldings ? "info" : "muted"],
          ["Unrealized P/L", hasHoldings ? formatMoney(model.holdings.reduce((total, holding) => total + holding.unrealizedPnl, 0)) : "Requires import", hasHoldings ? "From position rows" : "Requires cost basis", hasHoldings ? "good" : "muted"],
          ["Positions", hasHoldings ? String(model.holdings.length) : "0 imported", hasHoldings ? "Holdings" : "No owned exposure", "info"],
          ["Concentration", hasHoldings ? "Available" : "Needs holdings", hasHoldings ? "Risk" : "Enter positions first", hasHoldings ? "warn" : "muted"],
        ]}
      />
      <div className="portfolio-grid">
        <Panel className="span-8" title={`Holdings (${model.holdings.length})`}>
          <HoldingsTable holdings={model.holdings} onOpenTicker={onOpenTicker} onDelete={onRefresh} />
        </Panel>
        <Panel className="span-4" title="Add / Update Position">
          <PortfolioEntryForm onSaved={onRefresh} />
        </Panel>
        <Panel className="span-4" title="Exposure Breakdown">
          <SummaryList rows={hasHoldings ? holdingSummaryRows(model.holdings) : model.sectors.slice(0, 5)} />
        </Panel>
        <Panel className="span-4" title="Risk & Concentration">
          <SummaryList rows={model.liquidityRows.slice(0, 5)} onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Portfolio Fit Insights">
          <BulletList tone={model.holdings.length ? "warn" : "info"} items={model.holdings.length ? ["Portfolio rows loaded", "Review concentration against signal strength", "Liquidity check available from source tables"] : ["Manual position entry writes to local storage", "Use ticker, share count, and average cost", "No broker credentials or account data are required"]} />
        </Panel>
        <Panel className="span-4" title="Top Correlations">
          <SummaryList rows={model.correlationRows.slice(0, 5)} onOpenTicker={onOpenTicker} />
        </Panel>
      </div>
    </PageFrame>
  );
}

function OpportunitiesPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [decision, setDecision] = useState("");
  const [tickerQuery, setTickerQuery] = useState("");
  const [minScore, setMinScore] = useState(0);
  const [assetClass, setAssetClass] = useState("");
  const [minConfidence, setMinConfidence] = useState(0);
  const [source, setSource] = useState("");
  const filtered = model.opportunities.filter((item) => {
    const decisionMatches = !decision || item.decision === decision;
    const tickerMatches = !tickerQuery || item.ticker.includes(tickerQuery.trim().toUpperCase());
    const assetMatches = !assetClass || item.assetClass === assetClass;
    const confidenceMatches = item.confidence >= minConfidence;
    const sourceMatches = !source || item.components.some(([label, value]) => label === source && value > 0);
    return decisionMatches && tickerMatches && assetMatches && confidenceMatches && sourceMatches && item.score >= minScore;
  });
  const decisions = Array.from(new Set(model.opportunities.map((item) => item.decision)));
  const assetClasses = Array.from(new Set(model.opportunities.map((item) => item.assetClass).filter(Boolean))).sort();
  const sources = Array.from(new Set(model.opportunities.flatMap((item) => item.components.map(([label]) => label)))).sort();
  const leader = filtered[0];
  return (
    <div className="split-page">
      <FilterRail
        decision={decision}
        decisions={decisions}
        tickerQuery={tickerQuery}
        minScore={minScore}
        assetClass={assetClass}
        assetClasses={assetClasses}
        minConfidence={minConfidence}
        sources={sources}
        source={source}
        onDecision={setDecision}
        onTickerQuery={setTickerQuery}
        onMinScore={setMinScore}
        onAssetClass={setAssetClass}
        onMinConfidence={setMinConfidence}
        onSource={setSource}
        onReset={() => {
          setDecision("");
          setTickerQuery("");
          setMinScore(0);
          setAssetClass("");
          setMinConfidence(0);
          setSource("");
        }}
      />
      <PageFrame
        title="Opportunities"
        subtitle={`${filtered.length} of ${model.opportunities.length} results`}
        action={
          <GhostButton title="Filters apply immediately to the loaded source rows">
            <Database size={14} /> Source-backed View
          </GhostButton>
        }
      >
        <SourceNotice items={[["Signals", model.sources.opportunities], ["Quotes", model.sources.watchlist]]} />
        {leader ? (
          <TopOpportunityTicker opportunity={leader} onOpenTicker={onOpenTicker} />
        ) : (
          <EmptyState title="No top ticker for this filter set" detail="The ranked ticker card appears when at least one opportunity matches the active filters." />
        )}
        <div className="source-panel-grid">
          {model.signalSources.map((panel) => (
            <Panel key={panel.key} title={panel.title} headerAction={<SourcePill state={panel.state} />}>
              <SummaryList rows={panel.leaders} onOpenTicker={onOpenTicker} />
              <small className="panel-footnote">{panel.count} source rows</small>
            </Panel>
          ))}
        </div>
        <Panel title="Ranked Screen">
          <OpportunityTable rows={filtered} onOpenTicker={onOpenTicker} />
        </Panel>
      </PageFrame>
    </div>
  );
}

function FilingsPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [selectedInvestor, setSelectedInvestor] = useState("");
  const [traderQuery, setTraderQuery] = useState("");
  const fallbackCards = model.traderFilingCards;
  const traderMatches = model.traderPortfolios.filter((portfolio) => {
    const query = traderQuery.trim().toLowerCase();
    return !query || portfolio.investor.toLowerCase().includes(query) || portfolio.holdings.some((holding) => holding.ticker.toLowerCase().includes(query));
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
              <Panel className="span-8" title="Portfolio Performance">
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

function CalendarPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const monthLabel = model.calendar[0]?.monthLabel ?? "Source Calendar";
  return (
    <PageFrame title="Calendar" subtitle={`${model.calendar.length} dated source events`}>
      <SourceNotice items={[["Calendar", model.sources.calendar]]} />
      <div className="calendar-actions">
        <TabBar tabs={["Timeline", "Calendar", "By Ticker"]} active="Calendar" />
        <GhostButton>All Events</GhostButton>
        <GhostButton>All Tickers</GhostButton>
      </div>
      <div className="calendar-grid-wrap">
        <Panel className="calendar-panel" title={monthLabel}>
          <CalendarMonth events={model.calendar} onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel title="Upcoming (Next 7 Days)">
          <CatalystList events={model.calendar.slice(0, 5)} onOpenTicker={onOpenTicker} />
          <TextLink>View full calendar</TextLink>
        </Panel>
      </div>
    </PageFrame>
  );
}

function HealthPage({ model, data }: { model: AppModel; data: PanelData }) {
  const ready = data.dashboard.status?.ready ?? data.settings.status?.ready ?? true;
  return (
    <PageFrame title="Operations Health" subtitle="All times in ET">
      <SourceNotice items={[["Source Health", model.sources.health]]} />
      <MetricStrip
        metrics={[
          [ready ? "All Systems Operational" : "System Needs Attention", ready ? "Ready" : "Check", `Last check ${model.latestHealthCheck}`, ready ? "good" : "warn"],
          ["Providers", String(model.healthRows.length), "", "info"],
          ["Warnings", String(model.healthRows.filter((row) => row.status === "Warning").length), "", "warn"],
          ["Critical", String(model.healthRows.filter((row) => row.status === "Degraded").length), "", "bad"],
        ]}
      />
      <div className="health-grid">
        <Panel className="span-8" title="Provider Health">
          <HealthTable rows={model.healthRows} />
        </Panel>
        <Panel className="span-4" title="Active Alerts">
          <AlertList rows={model.healthRows} />
        </Panel>
        <Panel className="span-6" title="Recent Job Runs">
          <JobRuns rows={model.healthRows} />
        </Panel>
        <Panel className="span-6" title="Freshness Overview">
          <FreshnessGrid rows={model.healthRows} />
        </Panel>
      </div>
    </PageFrame>
  );
}

function TickerTabContent({ activeTab, ticker, data }: { activeTab: string; ticker: TickerPayload | null; data: PanelData }) {
  const keyByTab: Record<string, string[]> = {
    "Evidence Stack": ["opportunities_ranked", "opportunity_sources", "signals", "technicals", "sepa", "liquidity", "correlations", "valuations"],
    Fundamentals: ["fundamentals"],
    Estimates: ["analyst_estimates", "earnings"],
    Financials: ["fundamentals", "valuations"],
    News: ["news"],
    Filings: ["disclosures"],
    Memos: ["research_packets", "memos", "theses"],
  };
  const fallbackByTab: Record<string, RowRecord[]> = {
    "Evidence Stack": [...rows(data.opportunitiesRanked), ...rows(data.opportunitySources), ...rows(data.signals), ...rows(data.technicals), ...rows(data.sepa), ...rows(data.liquidity), ...rows(data.valuations)],
    Fundamentals: rows(data.fundamentals),
    Estimates: [...rows(data.analystEstimates), ...rows(data.earnings)],
    Financials: [...rows(data.fundamentals), ...rows(data.valuations)],
    News: rows(data.news),
    Filings: rows(data.disclosures),
    Memos: [...rows(data.researchPackets), ...rows(data.memos), ...rows(data.theses)],
  };
  const keys = keyByTab[activeTab] ?? [];
  const sourceRows = keys.flatMap((key) => ticker?.tables?.[key] ?? []);
  const displayRows = sourceRows.length ? sourceRows : fallbackByTab[activeTab] ?? [];
  return (
    <Panel title={activeTab}>
      <GenericRows
        rows={displayRows}
        emptyTitle={`No ${activeTab.toLowerCase()} rows`}
        emptyDetail={`No ticker-specific or global rows are available for ${activeTab}.`}
        onOpenTicker={() => undefined}
      />
    </Panel>
  );
}

function ResearchPage({ data, model, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const [selectedSymbol, setSelectedSymbol] = useState(model.opportunities[0]?.ticker ?? symbolFromRow(rows(data.signals)[0]) ?? "");
  const thesisRows = rows(data.theses);
  const newsRows = rows(data.news);
  const fundamentalsRows = rows(data.fundamentals);
  const memoRows = rows(data.memos);
  const signalRows = rows(data.signals);
  const packetRows = rows(data.researchPackets);
  const selected = selectedSymbol || model.opportunities[0]?.ticker || "";
  const selectedOpportunity = model.opportunities.find((item) => item.ticker === selected);
  const selectedPacket = packetRows.find((row) => symbolFromRow(row) === selected);
  const relatedTheses = thesisRows.filter((row) => symbolFromRow(row) === selected);
  const relatedNews = newsRows.filter((row) => symbolFromRow(row) === selected);
  const relatedFundamentals = fundamentalsRows.filter((row) => symbolFromRow(row) === selected);
  const relatedMemos = memoRows.filter((row) => symbolFromRow(row) === selected);
  const relatedSignals = signalRows.filter((row) => symbolFromRow(row) === selected);
  const researchUniverse = model.opportunities.slice(0, 12);
  return (
    <PageFrame title="Research" subtitle="Evidence, theses, memos, and source-backed notes">
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
          <GenericRows rows={relatedTheses.length ? relatedTheses : thesisRows} emptyTitle="No thesis rows" emptyDetail="Thesis tracker is empty for this database." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-6" title="Evidence Feed">
          <GenericRows rows={relatedNews.length ? relatedNews : newsRows} emptyTitle="No evidence feed rows" emptyDetail="News/evidence rows are empty." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Fundamental Watch">
          <GenericRows rows={relatedFundamentals.length ? relatedFundamentals : fundamentalsRows} emptyTitle="No fundamental rows" emptyDetail="Run fundamentals ingestion to fill this panel." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Invalidation Queue">
          <GenericRows rows={(relatedSignals.length ? relatedSignals : signalRows).map((row) => ({ ...row, title: row.invalidation ?? row.next_action ?? row.why_now }))} emptyTitle="No invalidation rows" emptyDetail="Signals have not produced invalidation text." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Memo Queue">
          <GenericRows rows={relatedMemos.length ? relatedMemos : memoRows} emptyTitle="No memo rows" emptyDetail="Run research_candidate or weekly_portfolio_review to create memos." onOpenTicker={onOpenTicker} />
        </Panel>
      </div>
    </PageFrame>
  );
}

function SettingsPage({ data }: { data: PanelData }) {
  const config = data.settings.config ?? {};
  const integration = data.settings.integration ?? {};
  return (
    <PageFrame title="Settings" subtitle="Local app configuration and source wiring">
      <div className="settings-grid">
        <Panel title="Configuration">
          <DetailRows rows={Object.entries(config).slice(0, 8).map(([key, value]) => [key, displayValue(value)])} />
        </Panel>
        <Panel title="Integration">
          <DetailRows rows={Object.entries(integration).map(([key, value]) => [key, displayValue(value)])} />
        </Panel>
        <Panel title="Source Rules">
          <BulletList tone="info" items={["No secrets are displayed in this UI.", "Arco evidence is consumed from the durable brain raw source path.", "Investment logic remains in the Python backend.", "Frontend pages only format and group source-backed rows."]} />
        </Panel>
      </div>
    </PageFrame>
  );
}

type AppModel = {
  watchlist: WatchItem[];
  opportunities: Opportunity[];
  holdings: Holding[];
  filings: Filing[];
  traderPortfolios: TraderPortfolio[];
  traderFilingCards: TraderFilingCard[];
  calendar: CalendarEvent[];
  healthRows: HealthRow[];
  portfolioValue: number;
  sectors: SummaryItem[];
  setupRows: SummaryItem[];
  liquidityRows: SummaryItem[];
  correlationRows: SummaryItem[];
  valuationRows: SummaryItem[];
  technicalRows: SummaryItem[];
  signalSources: SignalSourcePanel[];
  memoRows: RowRecord[];
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

function buildModel(data: PanelData): AppModel {
  const watchlist = buildWatchlist(rows(data.quotes));
  const opportunities = buildOpportunities(rows(data.opportunitiesRanked), rows(data.signals), rows(data.candidates));
  const holdings = buildHoldings(rows(data.portfolio));
  const filings = buildFilings(rows(data.disclosures));
  const traderPortfolios = buildTraderPortfolios(rows(data.disclosures));
  const calendar = buildCalendar(rows(data.catalysts), rows(data.earnings));
  const healthRows = buildHealthRows(rows(data.sourceHealth), rows(data.providerRuns));
  const portfolioValue = holdings.reduce((total, holding) => total + holding.marketValue, 0);
  const latestHealthCheck = newestDateLabel(healthRows.map((row) => row.freshness));
  return {
    watchlist,
    opportunities,
    holdings,
    filings,
    traderPortfolios,
    traderFilingCards: buildTraderFilingCards(filings),
    calendar,
    healthRows,
    portfolioValue,
    sectors: buildSectorRows(rows(data.screener)),
    setupRows: buildSetupRows(rows(data.sepa), rows(data.liquidity)),
    liquidityRows: buildLiquidityRows(rows(data.liquidity)),
    correlationRows: buildCorrelationRows(rows(data.correlations)),
    valuationRows: buildValuationRows(rows(data.valuations)),
    technicalRows: buildTechnicalRows(rows(data.technicals)),
    signalSources: buildSignalSourcePanels(data),
    memoRows: rows(data.memos),
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

function buildWatchlist(quoteRows: RowRecord[]): WatchItem[] {
  return quoteRows.slice(0, 8).map((row) => ({
    symbol: stringField(row, ["symbol", "ticker"]).toUpperCase(),
    price: formatRawPrice(row.price ?? row.close ?? row.last),
    change: numberField(row, ["change_pct", "percent_change", "change"], 0),
  })).filter((item) => item.symbol);
}

function buildOpportunities(rankedRows: RowRecord[], signalRows: RowRecord[], candidateRows: RowRecord[]): Opportunity[] {
  const sourceRows = rankedRows.length ? rankedRows : signalRows.length ? signalRows : candidateRows;
  return sourceRows.slice(0, 25).map((row, index) => ({
    rank: Math.round(numberField(row, ["rank"], index + 1)),
    ticker: stringField(row, ["symbol", "ticker", "security", "name"]).toUpperCase() || `ITEM-${index + 1}`,
    name: stringField(row, ["name"]) || stringField(row, ["symbol", "ticker"]) || `Item ${index + 1}`,
    assetClass: stringField(row, ["asset_class"]) || "unknown",
    category: stringField(row, ["category"]) || "uncategorized",
    score: Math.round(numberField(row, ["score", "final_score"], 60)),
    grade: stringField(row, ["signal_grade", "grade"]) || gradeFromScore(numberField(row, ["score", "final_score"], 60)),
    confidence: confidenceValue(row),
    decision: normalizeDecision(stringField(row, ["decision", "action", "recommendation", "status"]) || "Watch"),
    whyNow: stringField(row, ["why_now", "rationale", "summary", "notes", "thesis"]) || "Evidence recently updated",
    nextAction: stringField(row, ["next_action", "action_required", "next_step"]) || "Review setup",
    invalidation: stringField(row, ["invalidation", "invalidates_if", "risk", "bear_case"]) || "No explicit invalidation in source row.",
    freshness: stringField(row, ["freshness", "source_freshness", "updated_at", "as_of", "run_date"]) || "recent",
    tags: ["T", "C", "H"].slice(0, 1 + (index % 3)),
    components: componentRows(row),
    evidenceCount: Math.round(numberField(row, ["evidence_count", "source_count"], 0)),
  }));
}

function buildHoldings(portfolioRows: RowRecord[]): Holding[] {
  return portfolioRows.slice(0, 20).map((row) => ({
    ticker: stringField(row, ["ticker", "symbol", "name"]).toUpperCase() || "UNKNOWN",
    weight: numberField(row, ["weight", "portfolio_weight"], 0),
    marketValue: numberField(row, ["market_value", "value", "position"], 0),
    averageCost: numberField(row, ["cost_basis", "average_cost", "avg_cost"], 0),
    purchaseDate: stringField(row, ["purchase_date"]) || "",
    holdingDays: numberField(row, ["holding_days"], 0),
    taxLotTerm: normalizeTaxLotTerm(stringField(row, ["tax_lot_term"])),
    unrealizedPnl: numberField(row, ["pnl", "unrealized_pnl", "gain_loss"], 0),
    unrealizedPnlPct: numberField(row, ["unrealized_pnl_pct"], 0),
    signal: normalizeDecision(stringField(row, ["signal", "thesis_status", "decision"]) || "Hold"),
    action: stringField(row, ["action", "next_action"]) || "Hold",
  })).filter((row) => row.ticker !== "UNKNOWN");
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
  return disclosureRows
    .map((row) => {
      const raw = objectField(row.raw);
      const sourceType = stringField(row, ["source_type"]) || stringField(raw, ["source_type"]);
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
      };
    })
    .filter((portfolio): portfolio is TraderPortfolio => Boolean(portfolio))
    .sort((a, b) => b.totalValue - a.totalValue);
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
    ["thesis", "Thesis / Memos", [...rows(data.theses), ...rows(data.memos)], ["conviction", "score"]],
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
  const label = symbol;
  const value = Number.isFinite(score)
    ? (Math.abs(score) <= 1 ? formatPct(score * 100) : Math.round(score).toString())
    : titleLabel(stringField(row, ["grade", "verdict", "status", "event_type"]) || "Loaded");
  return {
    label,
    value,
    caption: displayValue(row.caption ?? row.summary ?? row.title ?? row.stage ?? row.method ?? row.source ?? row.event_date ?? row.filed_date),
    tone: Number.isFinite(score) ? score >= 0 ? "good" : "bad" : "info",
    symbol,
  };
}

function buildCalendar(catalystRows: RowRecord[], earningsRows: RowRecord[]): CalendarEvent[] {
  const combined = [...catalystRows, ...earningsRows].slice(0, 15);
  return combined.map((row) => {
    const rawDate = stringField(row, ["event_date", "date", "due_date", "published_at"]);
    const parsed = rawDate ? new Date(rawDate) : null;
    const symbol = stringField(row, ["symbol", "ticker"]);
    const eventType = stringField(row, ["event_type", "type", "title"]) || "event";
    return {
      date: parsed && !Number.isNaN(parsed.getTime()) ? parsed.getDate() : 0,
      dateText: parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "-",
      monthLabel: parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleDateString(undefined, { month: "long", year: "numeric" }) : "Source Calendar",
      label: [symbol, eventType].filter(Boolean).join(" ") || "Market Event",
      type: calendarType(eventType),
    };
  }).filter((event) => event.date > 0);
}

function buildHealthRows(sourceRows: RowRecord[], providerRows: RowRecord[]): HealthRow[] {
  const rowsToMap = sourceRows.length ? sourceRows : providerRows;
  return rowsToMap.slice(0, 12).map((row) => {
    const status = stringField(row, ["status"]) || "Healthy";
    return {
      provider: stringField(row, ["source", "provider", "capability"]) || "Provider",
      status: status.toLowerCase().includes("degrad") ? "Degraded" : status.toLowerCase().includes("warn") ? "Warning" : "Healthy",
      freshness: stringField(row, ["checked_at", "finished_at", "freshness"]) || "recent",
      lastRun: stringField(row, ["detail"]) || "OK",
      sourceUrl: stringField(row, ["source_url", "source"]) || stringField(row, ["provider"]) || "local",
    };
  });
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

function buildLiquidityRows(liquidityRows: RowRecord[]): SummaryItem[] {
  return liquidityRows.slice(0, 9).map((row) => {
    const symbol = stringField(row, ["symbol"]).toUpperCase();
    return {
      label: symbol || "Liquidity",
      value: titleLabel(stringField(row, ["grade"]) || "Unknown"),
      caption: `${formatMoney(numberField(row, ["avg_dollar_volume"], 0))} ADV`,
      tone: stringField(row, ["grade"]).includes("high") ? "good" : "info",
      symbol,
    };
  });
}

function buildCorrelationRows(correlationRows: RowRecord[]): SummaryItem[] {
  return correlationRows.slice(0, 9).map((row) => {
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
    value: holding.weight ? `${holding.weight.toFixed(1)}%` : formatMoney(holding.marketValue),
    caption: `${holding.taxLotTerm} · ${formatMoney(holding.unrealizedPnl)} P/L`,
    tone: holding.unrealizedPnl >= 0 ? "good" : "bad",
    symbol: holding.ticker,
  }));
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

function PageFrame({ eyebrow, title, subtitle, action, children }: { eyebrow?: string; title: string; subtitle?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="page-frame">
      <header className="page-header">
        <div>
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          <h1>{title}</h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

function Panel({ title, children, className = "", headerAction }: { title: string; children: ReactNode; className?: string; headerAction?: ReactNode }) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <h2>{title}</h2>
        {headerAction && <span>{headerAction}</span>}
      </header>
      {children}
    </section>
  );
}

function SourceNotice({ items }: { items: Array<[string, DataSourceState]> }) {
  return (
    <section className="source-notice" aria-label="Data source status">
      {items.map(([label, state]) => (
        <span key={label}>
          {label}
          <SourcePill state={state} />
        </span>
      ))}
    </section>
  );
}

function SourcePill({ state }: { state: DataSourceState }) {
  const label = state === "live" ? "Rows loaded" : "No rows";
  return <i className={`source-pill ${state}`}>{label}</i>;
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
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
        <MetricBadge label="Confidence" value={`${opportunity.confidence}%`} tone={opportunity.confidence >= 70 ? "good" : "warn"} />
        <DecisionBadge value={opportunity.decision} />
      </div>
      <div className="top-opportunity-copy">
        <p>{opportunity.whyNow}</p>
        <small>{opportunity.nextAction}</small>
      </div>
    </section>
  );
}

function OpportunityTable({ rows, compact = false, onOpenTicker }: { rows: Opportunity[]; compact?: boolean; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No opportunities" detail="No signal or candidate rows matched this view." />;
  }
  return (
    <div className="table-wrap">
      <table className="desk-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Ticker</th>
            <th>Score</th>
            <th>Grade</th>
            <th>Conf.</th>
            <th>Decision</th>
            <th>Why Now</th>
            <th>Next Action</th>
            <th>Freshness</th>
            {!compact && <th>Top Evidence</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.rank}-${row.ticker}`}>
              <td className="rank">{row.rank}</td>
              <td><button className="ticker-link" type="button" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}</button></td>
              <td>{row.score}</td>
              <td>{row.grade}</td>
              <td>{row.confidence}%</td>
              <td><DecisionBadge value={row.decision} /></td>
              <td className="clip">{row.whyNow}</td>
              <td className="clip">{row.nextAction}</td>
              <td>{row.freshness}</td>
              {!compact && <td><TagRow tags={row.tags} /></td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TraderPortfolioHero({ portfolio }: { portfolio: TraderPortfolio }) {
  return (
    <section className="trader-hero">
      <div className="trader-hero-main">
        <span>{portfolio.category}</span>
        <h2>{portfolio.investor}</h2>
        <p>{portfolio.description}</p>
        <small>Updated {formatDateLabel(portfolio.updated)}</small>
      </div>
      <div className="trader-hero-metrics">
        <MetricBadge label="Current Value" value={formatMoney(portfolio.totalValue)} caption={`${formatMoney(portfolio.estimatedInvested)} cost basis`} tone="info" />
        <MetricBadge label="Open-Lot Return" value={formatPct(portfolio.performance)} caption="current holdings" tone={portfolio.performance >= 0 ? "good" : "bad"} />
        <MetricBadge label="Holdings" value={String(portfolio.holdingsCount)} caption={`${portfolio.riskLevel} risk`} />
        <MetricBadge label="Largest Weight" value={`${Math.round(portfolio.holdings[0]?.weight ?? 0)}%`} caption={portfolio.holdings[0]?.ticker ?? "No holdings"} tone="warn" />
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
  return (
    <button type="button" className={`trader-directory-row ${active ? "active" : ""}`} onClick={onSelect}>
      <div>
        <strong>{portfolio.investor}</strong>
        <small>{portfolio.holdingsCount} holdings</small>
      </div>
      <div>
        <i className={portfolio.performance >= 0 ? "good" : "bad"}>{formatPct(portfolio.performance)}</i>
        <small>{formatCompactMoney(portfolio.totalValue)}</small>
      </div>
    </button>
  );
}

function TraderPerformanceChart({ portfolio }: { portfolio: TraderPortfolio }) {
  const [windowKey, setWindowKey] = useState<"1Y" | "3Y" | "5Y" | "ALL">("3Y");
  const sourcePoints = portfolio.history.length ? portfolio.history : [{ date: portfolio.updated, value: portfolio.totalValue, costBasis: portfolio.estimatedInvested, performance: portfolio.performance }];
  const chartData = filterHistoryWindow(sourcePoints, windowKey).map((point) => ({
    date: point.date,
    performance: Number(point.performance.toFixed(2)),
    value: point.value,
    costBasis: point.costBasis,
  }));
  const last = chartData[chartData.length - 1];
  return (
    <div className="trader-performance">
      <div className="chart-toolbar">
        <div>
          <strong>{formatPct(portfolio.performance)}</strong>
          <span>Open-lot return on current reconstructed holdings</span>
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
              tickFormatter={(value) => `${Number(value).toFixed(0)}%`}
              width={52}
              domain={["dataMin - 5", "dataMax + 5"]}
              axisLine={{ stroke: "#223141" }}
              tickLine={{ stroke: "#223141" }}
            />
            <Tooltip
              cursor={{ stroke: "#68a8ff", strokeDasharray: "4 4" }}
              contentStyle={{ background: "#0b141e", border: "1px solid #223141", borderRadius: 6, color: "#e8f1f8" }}
              labelFormatter={(label) => formatDateLabel(String(label))}
              formatter={(value, name) => [name === "performance" ? formatPct(Number(value)) : formatMoney(Number(value)), titleLabel(String(name))]}
            />
            <Line type="monotone" dataKey="performance" name="open-lot return" stroke="#43e58f" strokeWidth={2.4} dot={false} activeDot={{ r: 4 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="performance-compare">
        <MetricBadge label="Portfolio" value={formatPct(portfolio.performance)} tone={portfolio.performance >= 0 ? "good" : "bad"} />
        <MetricBadge label="Latest Value" value={formatMoney(last?.value ?? portfolio.totalValue)} caption={last ? formatDateLabel(last.date) : formatDateLabel(portfolio.updated)} />
        <MetricBadge label="Cost Basis" value={formatMoney(last?.costBasis ?? portfolio.estimatedInvested)} caption={`${chartData.length} visible points`} />
      </div>
    </div>
  );
}

function TraderDistribution({ holdings }: { holdings: TraderPortfolioHolding[] }) {
  const top = holdings.slice(0, 7);
  const restWeight = holdings.slice(7).reduce((total, holding) => total + holding.weight, 0);
  const data = [
    ...top.map((holding) => ({ name: holding.ticker, value: Number(holding.weight.toFixed(2)), marketValue: holding.marketValue })),
    ...(restWeight > 0 ? [{ name: "OTHER", value: Number(restWeight.toFixed(2)), marketValue: holdings.slice(7).reduce((total, holding) => total + holding.marketValue, 0) }] : []),
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
              innerRadius="54%"
              outerRadius="82%"
              paddingAngle={2}
              cornerRadius={2}
              stroke="#07111b"
              strokeWidth={3}
              isAnimationActive={false}
            >
              {data.map((entry, index) => <Cell key={entry.name} fill={CHART_COLORS[index % CHART_COLORS.length]} stroke="#07111b" strokeWidth={3} />)}
            </Pie>
            <Tooltip
              contentStyle={{ background: "#0b141e", border: "1px solid #223141", borderRadius: 6, color: "#e8f1f8" }}
              formatter={(value, name, item) => [
                `${Number(value).toFixed(1)}% · ${formatMoney(Number((item.payload as { marketValue?: number }).marketValue ?? 0))}`,
                String(name),
              ]}
            />
          </PieChart>
        </ResponsiveContainer>
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
    traderHoldingColumn.accessor("ticker", {
      header: "Ticker",
      cell: ({ row, getValue }) => <button className="ticker-link" type="button" onClick={() => onOpenTicker(row.original.ticker)}>{String(getValue())}</button>,
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
      cell: ({ getValue }) => formatMoney(Number(getValue())),
    }),
    traderHoldingColumn.accessor("costBasis", {
      header: "Cost Basis",
      cell: ({ getValue }) => formatMoney(Number(getValue())),
    }),
    traderHoldingColumn.accessor("unrealizedPnl", {
      header: "Open P/L",
      cell: ({ getValue }) => {
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
      header: "Ticker",
      cell: ({ row, getValue }) => <button className="ticker-link" type="button" onClick={() => onOpenTicker(row.original.symbol)}>{String(getValue())}</button>,
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
      cell: ({ getValue }) => formatMoney(Number(getValue())),
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
      <div className="table-wrap">
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
      </div>
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
    studies: "MASimple@tv-basicstudies\u001ERSI@tv-basicstudies\u001EMACD@tv-basicstudies",
  });
  return (
    <div className="tradingview-frame">
      <iframe
        title={`${symbol} TradingView chart`}
        src={`https://s.tradingview.com/widgetembed/?${params.toString()}`}
        loading="lazy"
        referrerPolicy="no-referrer-when-downgrade"
      />
      <small>Daily candles with moving average, RSI, and MACD overlays from TradingView.</small>
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
  if (!holdings.length) {
    return <EmptyState title="No holdings loaded" detail="Add your real positions with the form on this page." />;
  }
  const remove = async (symbol: string) => {
    await deletePortfolioPosition(symbol);
    await onDelete();
  };
  return (
    <div className="table-wrap">
      <table className="desk-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Weight</th>
            <th>Market Value</th>
            <th>Avg Cost</th>
            <th>Purchase Date</th>
            <th>Term</th>
            <th>Unreal P/L</th>
            <th>Signal</th>
            <th>Action</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((holding) => (
            <tr key={holding.ticker}>
              <td><button className="ticker-link" type="button" onClick={() => onOpenTicker(holding.ticker)}>{holding.ticker}</button></td>
              <td>{holding.weight ? `${holding.weight.toFixed(1)}%` : "-"}</td>
              <td>{formatMoney(holding.marketValue)}</td>
              <td>{formatMoney(holding.averageCost)}</td>
              <td>{holding.purchaseDate || "Not set"}</td>
              <td>{holding.taxLotTerm}{holding.holdingDays ? ` (${holding.holdingDays}d)` : ""}</td>
              <td className={holding.unrealizedPnl >= 0 ? "positive" : "negative"}>{formatMoney(holding.unrealizedPnl)} · {formatPct(holding.unrealizedPnlPct)}</td>
              <td><DecisionBadge value={holding.signal} /></td>
              <td>{holding.action}</td>
              <td><button className="text-link" type="button" onClick={() => void remove(holding.ticker)}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FilingsTable({ rows, onOpenTicker }: { rows: Filing[]; onOpenTicker: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No filing rows loaded" detail="13F/disclosure tables are empty for this run." />;
  }
  return (
    <div className="table-wrap">
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
    </div>
  );
}

function HealthTable({ rows }: { rows: HealthRow[] }) {
  if (!rows.length) {
    return <EmptyState title="No source-health rows" detail="Run provider/source health jobs to populate this page." />;
  }
  return (
    <div className="table-wrap">
      <table className="desk-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Status</th>
            <th>Freshness</th>
            <th>Last Run</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.provider}>
              <td>{row.provider}</td>
              <td><StatusDot status={row.status} /></td>
              <td>{row.freshness}</td>
              <td>{row.lastRun}</td>
              <td>{row.sourceUrl}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
      {events.map((event) => {
        const symbol = event.label.split(" ")[0].replace(/[^A-Z-]/g, "");
        return (
          <button key={`${event.date}-${event.label}`} type="button" onClick={() => symbol && onOpenTicker?.(symbol)}>
            <i className={event.type} />
            <span>{event.label}</span>
            <small>{event.dateText}</small>
          </button>
        );
      })}
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

function MetricStrip({ metrics }: { metrics: Array<[string, string, string, string]> }) {
  return (
    <section className="metric-strip">
      {metrics.map(([label, value, caption, tone]) => (
        <div key={label} className={`metric-box ${tone}`}>
          <span>{label}</span>
          <strong>{value}</strong>
          <small>{caption}</small>
        </div>
      ))}
    </section>
  );
}

function MetricBadge({ label, value, caption, tone = "info" }: { label: string; value: string; caption?: string; tone?: Tone }) {
  return (
    <div className={`metric-badge ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {caption && <small>{caption}</small>}
    </div>
  );
}

function DecisionBadge({ value }: { value: string }) {
  return <span className={`decision-badge ${toneClass(value)}`}>{value}</span>;
}

function StatusDot({ status }: { status: HealthRow["status"] }) {
  return <span className={`status-dot ${status.toLowerCase()}`}><i />{status}</span>;
}

function TabBar({ tabs, active, onSelect }: { tabs: string[]; active?: string; onSelect?: (tab: string) => void }) {
  return (
    <div className="tab-bar">
      {tabs.map((tab, index) => <button key={tab} className={(active ?? tabs[0]) === tab || (!active && index === 0) ? "active" : ""} type="button" onClick={() => onSelect?.(tab)}>{tab}</button>)}
    </div>
  );
}

function SegmentedControl({ options, value, onChange }: { options: string[]; value: string; onChange: (value: string) => void }) {
  return (
    <div className="segmented-control">
      {options.map((option) => (
        <button key={option} className={value === option ? "active" : ""} type="button" onClick={() => onChange(option)}>{option}</button>
      ))}
    </div>
  );
}

function FilterRail({
  compact = false,
  decision = "",
  decisions = [],
  tickerQuery = "",
  minScore = 0,
  assetClass = "",
  assetClasses = [],
  minConfidence = 0,
  source = "",
  sources = [],
  investorQuery = "",
  onDecision,
  onTickerQuery,
  onMinScore,
  onAssetClass,
  onMinConfidence,
  onSource,
  onInvestorQuery,
  onReset,
}: {
  compact?: boolean;
  decision?: string;
  decisions?: string[];
  tickerQuery?: string;
  minScore?: number;
  assetClass?: string;
  assetClasses?: string[];
  minConfidence?: number;
  source?: string;
  sources?: string[];
  investorQuery?: string;
  onDecision?: (value: string) => void;
  onTickerQuery?: (value: string) => void;
  onMinScore?: (value: number) => void;
  onAssetClass?: (value: string) => void;
  onMinConfidence?: (value: number) => void;
  onSource?: (value: string) => void;
  onInvestorQuery?: (value: string) => void;
  onReset?: () => void;
}) {
  return (
    <aside className="filter-rail">
      <div className="rail-title">
        <strong>Filters</strong>
        <button type="button" onClick={onReset}>Reset</button>
      </div>
      {(!compact || decisions.length > 0) && (
        <label>
          <span>Decision</span>
          <select value={decision} onChange={(event) => onDecision?.(event.target.value)}>
            <option value="">All</option>
            {decisions.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      )}
      <label>
        <span>Ticker</span>
        <input value={tickerQuery} onChange={(event) => onTickerQuery?.(event.target.value)} placeholder="Any" />
      </label>
      {compact && (
        <label>
          <span>Investor</span>
          <input value={investorQuery} onChange={(event) => onInvestorQuery?.(event.target.value)} placeholder="Any" />
        </label>
      )}
      {!compact && (
        <label>
          <span>Score Range</span>
          <input type="range" min="0" max="100" value={minScore} onChange={(event) => onMinScore?.(Number(event.target.value))} />
          <small>{minScore}+ minimum</small>
        </label>
      )}
      {!compact && (
        <label>
          <span>Asset</span>
          <select value={assetClass} onChange={(event) => onAssetClass?.(event.target.value)}>
            <option value="">All</option>
            {assetClasses.map((item) => <option key={item} value={item}>{titleLabel(item)}</option>)}
          </select>
        </label>
      )}
      {!compact && (
        <label>
          <span>Confidence</span>
          <input type="range" min="0" max="100" value={minConfidence} onChange={(event) => onMinConfidence?.(Number(event.target.value))} />
          <small>{minConfidence}+ minimum</small>
        </label>
      )}
      {!compact && (
        <label>
          <span>Signal Source</span>
          <select value={source} onChange={(event) => onSource?.(event.target.value)}>
            <option value="">All</option>
            {sources.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      )}
      <button className="primary-button" type="button" onClick={onReset}>Reset Filters</button>
    </aside>
  );
}

function CalendarMonth({ events, onOpenTicker }: { events: CalendarEvent[]; onOpenTicker: (symbol: string) => void }) {
  const cells = [
    ...[27, 28, 29, 30].map((day) => ({ day, muted: true })),
    ...Array.from({ length: 31 }, (_, index) => ({ day: index + 1, muted: false })),
  ];
  return (
    <div className="calendar-month">
      {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => <strong key={day}>{day}</strong>)}
      {cells.map((cell, index) => {
        const event = cell.muted ? undefined : events.find((item) => item.date === cell.day);
        const symbol = event?.label.split(" ")[0].replace(/[^A-Z-]/g, "") ?? "";
        return (
          <button key={`${cell.day}-${index}`} className={`${cell.day === 20 && !cell.muted ? "today" : ""} ${cell.muted ? "muted-day" : ""}`} type="button" onClick={() => symbol && onOpenTicker(symbol)}>
            <span>{cell.day}</span>
            {event && <em className={event.type}>{event.label}</em>}
          </button>
        );
      })}
    </div>
  );
}

function GenericRows({ rows: sourceRows, emptyTitle, emptyDetail, onOpenTicker }: { rows: RowRecord[]; emptyTitle: string; emptyDetail: string; onOpenTicker: (symbol: string) => void }) {
  if (!sourceRows.length) {
    return <EmptyState title={emptyTitle} detail={emptyDetail} />;
  }
  const items = sourceRows.slice(0, 6).map((row) => ({
    symbol: symbolFromRow(row),
    text: displayValue(
      row.title ??
      row.thesis_summary ??
      row.report_markdown ??
      row.event ??
      row.event_type ??
      row.summary ??
      row.notes ??
      row.status ??
      row.detail ??
      row.form_type ??
      row.metrics ??
      row.estimates ??
      row.report_json ??
      row.source ??
      JSON.stringify(row),
    ),
  }));
  return (
    <div className="generic-list">
      {items.map((item) => (
        <button key={item.text} type="button" onClick={() => item.symbol && onOpenTicker(item.symbol)}>
          <span>{item.symbol || "MARKET"}</span>
          <p>{item.text}</p>
        </button>
      ))}
    </div>
  );
}

function SummaryList({ rows, onOpenTicker }: { rows: SummaryItem[]; onOpenTicker?: (symbol: string) => void }) {
  if (!rows.length) {
    return <EmptyState title="No source rows" detail="The backing source table has no rows for this panel." />;
  }
  return (
    <div className="summary-list">
      {rows.map((row) => (
        <button key={`${row.label}-${row.value}`} type="button" disabled={!row.symbol || !onOpenTicker} onClick={() => row.symbol && onOpenTicker?.(row.symbol)}>
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
        <div><HeartPulse size={15} /><strong>All clear</strong><small>No degraded or warning source-health rows are loaded.</small></div>
      </div>
    );
  }
  return (
    <div className="alert-list">
      {alerts.map((row) => (
        <div key={row.provider}><AlertTriangle size={15} /><strong>{row.provider}</strong><small>{row.status}: {row.lastRun}</small></div>
      ))}
      <TextLink>View all alerts</TextLink>
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
        <div key={row.provider}>
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
    return <EmptyState title="No freshness rows" detail="Source-health checks have not produced rows." />;
  }
  return (
    <div className="freshness-grid">
      {rows.slice(0, 5).map((row) => (
        <div key={row.provider}>
          <span>{row.provider}</span>
          {Array.from({ length: 6 }, (_, index) => <i key={index} className={row.status === "Healthy" || index < 4 ? "good" : "warn"} />)}
        </div>
      ))}
    </div>
  );
}

function TextLink({ children }: { children: ReactNode }) {
  return <button className="text-link" type="button">{children} <ChevronRight size={13} /></button>;
}

function GhostButton({ children, disabled = false, title }: { children: ReactNode; disabled?: boolean; title?: string }) {
  return <button className="ghost-button" type="button" disabled={disabled} title={title}>{children}</button>;
}

function IconButton({ children, label, onClick }: { children: ReactNode; label: string; onClick?: () => void }) {
  return <button className="icon-button" type="button" aria-label={label} title={label} onClick={onClick}>{children}</button>;
}

function TagRow({ tags }: { tags: string[] }) {
  return <span className="tag-row">{tags.map((tag) => <i key={tag}>{tag}</i>)}</span>;
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
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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
  if (normalized.includes("accumulate") || normalized.includes("buy") || normalized.includes("success")) return "good";
  if (normalized.includes("avoid") || normalized.includes("degraded")) return "bad";
  if (normalized.includes("watch") || normalized.includes("warning")) return "warn";
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
  const date = new Date(value);
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

export default App;
