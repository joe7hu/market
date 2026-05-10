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
import { deletePortfolioPosition, loadPanelData, loadTicker, savePortfolioPosition } from "./api";
import type { PanelData, RowRecord, TickerPayload } from "./types";
import { displayValue, rows, symbolFromRow } from "./utils";

const initialData: PanelData = {
  dashboard: {},
  signals: { rows: [], count: 0 },
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
        {activePage === "research" && <ResearchPage data={data} onOpenTicker={openTicker} />}
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
      subtitle={lastRefresh ? `Refreshed ${lastRefresh.toLocaleTimeString()}` : loading ? "Loading DuckDB data..." : "No data loaded"}
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
        <Panel className="span-7" title="Price & Setup">
          <SummaryList rows={[
            { label: "Latest Quote", value: quote?.price ?? "-", caption: quote ? `Move ${formatPct(quote.change)}` : "No quote row", tone: quote ? "info" : "warn" },
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
          ["Total Value", hasHoldings ? formatMoney(model.portfolioValue) : "Awaiting positions", hasHoldings ? "DuckDB portfolio rows" : "Manual entry below", hasHoldings ? "info" : "muted"],
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
          <BulletList tone={model.holdings.length ? "warn" : "info"} items={model.holdings.length ? ["Portfolio rows loaded", "Review concentration against signal strength", "Liquidity check available from source tables"] : ["Manual position entry writes to local DuckDB", "Use ticker, share count, and average cost", "No broker credentials or account data are required"]} />
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
  const filtered = model.opportunities.filter((item) => {
    const decisionMatches = !decision || item.decision === decision;
    const tickerMatches = !tickerQuery || item.ticker.includes(tickerQuery.trim().toUpperCase());
    return decisionMatches && tickerMatches && item.score >= minScore;
  });
  const decisions = Array.from(new Set(model.opportunities.map((item) => item.decision)));
  return (
    <div className="split-page">
      <FilterRail
        decision={decision}
        decisions={decisions}
        tickerQuery={tickerQuery}
        minScore={minScore}
        onDecision={setDecision}
        onTickerQuery={setTickerQuery}
        onMinScore={setMinScore}
        onReset={() => {
          setDecision("");
          setTickerQuery("");
          setMinScore(0);
        }}
      />
      <PageFrame
        title="Opportunities"
        subtitle={`${filtered.length} of ${model.opportunities.length} results`}
        action={
          <GhostButton disabled title="Saved views are not persisted yet">
            <Database size={14} /> Current View
          </GhostButton>
        }
      >
        <SourceNotice items={[["Signals", model.sources.opportunities], ["Quotes", model.sources.watchlist]]} />
        <Panel title="Ranked Screen">
          <OpportunityTable rows={filtered} onOpenTicker={onOpenTicker} />
          <button className="load-more" type="button">Load more</button>
        </Panel>
      </PageFrame>
    </div>
  );
}

function FilingsPage({ model, onOpenTicker }: { model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const selected = model.filings[0];
  return (
    <div className="split-page">
      <FilterRail compact />
      <PageFrame title="Trader Filings" subtitle="Track notable investors">
        <SourceNotice items={[["Disclosures", model.sources.filings]]} />
        {model.filings.length > 0 && <TabBar tabs={Array.from(new Set(model.filings.map((filing) => filing.investor))).slice(0, 6)} />}
        <div className="filings-grid">
          <Panel className="span-8" title="Recent Filings">
            <FilingsTable rows={model.filings} onOpenTicker={onOpenTicker} />
          </Panel>
          <Panel className="span-4" title={selected?.investor ?? "Filing Detail"} headerAction={<X size={14} />}>
            {selected ? (
              <>
                <DetailRows rows={[["Form", "13F-HR"], ["Event Date", selected.event], ["Filed Date", selected.filed], ["Holdings Count", "Source row"], ["Holdings Value", formatMoney(selected.value)]]} />
                <div className="holding-detail">
                  <h3>Holding Detail</h3>
                  <strong>{selected.security}</strong>
                  <DetailRows rows={[["Action", selected.action], ["Shares", formatNumber(selected.shares)], ["Value (K)", formatMoney(selected.value)], ["Ticker", selected.ticker || "CUSIP-only 13F row"]]} />
                  <button className="primary-button" type="button" disabled={!selected.ticker} onClick={() => selected.ticker && onOpenTicker(selected.ticker)}>View Ticker</button>
                </div>
              </>
            ) : <EmptyState title="No filing selected" detail="Disclosure import has no rows for the current database." />}
          </Panel>
          <Panel className="span-8" title="About 13F Filings">
            <p className="panel-copy">13F filings are delayed quarterly position disclosures. They do not prove live fund ownership; they are useful for pattern recognition, optionality, and holdings map review.</p>
          </Panel>
        </div>
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
    "Evidence Stack": ["signals", "sepa", "liquidity", "correlations", "valuations"],
    Fundamentals: ["fundamentals"],
    Estimates: ["analyst_estimates", "earnings"],
    Financials: ["fundamentals", "valuations"],
    News: ["news"],
    Filings: ["disclosures"],
    Memos: ["memos", "theses"],
  };
  const fallbackByTab: Record<string, RowRecord[]> = {
    "Evidence Stack": [...rows(data.signals), ...rows(data.sepa), ...rows(data.liquidity), ...rows(data.valuations)],
    Fundamentals: rows(data.fundamentals),
    Estimates: [...rows(data.analystEstimates), ...rows(data.earnings)],
    Financials: [...rows(data.fundamentals), ...rows(data.valuations)],
    News: rows(data.news),
    Filings: rows(data.disclosures),
    Memos: [...rows(data.memos), ...rows(data.theses)],
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

function ResearchPage({ data, onOpenTicker }: { data: PanelData; onOpenTicker: (symbol: string) => void }) {
  const thesisRows = rows(data.theses);
  const newsRows = rows(data.news);
  const fundamentalsRows = rows(data.fundamentals);
  const memoRows = rows(data.memos);
  const signalRows = rows(data.signals);
  return (
    <PageFrame title="Research" subtitle="Evidence, theses, memos, and source-backed notes">
      <SourceNotice items={[["Theses", thesisRows.length ? "live" : "empty"], ["News", newsRows.length ? "live" : "empty"], ["Fundamentals", fundamentalsRows.length ? "live" : "empty"]]} />
      <div className="research-grid">
        <Panel className="span-6" title="Active Thesis Tracker">
          <GenericRows rows={thesisRows} emptyTitle="No thesis rows" emptyDetail="Thesis tracker is empty for this database." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-6" title="Evidence Feed">
          <GenericRows rows={newsRows} emptyTitle="No evidence feed rows" emptyDetail="News/evidence rows are empty." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Fundamental Watch">
          <GenericRows rows={fundamentalsRows} emptyTitle="No fundamental rows" emptyDetail="Run fundamentals ingestion to fill this panel." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Invalidation Queue">
          <GenericRows rows={signalRows.map((row) => ({ ...row, title: row.invalidation ?? row.next_action ?? row.why_now }))} emptyTitle="No invalidation rows" emptyDetail="Signals have not produced invalidation text." onOpenTicker={onOpenTicker} />
        </Panel>
        <Panel className="span-4" title="Memo Queue">
          <GenericRows rows={memoRows} emptyTitle="No memo rows" emptyDetail="Run research_candidate or weekly_portfolio_review to create memos." onOpenTicker={onOpenTicker} />
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
  calendar: CalendarEvent[];
  healthRows: HealthRow[];
  portfolioValue: number;
  sectors: SummaryItem[];
  setupRows: SummaryItem[];
  liquidityRows: SummaryItem[];
  correlationRows: SummaryItem[];
  valuationRows: SummaryItem[];
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
  const opportunities = buildOpportunities(rows(data.signals), rows(data.candidates));
  const holdings = buildHoldings(rows(data.portfolio));
  const filings = buildFilings(rows(data.disclosures));
  const calendar = buildCalendar(rows(data.catalysts), rows(data.earnings));
  const healthRows = buildHealthRows(rows(data.sourceHealth), rows(data.providerRuns));
  const portfolioValue = holdings.reduce((total, holding) => total + holding.marketValue, 0);
  const latestHealthCheck = newestDateLabel(healthRows.map((row) => row.freshness));
  return {
    watchlist,
    opportunities,
    holdings,
    filings,
    calendar,
    healthRows,
    portfolioValue,
    sectors: buildSectorRows(rows(data.screener)),
    setupRows: buildSetupRows(rows(data.sepa), rows(data.liquidity)),
    liquidityRows: buildLiquidityRows(rows(data.liquidity)),
    correlationRows: buildCorrelationRows(rows(data.correlations)),
    valuationRows: buildValuationRows(rows(data.valuations)),
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

function buildOpportunities(signalRows: RowRecord[], candidateRows: RowRecord[]): Opportunity[] {
  const sourceRows = signalRows.length ? signalRows : candidateRows;
  return sourceRows.slice(0, 25).map((row, index) => ({
    rank: index + 1,
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
    evidenceCount: Math.round(numberField(row, ["evidence_count"], 0)),
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
  const label = state === "live" ? "Live DuckDB" : "No rows";
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

function FilterRail({
  compact = false,
  decision = "",
  decisions = [],
  tickerQuery = "",
  minScore = 0,
  onDecision,
  onTickerQuery,
  onMinScore,
  onReset,
}: {
  compact?: boolean;
  decision?: string;
  decisions?: string[];
  tickerQuery?: string;
  minScore?: number;
  onDecision?: (value: string) => void;
  onTickerQuery?: (value: string) => void;
  onMinScore?: (value: number) => void;
  onReset?: () => void;
}) {
  return (
    <aside className="filter-rail">
      <div className="rail-title">
        <strong>Filters</strong>
        <button type="button" onClick={onReset}>Reset</button>
      </div>
      <label>
        <span>Decision</span>
        <select value={decision} onChange={(event) => onDecision?.(event.target.value)}>
          <option value="">All</option>
          {decisions.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>
        <span>Ticker</span>
        <input value={tickerQuery} onChange={(event) => onTickerQuery?.(event.target.value)} placeholder="Any" />
      </label>
      <label>
        <span>Score Range</span>
        <input type="range" min="0" max="100" value={minScore} onChange={(event) => onMinScore?.(Number(event.target.value))} />
        <small>{minScore}+ minimum</small>
      </label>
      {!compact && ["Asset", "Confidence", "Catalyst Window", "Liquidity Grade"].map((label) => (
        <label key={label}>
          <span>{label}</span>
          <select disabled value=""><option value="">Source-backed</option></select>
        </label>
      ))}
      <button className="primary-button" type="button" disabled>Filters apply live</button>
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
    return <EmptyState title="No source rows" detail="The backing DuckDB table has no rows for this panel." />;
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

function formatPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatMoney(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value > 1000 ? 0 : 2 });
}

function formatNumber(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString() : "-";
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
