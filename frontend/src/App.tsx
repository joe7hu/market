import {
  Activity,
  AlertCircle,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleDollarSign,
  ClipboardList,
  Database,
  Gauge,
  LineChart,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Target,
  UserRoundCog,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { loadPanelData, loadTicker } from "./api";
import type { ApiStatus, DashboardPayload, JsonValue, PanelData, RowRecord, TickerPayload } from "./types";
import { collectColumns, displayValue, fullDisplayValue, getMetric, rows, symbolFromRow, titleize } from "./utils";

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
  providerRuns: { rows: [], count: 0 },
  sourceHealth: { rows: [], count: 0 },
  settings: {},
  errors: {},
};

const candidateColumnOrder = [
  "symbol",
  "name",
  "asset_class",
  "category",
  "final_score",
  "decision",
  "run_date",
];

const signalColumnOrder = [
  "symbol",
  "score",
  "signal_grade",
  "confidence",
  "decision",
  "why_now",
  "next_action",
];

const portfolioColumnOrder = [
  "ticker",
  "symbol",
  "name",
  "shares",
  "quantity",
  "position",
  "weight",
  "cost_basis",
  "price",
  "market_value",
  "pnl",
  "thesis_status",
  "updated_at",
];

const sectionColumnOrder = [
  "ticker",
  "symbol",
  "title",
  "name",
  "status",
  "date",
  "due_date",
  "profile",
  "signal",
  "summary",
  "notes",
  "updated_at",
];

const fundamentalsColumnOrder = ["symbol", "period_end", "filing_date", "form_type", "metrics", "source_url"];
const quoteColumnOrder = ["symbol", "observed_at", "price", "change_pct", "change_abs", "currency", "source"];
const sepaColumnOrder = ["symbol", "as_of", "score", "stage", "verdict", "metrics", "checklist"];
const liquidityColumnOrder = ["symbol", "as_of", "grade", "avg_dollar_volume", "avg_daily_volume", "impact_1pct_adv_bps"];
const optionColumnOrder = ["symbol", "expiry", "dte", "contracts_count", "strike", "option_type", "mid", "iv", "delta"];
const newsColumnOrder = ["published_at", "provider", "title", "related_symbols", "link", "source"];
const valuationColumnOrder = ["symbol", "upside_pct", "fair_value", "diagnostics", "as_of", "method"];
const providerRunColumnOrder = ["provider", "capability", "finished_at", "status", "detail"];
const disclosureColumnOrder = [
  "source_type",
  "trader_name",
  "filer_name",
  "event_date",
  "filed_date",
  "action",
  "holdings_count",
  "holdings_value_thousands",
  "source_url",
];
const sourceHealthColumnOrder = ["source", "status", "checked_at", "detail", "source_url"];

const columnHelper = createColumnHelper<RowRecord>();

function App() {
  const [data, setData] = useState<PanelData>(initialData);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [tickerInput, setTickerInput] = useState("");
  const [ticker, setTicker] = useState<TickerPayload | null>(null);
  const [tickerLoading, setTickerLoading] = useState(false);
  const [tickerError, setTickerError] = useState("");
  const [activeSourceGroup, setActiveSourceGroup] = useState("Market");
  const [expandedSignal, setExpandedSignal] = useState("");

  const refresh = async () => {
    setLoading(true);
    const nextData = await loadPanelData();
    setData(nextData);
    setLastUpdated(new Date());
    setLoading(false);
    if (!selectedSymbol) {
      const firstSymbol = symbolFromRow(rows(nextData.candidates)[0] ?? rows(nextData.portfolio)[0]);
      if (firstSymbol) {
        setSelectedSymbol(firstSymbol);
        setTickerInput(firstSymbol);
      }
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (!selectedSymbol) {
      setTicker(null);
      return;
    }

    let cancelled = false;
    setTickerLoading(true);
    setTickerError("");
    loadTicker(selectedSymbol)
      .then((payload) => {
        if (!cancelled) {
          setTicker(payload);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setTicker(null);
          setTickerError(error instanceof Error ? error.message : "Ticker request failed");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setTickerLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [selectedSymbol]);

  const status = data.dashboard.status ?? data.settings.status ?? data.candidates.status;
  const signalRows = useMemo(() => buildSignalRows(rows(data.signals), rows(data.candidates)), [data.signals, data.candidates]);
  const candidateRows = rows(data.candidates);
  const portfolioRows = rows(data.portfolio);
  const thesisRows = rows(data.theses);
  const catalystRows = rows(data.catalysts);
  const twinRows = rows(data.traderTwins);
  const fundamentalRows = rows(data.fundamentals);
  const disclosureRows = rows(data.disclosures);
  const quoteRows = rows(data.quotes);
  const screenerRows = rows(data.screener);
  const optionExpiryRows = rows(data.optionsExpiries);
  const optionChainRows = rows(data.optionsChain);
  const newsRows = rows(data.news);
  const sepaRows = rows(data.sepa);
  const liquidityRows = rows(data.liquidity);
  const correlationRows = rows(data.correlations);
  const etfPremiumRows = rows(data.etfPremiums);
  const estimateRows = rows(data.analystEstimates);
  const earningsRows = rows(data.earnings);
  const valuationRows = rows(data.valuations);
  const providerRunRows = rows(data.providerRuns);
  const sourceHealthRows = rows(data.sourceHealth);
  const decisionSignals = buildDecisionSignals({
    signalRows,
    candidateRows,
    quoteRows,
    sepaRows,
    liquidityRows,
    valuationRows,
  });
  const signalGroups = buildSignalGroups(decisionSignals);
  const operationCards = buildOperationCards({ status, providerRunRows, sourceHealthRows });
  const tickerInsight = buildTickerInsight({
    symbol: selectedSymbol,
    decisionSignals,
    quoteRows,
    sepaRows,
    liquidityRows,
    valuationRows,
    thesisRows,
    catalystRows,
    disclosureRows,
  });
  const marketCharts = buildMarketCharts({
    signalRows,
    quoteRows,
    sepaRows,
    valuationRows,
  });
  const traderPortfolios = buildTraderPortfolios(disclosureRows);
  const sourceGroups: SourceGroup[] = [
    {
      name: "Market",
      count: quoteRows.length + screenerRows.length + newsRows.length,
      sections: [
        { title: "TradingView Quotes", icon: <Activity size={17} />, rows: quoteRows, preferredColumns: quoteColumnOrder },
        { title: "TradingView Screener", icon: <Gauge size={17} />, rows: screenerRows, preferredColumns: quoteColumnOrder },
        { title: "News", icon: <ClipboardList size={17} />, rows: newsRows, preferredColumns: newsColumnOrder },
      ],
    },
    {
      name: "Analysis",
      count: sepaRows.length + liquidityRows.length + correlationRows.length + valuationRows.length,
      sections: [
        { title: "SEPA Setups", icon: <Target size={17} />, rows: sepaRows, preferredColumns: sepaColumnOrder },
        { title: "Liquidity", icon: <Activity size={17} />, rows: liquidityRows, preferredColumns: liquidityColumnOrder },
        { title: "Correlations", icon: <Activity size={17} />, rows: correlationRows, preferredColumns: ["symbol", "as_of", "lookback_days", "peers", "metrics"] },
        { title: "Valuations", icon: <CircleDollarSign size={17} />, rows: valuationRows, preferredColumns: valuationColumnOrder },
      ],
    },
    {
      name: "Options",
      count: optionExpiryRows.length + optionChainRows.length,
      sections: [
        { title: "Options Expiries", icon: <CalendarClock size={17} />, rows: optionExpiryRows, preferredColumns: optionColumnOrder },
        { title: "Options Chain", icon: <CircleDollarSign size={17} />, rows: optionChainRows, preferredColumns: optionColumnOrder },
      ],
    },
    {
      name: "Fundamental",
      count: estimateRows.length + earningsRows.length + fundamentalRows.length + etfPremiumRows.length,
      sections: [
        { title: "Analyst Estimates", icon: <Database size={17} />, rows: estimateRows, preferredColumns: ["symbol", "as_of", "source", "estimates"] },
        { title: "Earnings", icon: <CalendarClock size={17} />, rows: earningsRows, preferredColumns: ["symbol", "event_date", "event_type", "source", "metrics"] },
        { title: "Fundamentals", icon: <Database size={17} />, rows: fundamentalRows, preferredColumns: fundamentalsColumnOrder },
        { title: "ETF Premiums", icon: <CircleDollarSign size={17} />, rows: etfPremiumRows, preferredColumns: ["symbol", "as_of", "market_price", "nav", "premium_pct", "source"] },
      ],
    },
    {
      name: "Workflow",
      count: thesisRows.length + catalystRows.length + providerRunRows.length + sourceHealthRows.length + twinRows.length,
      sections: [
        { title: "Thesis Tracker", icon: <ClipboardList size={17} />, rows: thesisRows, preferredColumns: sectionColumnOrder },
        { title: "Catalyst Calendar", icon: <CalendarClock size={17} />, rows: catalystRows, preferredColumns: sectionColumnOrder },
        { title: "Provider Runs", icon: <Database size={17} />, rows: providerRunRows, preferredColumns: providerRunColumnOrder },
        { title: "Source Health", icon: <Database size={17} />, rows: sourceHealthRows, preferredColumns: sourceHealthColumnOrder },
        { title: "Public Disclosures", icon: <ShieldCheck size={17} />, rows: disclosureRows, preferredColumns: disclosureColumnOrder },
        { title: "Trader Twins", icon: <UserRoundCog size={17} />, rows: twinRows, preferredColumns: sectionColumnOrder },
      ],
    },
  ];

  const submitTicker = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = tickerInput.trim().toUpperCase();
    if (normalized) {
      setSelectedSymbol(normalized);
      setTickerInput(normalized);
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Market intelligence</p>
          <h1>Decision Desk</h1>
          <p className="topbar-subtitle">
            A personal investment command center that turns watchlists, portfolio state, Arco evidence, catalysts, and free-source market data into a daily attention queue.
          </p>
        </div>
        <div className="topbar-actions">
          <StatusPill status={status} />
          <button className="icon-button" type="button" onClick={refresh} disabled={loading} title="Refresh data">
            <RefreshCw size={16} className={loading ? "spin" : ""} />
          </button>
        </div>
      </header>

      <StatusBanner status={status} errors={data.errors} />

      <section className="market-zone" aria-label="Market signals">
        <SectionIntro
          eyebrow="Market signals"
          title="Signal Command"
          detail="Ranked names, setup quality, exposure, catalysts, and valuation cues."
        />

        <section className="decision-cockpit" aria-label="Decision cockpit">
          <SignalHierarchy
            groups={signalGroups}
            selectedSymbol={selectedSymbol}
            expandedSignal={expandedSignal}
            onSelectSymbol={(symbol) => {
              setSelectedSymbol(symbol);
              setTickerInput(symbol);
            }}
            onExpandedSignal={setExpandedSignal}
          />
          <TickerInsightPanel insight={tickerInsight} />
        </section>

        <section className="market-evidence-grid" aria-label="Market evidence">
          <MarketChartsPanel charts={marketCharts} />
          <TraderPortfolioPanel portfolios={traderPortfolios} />
        </section>
      </section>

      <section className="operations-zone" aria-label="Service operation health">
        <SectionIntro
          eyebrow="Service operation health"
          title="Pipeline & Source Health"
          detail="Ingestion, provider run, and source readiness checks kept separate from market decisions."
        />
        <OperationHealthPanel cards={operationCards} sourceRows={sourceHealthRows} providerRows={providerRunRows} serviceSource={status?.source} />
      </section>

      <section className="workbench">
        <div className="table-stack">
          <SignalsPanel
            data={signalRows}
            preferredColumns={signalColumnOrder}
            source={rows(data.signals).length ? "/api/signals" : "candidate rows"}
            onSelectSymbol={(symbol) => {
              setSelectedSymbol(symbol);
              setTickerInput(symbol);
            }}
            selectedSymbol={selectedSymbol}
          />
          <DataTable
            title="Candidate Screen"
            icon={<Gauge size={18} />}
            data={candidateRows}
            preferredColumns={candidateColumnOrder}
            includeExtraColumns={false}
            onSelectSymbol={(symbol) => {
              setSelectedSymbol(symbol);
              setTickerInput(symbol);
            }}
            selectedSymbol={selectedSymbol}
          />
          <DataTable
            title="Portfolio"
            icon={<ShieldCheck size={18} />}
            data={portfolioRows}
            preferredColumns={portfolioColumnOrder}
            onSelectSymbol={(symbol) => {
              setSelectedSymbol(symbol);
              setTickerInput(symbol);
            }}
            selectedSymbol={selectedSymbol}
          />
        </div>

        <aside className="detail-rail">
          <TickerDetail
            input={tickerInput}
            onInput={setTickerInput}
            onSubmit={submitTicker}
            selectedSymbol={selectedSymbol}
            ticker={ticker}
            loading={tickerLoading}
            error={tickerError}
          />
          <DashboardPreviews dashboard={data.dashboard} />
        </aside>
      </section>

      <SourceWorkspace groups={sourceGroups} activeGroup={activeSourceGroup} onActiveGroup={setActiveSourceGroup} />

      <section className="section-grid settings-band">
        <SettingsSection config={data.settings.config} integration={data.settings.integration} />
      </section>

      <footer className="footer-line">
        <span>{lastUpdated ? `Last refresh ${lastUpdated.toLocaleTimeString()}` : "Awaiting first refresh"}</span>
      </footer>
    </main>
  );
}

type SourceSection = {
  title: string;
  icon: ReactNode;
  rows: RowRecord[];
  preferredColumns: string[];
};

type SourceGroup = {
  name: string;
  count: number;
  sections: SourceSection[];
};

function buildSignalRows(signalRows: RowRecord[], candidateRows: RowRecord[]): RowRecord[] {
  const sourceRows = signalRows.length ? signalRows : candidateRows;
  return sourceRows.map((row) => ({
    symbol: signalField(row, ["symbol", "ticker", "security", "name"]),
    score: signalField(row, ["score", "final_score"]),
    signal_grade: signalField(row, ["signal_grade", "grade", "signal", "rating", "score"]),
    confidence: signalField(row, ["confidence", "confidence_score", "probability", "conviction"]),
    decision: signalField(row, ["decision", "action", "recommendation", "stance", "status"]),
    why_now: signalField(row, ["why_now", "rationale", "summary", "notes", "catalyst", "thesis"]),
    evidence_count: evidenceCount(row),
    invalidation: signalField(row, ["invalidation", "invalidates_if", "risk", "bear_case", "stop_loss"]),
    next_action: signalField(row, ["next_action", "action_required", "follow_up", "next_step"]),
    source_freshness: signalField(row, ["source_freshness", "freshness", "latest_source_at", "updated_at", "as_of", "date"]),
  }));
}

function signalField(row: RowRecord, keys: string[]): JsonValue | undefined {
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== "") {
      return value;
    }
  }
  return undefined;
}

function evidenceCount(row: RowRecord): JsonValue | undefined {
  const direct = signalField(row, ["evidence_count", "evidence", "sources_count", "source_count", "citations_count"]);
  if (Array.isArray(direct)) {
    return direct.length;
  }
  return direct;
}

type AnalysisCard = {
  label: string;
  value: string;
  detail: string;
  tone: "positive" | "negative" | "warning" | "neutral";
  progress?: number;
};

type DecisionSignal = {
  symbol: string;
  name: string;
  score: number;
  grade: string;
  confidence: string;
  decision: string;
  tier: "action" | "research" | "monitor" | "gated";
  whyNow: string;
  setup: string;
  quote: string;
  liquidity: string;
  valuation: string;
  invalidation: string;
  nextAction: string;
  freshness: string;
};

type SignalGroup = {
  key: DecisionSignal["tier"];
  title: string;
  description: string;
  tone: "positive" | "negative" | "warning" | "neutral";
  items: DecisionSignal[];
};

type PulseItem = {
  label: string;
  title: string;
  detail: string;
  tone: "positive" | "negative" | "warning" | "neutral";
};

type ExposureCard = {
  label: string;
  value: string;
  detail: string;
  tone: "positive" | "negative" | "warning" | "neutral";
};

type OperationCard = ExposureCard;

type SignalOverview = {
  leadCount: number;
  researchCount: number;
  monitoredCount: number;
  gatedCount: number;
  averageScore: number;
  scoreBuckets: number[];
  breadthPositive: number;
  breadthTotal: number;
  sepaCoverage: number;
  valuationRows: number;
};

type FactorBar = {
  label: string;
  value: number;
  detail: string;
  tone: "positive" | "negative" | "warning" | "neutral";
};

type TickerInsight = {
  symbol: string;
  name: string;
  decision: string;
  score: number;
  factors: FactorBar[];
  rationale: string;
  nextAction: string;
  invalidation: string;
  evidence: Array<{ label: string; value: string; tone: "positive" | "negative" | "warning" | "neutral" }>;
};

type ChartItem = {
  label: string;
  value: number;
  detail: string;
  tone: "positive" | "negative" | "warning" | "neutral";
};

type MarketCharts = {
  scoreBuckets: number[];
  movers: ChartItem[];
  upside: ChartItem[];
  setups: ChartItem[];
};

type TraderHolding = {
  name: string;
  value: number;
  share: number;
};

type TraderPortfolio = {
  name: string;
  filer: string;
  filedDate: string;
  eventDate: string;
  holdingsCount: number;
  totalValue: number;
  caveat: string;
  topHoldings: TraderHolding[];
};

function buildSignalOverview({
  signalGroups,
  signalRows,
  quoteRows,
  sepaRows,
  valuationRows,
}: {
  signalGroups: SignalGroup[];
  signalRows: RowRecord[];
  quoteRows: RowRecord[];
  sepaRows: RowRecord[];
  valuationRows: RowRecord[];
}): SignalOverview {
  const scores = signalRows.map((row) => numericValue(row.score ?? row.final_score)).filter(Number.isFinite);
  const scoreBuckets = [0, 0, 0, 0, 0];
  for (const score of scores) {
    const normalized = score > 10 ? score : score * 10;
    const bucket = clamp(Math.floor(normalized / 20), 0, 4);
    scoreBuckets[bucket] += 1;
  }
  const countByTier = (tier: SignalGroup["key"]) => signalGroups.find((group) => group.key === tier)?.items.length ?? 0;

  return {
    leadCount: countByTier("action"),
    researchCount: countByTier("research"),
    monitoredCount: countByTier("monitor"),
    gatedCount: countByTier("gated"),
    averageScore: scores.length ? scores.reduce((total, score) => total + (score > 10 ? score : score * 10), 0) / scores.length : 0,
    scoreBuckets,
    breadthPositive: quoteRows.filter((row) => numericValue(row.change_pct) > 0).length,
    breadthTotal: quoteRows.length,
    sepaCoverage: sepaRows.length,
    valuationRows: valuationRows.length,
  };
}

function buildTickerInsight({
  symbol,
  decisionSignals,
  quoteRows,
  sepaRows,
  liquidityRows,
  valuationRows,
  thesisRows,
  catalystRows,
  disclosureRows,
}: {
  symbol: string;
  decisionSignals: DecisionSignal[];
  quoteRows: RowRecord[];
  sepaRows: RowRecord[];
  liquidityRows: RowRecord[];
  valuationRows: RowRecord[];
  thesisRows: RowRecord[];
  catalystRows: RowRecord[];
  disclosureRows: RowRecord[];
}): TickerInsight {
  const fallback = decisionSignals[0];
  const active = decisionSignals.find((item) => item.symbol === symbol) ?? fallback;
  const activeSymbol = active?.symbol ?? symbol;
  const quote = findBySymbol(quoteRows, activeSymbol);
  const sepa = findBySymbol(sepaRows, activeSymbol);
  const liquidity = findBySymbol(liquidityRows, activeSymbol);
  const valuation = findBySymbol(valuationRows, activeSymbol);
  const thesis = findBySymbol(thesisRows, activeSymbol);
  const catalyst = findBySymbol(catalystRows, activeSymbol);
  const disclosure = findBySymbol(disclosureRows, activeSymbol);
  const score = active?.score ?? 0;
  const move = numericValue(quote?.change_pct);
  const upside = numericValue(valuation?.upside_pct);
  const setupScore = bestNumeric(sepa ?? {}, active ?? {}, ["score", "final_score"]);
  const liquidityScore = liquidityQuality(liquidity);
  const evidenceCount = [thesis, catalyst, disclosure].filter(Boolean).length;

  return {
    symbol: activeSymbol || "Select",
    name: active?.name ?? "Select a ticker from the queue",
    decision: active?.decision ?? "-",
    score,
    rationale: active?.whyNow ?? displayValue(signalField(thesis ?? {}, ["summary", "notes", "status"])),
    nextAction: active?.nextAction ?? displayValue(signalField(catalyst ?? {}, ["summary", "notes", "event_date", "due_date"])),
    invalidation: active?.invalidation ?? "-",
    factors: [
      {
        label: "Composite",
        value: clamp(score > 10 ? score : score * 10, 0, 100),
        detail: active ? `${formatScore(score)} score` : "No signal row",
        tone: toneForValue(score, "score"),
      },
      {
        label: "Setup",
        value: clamp(setupScore > 10 ? setupScore : setupScore * 10, 0, 100),
        detail: displayValue(signalField(sepa ?? {}, ["verdict", "stage", "score"])),
        tone: toneForValue(signalField(sepa ?? {}, ["verdict", "stage", "score"]), "verdict"),
      },
      {
        label: "Tape",
        value: Number.isFinite(move) ? clamp(((move + 10) / 20) * 100, 0, 100) : 0,
        detail: Number.isFinite(move) ? formatPercent(move) : "No quote",
        tone: toneForValue(move, "change_pct"),
      },
      {
        label: "Upside",
        value: Number.isFinite(upside) ? clamp(((upside + 25) / 75) * 100, 0, 100) : 0,
        detail: Number.isFinite(upside) ? formatPercent(upside) : "No valuation",
        tone: toneForValue(upside, "upside_pct"),
      },
      {
        label: "Liquidity",
        value: liquidityScore,
        detail: displayValue(signalField(liquidity ?? {}, ["grade", "avg_dollar_volume", "avg_daily_volume"])),
        tone: liquidityScore >= 70 ? "positive" : liquidityScore >= 40 ? "warning" : "negative",
      },
      {
        label: "Evidence",
        value: evidenceCount * 33.3,
        detail: `${evidenceCount}/3 supporting rows`,
        tone: evidenceCount >= 2 ? "positive" : evidenceCount ? "warning" : "neutral",
      },
    ],
    evidence: [
      { label: "Thesis", value: displayValue(signalField(thesis ?? {}, ["status", "summary", "notes"])), tone: thesis ? "positive" : "neutral" },
      { label: "Catalyst", value: displayValue(signalField(catalyst ?? {}, ["event_date", "due_date", "summary", "notes"])), tone: catalyst ? "warning" : "neutral" },
      { label: "Trader", value: displayValue(signalField(disclosure ?? {}, ["trader_name", "filer_name", "action", "filed_date"])), tone: disclosure ? "positive" : "neutral" },
    ],
  };
}

function buildMarketCharts({
  signalRows,
  quoteRows,
  sepaRows,
  valuationRows,
}: {
  signalRows: RowRecord[];
  quoteRows: RowRecord[];
  sepaRows: RowRecord[];
  valuationRows: RowRecord[];
}): MarketCharts {
  const scoreBuckets = [0, 0, 0, 0, 0];
  for (const row of signalRows) {
    const rawScore = numericValue(row.score ?? row.final_score);
    if (!Number.isFinite(rawScore)) {
      continue;
    }
    const score = rawScore > 10 ? rawScore : rawScore * 10;
    scoreBuckets[clamp(Math.floor(score / 20), 0, 4)] += 1;
  }

  const movers = quoteRows
    .map((row) => ({
      label: symbolFromRow(row) || displayValue(signalField(row, ["name", "title"])),
      value: numericValue(row.change_pct),
      detail: displayValue(signalField(row, ["price", "observed_at", "source"])),
      tone: toneForValue(row.change_pct, "change_pct"),
    }))
    .filter((item) => Number.isFinite(item.value))
    .sort((left, right) => Math.abs(right.value) - Math.abs(left.value))
    .slice(0, 6);

  const upside = valuationRows
    .map((row) => ({
      label: symbolFromRow(row),
      value: numericValue(row.upside_pct),
      detail: displayValue(signalField(row, ["fair_value", "method", "as_of"])),
      tone: toneForValue(row.upside_pct, "upside_pct"),
    }))
    .filter((item) => item.label && Number.isFinite(item.value))
    .sort((left, right) => right.value - left.value)
    .slice(0, 6);

  const setupCounts = new Map<string, number>();
  for (const row of sepaRows) {
    const label = displayValue(signalField(row, ["verdict", "stage", "grade"]));
    setupCounts.set(label, (setupCounts.get(label) ?? 0) + 1);
  }
  const setups = [...setupCounts.entries()]
    .map(([label, value]) => ({
      label,
      value,
      detail: `${value} setup${value === 1 ? "" : "s"}`,
      tone: toneForValue(label, "verdict"),
    }))
    .sort((left, right) => right.value - left.value)
    .slice(0, 5);

  return { scoreBuckets, movers, upside, setups };
}

function buildTraderPortfolios(disclosureRows: RowRecord[]): TraderPortfolio[] {
  return disclosureRows
    .map((row) => {
      const raw = objectValue(row.raw);
      const holdings = arrayObjectValue(raw?.holdings);
      const totalValue = numericValue(row.holdings_value_thousands ?? raw?.holdings_value_thousands);
      const parsedHoldings = holdings
        .map((holding) => ({
          name: displayValue(holding.name ?? holding.title ?? holding.cusip),
          value: numericValue(holding.value_thousands),
          share: 0,
        }))
        .filter((holding) => Number.isFinite(holding.value) && holding.value > 0)
        .sort((left, right) => right.value - left.value)
        .slice(0, 5);
      const topTotal = parsedHoldings.reduce((sum, holding) => sum + holding.value, 0);
      const topHoldings = parsedHoldings.map((holding) => ({
        ...holding,
        share: topTotal ? holding.value / topTotal : 0,
      }));

      return {
        name: displayValue(signalField(row, ["trader_name", "filer_name", "source_type"])),
        filer: displayValue(signalField(row, ["filer_name", "source_type"])),
        filedDate: displayValue(signalField(row, ["filed_date"])),
        eventDate: displayValue(signalField(row, ["event_date"])),
        holdingsCount: numericValue(row.holdings_count ?? raw?.holdings_count) || 0,
        totalValue: Number.isFinite(totalValue) ? totalValue : 0,
        caveat: displayValue(raw?.lag_caveat ?? row.lag_caveat),
        topHoldings,
      };
    })
    .filter((portfolio) => portfolio.name !== "-" || portfolio.holdingsCount || portfolio.totalValue)
    .sort((left, right) => right.totalValue - left.totalValue)
    .slice(0, 4);
}

function buildAnalysisCards({
  quoteRows,
  sepaRows,
  liquidityRows,
  valuationRows,
}: {
  quoteRows: RowRecord[];
  sepaRows: RowRecord[];
  liquidityRows: RowRecord[];
  valuationRows: RowRecord[];
}): AnalysisCard[] {
  const positiveQuotes = quoteRows.filter((row) => numericValue(row.change_pct) > 0).length;
  const topMover = [...quoteRows].sort((left, right) => Math.abs(numericValue(right.change_pct)) - Math.abs(numericValue(left.change_pct)))[0];
  const strongSetups = sepaRows.filter((row) => String(row.verdict ?? "").includes("strong")).length;
  const liquidSetups = liquidityRows.filter((row) => String(row.grade ?? "").includes("high")).length;
  const validUpsideRows = valuationRows.filter((row) => typeof row.upside_pct === "number");
  const medianUpside = median(validUpsideRows.map((row) => numericValue(row.upside_pct)));

  return [
    {
      label: "Quote Breadth",
      value: `${positiveQuotes}/${quoteRows.length || 0}`,
      detail: topMover ? `${topMover.symbol ?? "Top"} ${formatPercent(numericValue(topMover.change_pct))}` : "No quote rows",
      tone: positiveQuotes >= quoteRows.length / 2 ? "positive" : "warning",
      progress: quoteRows.length ? positiveQuotes / quoteRows.length : 0,
    },
    {
      label: "SEPA Strength",
      value: `${strongSetups}/${sepaRows.length || 0}`,
      detail: "Strong stage-2 style setups",
      tone: strongSetups ? "positive" : "neutral",
      progress: sepaRows.length ? strongSetups / sepaRows.length : 0,
    },
    {
      label: "Liquidity Coverage",
      value: `${liquidSetups}/${liquidityRows.length || 0}`,
      detail: "High or very-high liquidity",
      tone: liquidSetups === liquidityRows.length && liquidityRows.length ? "positive" : "neutral",
      progress: liquidityRows.length ? liquidSetups / liquidityRows.length : 0,
    },
    {
      label: "Valuation Signal",
      value: validUpsideRows.length ? formatPercent(medianUpside) : "-",
      detail: "Median proxy upside after sanity checks",
      tone: medianUpside > 0 ? "positive" : medianUpside < 0 ? "negative" : "neutral",
      progress: clamp((medianUpside + 50) / 100, 0, 1),
    },
  ];
}

function buildDecisionSignals({
  signalRows,
  candidateRows,
  quoteRows,
  sepaRows,
  liquidityRows,
  valuationRows,
}: {
  signalRows: RowRecord[];
  candidateRows: RowRecord[];
  quoteRows: RowRecord[];
  sepaRows: RowRecord[];
  liquidityRows: RowRecord[];
  valuationRows: RowRecord[];
}): DecisionSignal[] {
  const quoteBySymbol = indexBySymbol(quoteRows);
  const sepaBySymbol = indexBySymbol(sepaRows);
  const liquidityBySymbol = indexBySymbol(liquidityRows);
  const valuationBySymbol = indexBySymbol(valuationRows);
  const candidatesBySymbol = indexBySymbol(candidateRows);
  const sourceRows = signalRows.length ? signalRows : candidateRows;

  return sourceRows
    .map((row) => {
      const symbol = symbolFromRow(row);
      const candidate = candidatesBySymbol.get(symbol) ?? row;
      const quote = quoteBySymbol.get(symbol);
      const sepa = sepaBySymbol.get(symbol);
      const liquidity = liquidityBySymbol.get(symbol);
      const valuation = valuationBySymbol.get(symbol);
      const score = bestNumeric(row, candidate, ["final_score", "score", "confidence", "signal_grade"]);
      return {
        symbol,
        name: displayValue(signalField(candidate, ["name", "title", "category", "asset_class"]) ?? "Watchlist name"),
        score,
        grade: displayValue(signalField(row, ["signal_grade", "grade", "rating"])),
        confidence: displayValue(signalField(row, ["confidence", "confidence_score", "probability", "conviction"])),
        decision: displayValue(signalField(row, ["decision", "action", "recommendation", "stance", "status"])),
        tier: signalTier(row, candidate, score, sepa),
        whyNow: displayValue(signalField(row, ["why_now", "rationale", "summary", "notes", "catalyst", "thesis"])),
        setup: displayValue(signalField(sepa ?? row, ["verdict", "stage", "signal_grade", "grade"])),
        quote: formatSignedPercent(quote?.change_pct),
        liquidity: displayValue(signalField(liquidity ?? {}, ["grade", "avg_dollar_volume", "avg_daily_volume"])),
        valuation: formatSignedPercent(valuation?.upside_pct),
        invalidation: displayValue(signalField(row, ["invalidation", "invalidates_if", "risk", "bear_case", "stop_loss"])),
        nextAction: displayValue(signalField(row, ["next_action", "action_required", "follow_up", "next_step"])),
        freshness: displayValue(signalField(row, ["source_freshness", "freshness", "latest_source_at", "updated_at", "as_of", "date"])),
      };
    })
    .filter((item) => item.symbol)
    .sort((left, right) => right.score - left.score)
    .slice(0, 7);
}

function buildSignalGroups(items: DecisionSignal[]): SignalGroup[] {
  const groups: SignalGroup[] = [
    {
      key: "action",
      title: "Action",
      description: "High-conviction names where signal quality clears the bar.",
      tone: "positive",
      items: [],
    },
    {
      key: "research",
      title: "Research Next",
      description: "Promising setups that need thesis, catalyst, or valuation work.",
      tone: "warning",
      items: [],
    },
    {
      key: "monitor",
      title: "Monitor",
      description: "On watch, but not yet urgent.",
      tone: "neutral",
      items: [],
    },
    {
      key: "gated",
      title: "Gated",
      description: "Blocked by invalidation, missing thesis evidence, or explicit no-action gates.",
      tone: "negative",
      items: [],
    },
  ];

  for (const item of items) {
    groups.find((group) => group.key === item.tier)?.items.push(item);
  }
  return groups;
}

function signalTier(
  row: RowRecord,
  candidate: RowRecord,
  score: number,
  sepa: RowRecord | undefined,
): DecisionSignal["tier"] {
  const grade = String(row.signal_grade ?? row.grade ?? candidate.signal_grade ?? candidate.grade ?? "").trim().toLowerCase();
  const text = [
    row.decision,
    row.next_action,
    row.why_now,
    row.invalidation,
    row.signal_grade,
    candidate.decision,
    sepa?.verdict,
  ]
    .map((value) => String(value ?? "").toLowerCase())
    .join(" ");

  if (text.includes("do not") || text.includes("ignore") || text.includes("gated") || /\bf\b/.test(text)) {
    return "gated";
  }
  if (score >= 70 || ["a", "b"].includes(grade) || text.includes("buy") || text.includes("act now")) {
    return "action";
  }
  if (score >= 53 || text.includes("strong_setup") || text.includes("watch")) {
    return "research";
  }
  return "monitor";
}

function buildPulseItems({
  newsRows,
  catalystRows,
  thesisRows,
  disclosureRows,
}: {
  newsRows: RowRecord[];
  catalystRows: RowRecord[];
  thesisRows: RowRecord[];
  disclosureRows: RowRecord[];
}): PulseItem[] {
  const items: PulseItem[] = [];
  for (const row of catalystRows.slice(0, 2)) {
    items.push({
      label: "Catalyst",
      title: displayValue(signalField(row, ["symbol", "ticker", "title", "name", "event_type"])),
      detail: displayValue(signalField(row, ["due_date", "event_date", "date", "summary", "notes"])),
      tone: "warning",
    });
  }
  for (const row of newsRows.slice(0, 2)) {
    items.push({
      label: "News",
      title: displayValue(signalField(row, ["title", "summary", "provider"])),
      detail: displayValue(signalField(row, ["published_at", "related_symbols", "source"])),
      tone: "neutral",
    });
  }
  for (const row of thesisRows.slice(0, 2)) {
    items.push({
      label: "Thesis",
      title: displayValue(signalField(row, ["symbol", "ticker", "title", "name", "status"])),
      detail: displayValue(signalField(row, ["summary", "notes", "updated_at", "status"])),
      tone: String(row.status ?? "").toLowerCase().includes("active") ? "positive" : "neutral",
    });
  }
  for (const row of disclosureRows.slice(0, 1)) {
    items.push({
      label: "Disclosure",
      title: displayValue(signalField(row, ["trader_name", "filer_name", "source_type"])),
      detail: displayValue(signalField(row, ["action", "event_date", "filed_date"])),
      tone: "neutral",
    });
  }
  return items.slice(0, 5);
}

function buildExposureCards({
  portfolioRows,
  quoteRows,
  catalystRows,
  valuationRows,
}: {
  portfolioRows: RowRecord[];
  quoteRows: RowRecord[];
  catalystRows: RowRecord[];
  valuationRows: RowRecord[];
}): ExposureCard[] {
  const positiveQuotes = quoteRows.filter((row) => numericValue(row.change_pct) > 0).length;
  const totalMarketValue = portfolioRows.reduce((total, row) => {
    const value = numericValue(row.market_value);
    return Number.isFinite(value) ? total + value : total;
  }, 0);
  const upcomingCatalysts = catalystRows.length;
  const validUpsideRows = valuationRows.filter((row) => Number.isFinite(numericValue(row.upside_pct)));
  const medianUpside = median(validUpsideRows.map((row) => numericValue(row.upside_pct)));

  return [
    {
      label: "Portfolio",
      value: totalMarketValue ? `$${compactNumber(totalMarketValue)}` : String(portfolioRows.length),
      detail: totalMarketValue ? `${portfolioRows.length} tracked holdings` : "Tracked holdings",
      tone: "neutral",
    },
    {
      label: "Breadth",
      value: `${positiveQuotes}/${quoteRows.length || 0}`,
      detail: "Quotes positive today",
      tone: positiveQuotes >= quoteRows.length / 2 ? "positive" : "warning",
    },
    {
      label: "Catalysts",
      value: String(upcomingCatalysts),
      detail: "Events to monitor",
      tone: upcomingCatalysts ? "warning" : "neutral",
    },
    {
      label: "Proxy Upside",
      value: validUpsideRows.length ? formatPercent(medianUpside) : "-",
      detail: "Median valuation signal",
      tone: medianUpside > 0 ? "positive" : medianUpside < 0 ? "negative" : "neutral",
    },
  ];
}

function buildOperationCards({
  status,
  providerRunRows,
  sourceHealthRows,
}: {
  status?: ApiStatus;
  providerRunRows: RowRecord[];
  sourceHealthRows: RowRecord[];
}): OperationCard[] {
  const healthySources = sourceHealthRows.filter((row) => ["ok", "verified_docs"].includes(String(row.status ?? ""))).length;
  const failedRuns = providerRunRows.filter((row) => toneForValue(row.status, "status") === "negative").length;
  const latestRun = providerRunRows[0];
  return [
    {
      label: "Data Store",
      value: status?.ready ? "Ready" : "Setup",
      detail: status?.source ?? "unknown source",
      tone: status?.ready ? "positive" : "warning",
    },
    {
      label: "Source Health",
      value: `${healthySources}/${sourceHealthRows.length || 0}`,
      detail: "OK or verified checks",
      tone: healthySources === sourceHealthRows.length && sourceHealthRows.length ? "positive" : "warning",
    },
    {
      label: "Provider Runs",
      value: failedRuns ? `${failedRuns} failed` : String(providerRunRows.length),
      detail: latestRun ? displayValue(signalField(latestRun, ["finished_at", "provider", "capability"])) : "No provider run rows",
      tone: failedRuns ? "negative" : "positive",
    },
  ];
}

function indexBySymbol(sourceRows: RowRecord[]): Map<string, RowRecord> {
  const index = new Map<string, RowRecord>();
  for (const row of sourceRows) {
    const symbol = symbolFromRow(row);
    if (symbol && !index.has(symbol)) {
      index.set(symbol, row);
    }
  }
  return index;
}

function findBySymbol(sourceRows: RowRecord[], symbol: string): RowRecord | undefined {
  if (!symbol) {
    return undefined;
  }
  return sourceRows.find((row) => symbolFromRow(row) === symbol);
}

function liquidityQuality(row: RowRecord | undefined): number {
  if (!row) {
    return 0;
  }
  const grade = String(signalField(row, ["grade", "liquidity_grade"]) ?? "").toLowerCase();
  if (grade.includes("very") && grade.includes("high")) {
    return 95;
  }
  if (grade.includes("high")) {
    return 82;
  }
  if (grade.includes("medium")) {
    return 55;
  }
  if (grade.includes("low")) {
    return 22;
  }
  const impact = numericValue(row.impact_1pct_adv_bps);
  if (Number.isFinite(impact)) {
    return clamp(100 - impact, 0, 100);
  }
  const dollarVolume = numericValue(row.avg_dollar_volume);
  if (Number.isFinite(dollarVolume)) {
    return clamp(Math.log10(Math.max(dollarVolume, 1)) * 12, 0, 100);
  }
  return 0;
}

function objectValue(value: JsonValue | undefined): Record<string, JsonValue> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;
}

function arrayObjectValue(value: JsonValue | undefined): Array<Record<string, JsonValue>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, JsonValue> => typeof item === "object" && item !== null && !Array.isArray(item))
    : [];
}

function bestNumeric(primary: RowRecord, fallback: RowRecord, keys: string[]): number {
  for (const key of keys) {
    const value = numericValue(primary[key] ?? fallback[key]);
    if (Number.isFinite(value)) {
      return value;
    }
  }
  return 0;
}

function formatSignedPercent(value: JsonValue | undefined): string {
  const numeric = numericValue(value);
  return Number.isFinite(numeric) ? formatPercent(numeric) : "-";
}

function scoreValue(value: JsonValue | undefined): number {
  const numeric = numericValue(value);
  if (!Number.isFinite(numeric)) {
    return Number.NaN;
  }
  return numeric > 10 ? numeric / 10 : numeric;
}

function formatScore(value: JsonValue | undefined): string {
  const scaled = scoreValue(value);
  return Number.isFinite(scaled) ? String(Math.round(scaled)) : "-";
}

function formattedCellValue(value: JsonValue | undefined, columnId: string): string {
  const numeric = numericValue(value);
  if (Number.isFinite(numeric)) {
    if (isScoreColumn(columnId)) {
      return formatScore(value);
    }
    if (isPercentColumn(columnId)) {
      return formatPercent(numeric);
    }
    if (["avg_dollar_volume", "market_value", "holdings_value_thousands"].includes(columnId)) {
      return compactNumber(numeric);
    }
  }
  return displayValue(value);
}

function isScoreColumn(columnId: string): boolean {
  return ["score", "final_score"].some((key) => columnId === key || columnId.endsWith(`_${key}`));
}

function isPercentColumn(columnId: string): boolean {
  return columnId.endsWith("_pct") || columnId === "change_pct" || columnId === "premium_pct" || columnId === "upside_pct";
}

function isBadgeColumn(columnId: string, value: JsonValue | undefined): boolean {
  if (["decision", "status", "verdict", "grade", "signal_grade", "confidence", "risk", "event_type", "source"].includes(columnId)) {
    return true;
  }
  return typeof value === "string" && ["ok", "error", "monitor", "buy", "sell", "hold", "pass", "strong_setup"].includes(value.toLowerCase());
}

function toneForValue(value: JsonValue | undefined, columnId: string): "positive" | "negative" | "warning" | "neutral" {
  const normalized = String(value ?? "").toLowerCase();
  const numeric = numericValue(value);
  if (["ok", "verified_docs", "pass", "strong_setup", "high", "very_high", "ready"].some((term) => normalized.includes(term))) {
    return "positive";
  }
  if (["error", "fail", "missing", "low", "f"].some((term) => normalized === term || normalized.includes(`${term}_`))) {
    return "negative";
  }
  if (["monitor", "warning", "medium", "d", "disabled"].some((term) => normalized === term || normalized.includes(term))) {
    return "warning";
  }
  if (Number.isFinite(numeric) && (isPercentColumn(columnId) || columnId.includes("change"))) {
    return numeric > 0 ? "positive" : numeric < 0 ? "negative" : "neutral";
  }
  if (Number.isFinite(numeric) && isScoreColumn(columnId)) {
    return numeric >= 70 ? "positive" : numeric < 50 ? "negative" : "warning";
  }
  return "neutral";
}

function numericValue(value: JsonValue | undefined): number {
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.replace(/[%,$]/g, ""));
    return Number.isFinite(parsed) ? parsed : Number.NaN;
  }
  return Number.NaN;
}

function formatPercent(value: number): string {
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
}

function compactNumber(value: number): string {
  return value.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 2 });
}

function median(values: number[]): number {
  const sorted = values.filter(Number.isFinite).sort((left, right) => left - right);
  if (!sorted.length) {
    return 0;
  }
  const midpoint = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[midpoint] : (sorted[midpoint - 1] + sorted[midpoint]) / 2;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function SectionIntro({ eyebrow, title, detail }: { eyebrow: string; title: string; detail: string }) {
  return (
    <div className="section-intro">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
      </div>
      <p>{detail}</p>
    </div>
  );
}

function SignalOverviewPanel({ overview, groups }: { overview: SignalOverview; groups: SignalGroup[] }) {
  const tierItems = groups.map((group) => ({
    label: group.title,
    value: group.items.length,
    tone: group.tone,
  }));
  const breadthProgress = overview.breadthTotal ? overview.breadthPositive / overview.breadthTotal : 0;
  const averageScoreProgress = clamp(overview.averageScore / 100, 0, 1);

  return (
    <section className="signal-overview" aria-label="Signal overview">
      <article className="overview-card primary">
        <div>
          <span>Decision queue</span>
          <strong>{overview.leadCount}</strong>
        </div>
        <p>Action tier names</p>
        <DistributionBar items={tierItems} />
      </article>
      <article className="overview-card">
        <div>
          <span>Avg score</span>
          <strong>{overview.averageScore ? Math.round(overview.averageScore) : "-"}</strong>
        </div>
        <p>Normalized 0-100 conviction</p>
        <div className="overview-track" aria-hidden="true">
          <i style={{ width: `${averageScoreProgress * 100}%` }} />
        </div>
      </article>
      <article className="overview-card">
        <div>
          <span>Breadth</span>
          <strong>
            {overview.breadthPositive}/{overview.breadthTotal || 0}
          </strong>
        </div>
        <p>Positive quote rows</p>
        <div className="overview-track positive" aria-hidden="true">
          <i style={{ width: `${breadthProgress * 100}%` }} />
        </div>
      </article>
      <article className="overview-card">
        <div>
          <span>Score shape</span>
          <strong>{overview.scoreBuckets.reduce((total, value) => total + value, 0)}</strong>
        </div>
        <p>Low to high distribution</p>
        <MiniHistogram values={overview.scoreBuckets} />
      </article>
      <article className="overview-card">
        <div>
          <span>Coverage</span>
          <strong>{overview.sepaCoverage + overview.valuationRows}</strong>
        </div>
        <p>{overview.sepaCoverage} SEPA / {overview.valuationRows} valuation</p>
        <DistributionBar
          items={[
            { label: "SEPA", value: overview.sepaCoverage, tone: "positive" },
            { label: "Valuation", value: overview.valuationRows, tone: "warning" },
          ]}
        />
      </article>
    </section>
  );
}

function DistributionBar({
  items,
}: {
  items: Array<{ label: string; value: number; tone: "positive" | "negative" | "warning" | "neutral" }>;
}) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  return (
    <div className="distribution" aria-label={items.map((item) => `${item.label} ${item.value}`).join(", ")}>
      {items.map((item) => (
        <span
          key={item.label}
          className={item.tone}
          style={{ width: `${total ? Math.max((item.value / total) * 100, item.value ? 8 : 0) : 0}%` }}
          title={`${item.label}: ${item.value}`}
        />
      ))}
    </div>
  );
}

function MiniHistogram({ values }: { values: number[] }) {
  const max = Math.max(...values, 1);
  return (
    <div className="mini-histogram" aria-label={`Score buckets ${values.join(", ")}`}>
      {values.map((value, index) => (
        <span key={index} style={{ height: `${Math.max((value / max) * 100, value ? 16 : 4)}%` }} title={`${value} names`} />
      ))}
    </div>
  );
}

function TickerInsightPanel({ insight }: { insight: TickerInsight }) {
  return (
    <section className="decision-panel ticker-insight">
      <div className="decision-panel-header">
        <div>
          <p className="eyebrow">Why this decision</p>
          <h2>{insight.symbol === "Select" ? "Ticker Explanation" : insight.symbol}</h2>
        </div>
        <span className={`score-chip ${toneForValue(insight.score, "score")}`}>{formatScore(insight.score)}</span>
      </div>
      <div className="ticker-insight-body">
        <div className="ticker-hero">
          <div>
            <strong>{insight.name}</strong>
            <span className={`badge-cell ${toneForValue(insight.decision, "decision")}`}>{insight.decision}</span>
          </div>
          <p>{insight.rationale}</p>
        </div>

        <FactorBars factors={insight.factors} />

        <div className="decision-notes">
          <KeyValue label="Next action" value={insight.nextAction} />
          <KeyValue label="Invalidation" value={insight.invalidation} />
        </div>

        <div className="evidence-strip">
          {insight.evidence.map((item) => (
            <article key={item.label} className={`evidence-pill ${item.tone}`}>
              <span>{item.label}</span>
              <strong>{item.value}</strong>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function FactorBars({ factors }: { factors: FactorBar[] }) {
  return (
    <div className="factor-bars">
      {factors.map((factor) => (
        <div key={factor.label} className="factor-row">
          <div>
            <span>{factor.label}</span>
            <strong>{factor.detail}</strong>
          </div>
          <div className={`factor-track ${factor.tone}`} aria-hidden="true">
            <i style={{ width: `${clamp(factor.value, 0, 100)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

function MarketChartsPanel({ charts }: { charts: MarketCharts }) {
  return (
    <section className="decision-panel market-charts">
      <div className="decision-panel-header">
        <div>
          <p className="eyebrow">Analysis charts</p>
          <h2>Signal Shape</h2>
        </div>
        <LineChart size={18} />
      </div>
      <div className="chart-grid">
        <article className="chart-card">
          <h3>Score distribution</h3>
          <MiniHistogram values={charts.scoreBuckets} />
          <div className="axis-labels">
            <span>0</span>
            <span>100</span>
          </div>
        </article>
        <HorizontalBarChart title="Largest moves" items={charts.movers} valueFormat={formatPercent} symmetric />
        <HorizontalBarChart title="Valuation upside" items={charts.upside} valueFormat={formatPercent} />
        <SetupMix items={charts.setups} />
      </div>
    </section>
  );
}

function HorizontalBarChart({
  title,
  items,
  valueFormat,
  symmetric = false,
}: {
  title: string;
  items: ChartItem[];
  valueFormat: (value: number) => string;
  symmetric?: boolean;
}) {
  const max = Math.max(...items.map((item) => Math.abs(item.value)), 1);
  return (
    <article className="chart-card">
      <h3>{title}</h3>
      {items.length ? (
        <div className={`bar-chart ${symmetric ? "symmetric" : ""}`}>
          {items.map((item) => (
            <div key={`${title}-${item.label}`} className="bar-row">
              <span>{item.label}</span>
              <div className={`bar-track ${item.tone}`} aria-hidden="true">
                <i style={{ width: `${Math.max((Math.abs(item.value) / max) * 100, 3)}%` }} />
              </div>
              <strong>{valueFormat(item.value)}</strong>
            </div>
          ))}
        </div>
      ) : (
        <p className="empty-note">No chart rows.</p>
      )}
    </article>
  );
}

function SetupMix({ items }: { items: ChartItem[] }) {
  return (
    <article className="chart-card">
      <h3>Setup mix</h3>
      <DistributionBar items={items.length ? items : [{ label: "None", value: 0, tone: "neutral" }]} />
      <div className="setup-list">
        {items.length ? (
          items.map((item) => (
            <div key={item.label}>
              <span className={`health-dot ${item.tone}`} />
              <strong>{item.label}</strong>
              <em>{item.value}</em>
            </div>
          ))
        ) : (
          <p className="empty-note">No setup rows.</p>
        )}
      </div>
    </article>
  );
}

function TraderPortfolioPanel({ portfolios }: { portfolios: TraderPortfolio[] }) {
  return (
    <section className="decision-panel trader-portfolios">
      <div className="decision-panel-header">
        <div>
          <p className="eyebrow">Notable trader portfolio</p>
          <h2>13F Evidence</h2>
        </div>
        <span className="source-chip">{portfolios.length} filings</span>
      </div>
      <div className="trader-list">
        {portfolios.length ? (
          portfolios.map((portfolio) => (
            <article key={`${portfolio.name}-${portfolio.filedDate}`} className="trader-card">
              <div className="trader-card-header">
                <div>
                  <strong>{portfolio.name}</strong>
                  <span>{portfolio.filer}</span>
                </div>
                <div>
                  <b>{portfolio.holdingsCount || "-"}</b>
                  <span>holdings</span>
                </div>
              </div>
              <div className="trader-meta">
                <span>Filed {portfolio.filedDate}</span>
                <span>As of {portfolio.eventDate}</span>
                <span>{portfolio.totalValue ? `$${compactNumber(portfolio.totalValue * 1000)}` : "Value unavailable"}</span>
              </div>
              <div className="holding-bars">
                {portfolio.topHoldings.length ? (
                  portfolio.topHoldings.map((holding) => (
                    <div key={holding.name}>
                      <span>{holding.name}</span>
                      <div className="holding-track" aria-hidden="true">
                        <i style={{ width: `${Math.max(holding.share * 100, 4)}%` }} />
                      </div>
                      <strong>${compactNumber(holding.value * 1000)}</strong>
                    </div>
                  ))
                ) : (
                  <p className="empty-note">Holding table not parsed for this filing.</p>
                )}
              </div>
              {portfolio.caveat !== "-" ? <p className="filing-caveat">{portfolio.caveat}</p> : null}
            </article>
          ))
        ) : (
          <p className="empty-note">No notable trader 13F filings are available yet.</p>
        )}
      </div>
    </section>
  );
}

function SignalHierarchy({
  groups,
  selectedSymbol,
  expandedSignal,
  onSelectSymbol,
  onExpandedSignal,
}: {
  groups: SignalGroup[];
  selectedSymbol: string;
  expandedSignal: string;
  onSelectSymbol: (symbol: string) => void;
  onExpandedSignal: (symbol: string) => void;
}) {
  const total = groups.reduce((count, group) => count + group.items.length, 0);

  return (
    <section className="decision-panel signal-panel">
      <div className="decision-panel-header">
        <div>
          <p className="eyebrow">Today&apos;s queue</p>
          <h2>Decision Signal Hierarchy</h2>
        </div>
        <span className="source-chip">{total} ranked</span>
      </div>
      <div className="signal-groups">
        {groups.map((group) => (
          <section key={group.key} className={`signal-group ${group.tone}`}>
            <div className="signal-group-header">
              <div>
                <h3>{group.title}</h3>
                <p>{group.description}</p>
              </div>
              <strong>{group.items.length}</strong>
            </div>
            {group.items.length ? (
              <div className="signal-table-frame">
                <table className="signal-table">
                  <thead>
                    <tr>
                      <th>Ticker</th>
                      <th>Score</th>
                      <th>Signal</th>
                      <th>Setup</th>
                      <th>Move</th>
                      <th>Upside</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.items.map((item) => {
                      const expanded = expandedSignal === item.symbol;
                      return (
                        <Fragment key={item.symbol}>
                          <tr
                            className={`${item.symbol === selectedSymbol ? "selected" : ""} ${expanded ? "expanded" : ""}`}
                            onClick={() => {
                              onSelectSymbol(item.symbol);
                              onExpandedSignal(expanded ? "" : item.symbol);
                            }}
                          >
                            <td>
                              <span className="signal-symbol">
                                <strong>{item.symbol}</strong>
                                <small>{item.name}</small>
                              </span>
                            </td>
                            <td>
                              <span className={`score-chip ${toneForValue(item.score, "score")}`}>{formatScore(item.score)}</span>
                            </td>
                            <td>
                              <span className={`badge-cell ${toneForValue(item.decision, "decision")}`}>{item.decision}</span>
                            </td>
                            <td>
                              <span className={`badge-cell ${toneForValue(item.setup, "verdict")}`}>{item.setup}</span>
                            </td>
                            <td>
                              <span className={`value-pill ${toneForValue(item.quote, "change_pct")}`}>{item.quote}</span>
                            </td>
                            <td>
                              <span className={`value-pill ${toneForValue(item.valuation, "upside_pct")}`}>{item.valuation}</span>
                            </td>
                          </tr>
                          {expanded ? (
                            <tr className="signal-detail-row">
                              <td colSpan={6}>
                                <div className="signal-detail-grid">
                                  <KeyValue label="Why now" value={item.whyNow} />
                                  <KeyValue label="Next action" value={item.nextAction} />
                                  <KeyValue label="Invalidation" value={item.invalidation} />
                                  <KeyValue label="Move" value={item.quote} />
                                  <KeyValue label="Upside" value={item.valuation} />
                                  <KeyValue label="Setup" value={item.setup} />
                                  <KeyValue label="Confidence" value={item.confidence} />
                                  <KeyValue label="Grade" value={item.grade} />
                                  <KeyValue label="Liquidity" value={item.liquidity} />
                                  <KeyValue label="Freshness" value={item.freshness} />
                                </div>
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty-note signal-empty">No names in this tier.</p>
            )}
          </section>
        ))}
      </div>
    </section>
  );
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ExposurePanel({ cards }: { cards: ExposureCard[] }) {
  return (
    <section className="decision-panel exposure-panel">
      <div className="decision-panel-header">
        <div>
          <p className="eyebrow">Operating picture</p>
          <h2>Exposure & Timing</h2>
        </div>
      </div>
      <div className="exposure-grid">
        {cards.map((card) => (
          <article key={card.label} className={`exposure-card ${card.tone}`}>
            <span>{card.label}</span>
            <strong>{card.value}</strong>
            <p>{card.detail}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function PulsePanel({ items }: { items: PulseItem[] }) {
  return (
    <section className="decision-panel pulse-panel">
      <div className="decision-panel-header">
        <div>
          <p className="eyebrow">Evidence flow</p>
          <h2>What Changed</h2>
        </div>
      </div>
      <div className="pulse-list">
        {items.length ? (
          items.map((item, index) => (
            <article key={`${item.label}-${index}`} className={`pulse-item ${item.tone}`}>
              <span>{item.label}</span>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
            </article>
          ))
        ) : (
          <p className="empty-note">No catalysts, news, theses, or source-health rows returned.</p>
        )}
      </div>
    </section>
  );
}

function OperationHealthPanel({
  cards,
  sourceRows,
  providerRows,
  serviceSource,
}: {
  cards: OperationCard[];
  sourceRows: RowRecord[];
  providerRows: RowRecord[];
  serviceSource?: string;
}) {
  const sourceRowsToShow = sourceRows.slice(0, 4);
  const providerRowsToShow = providerRows.slice(0, 4);

  return (
    <section className="operation-health" aria-label="Operation health">
      <div className="operation-header">
        <div>
          <p className="eyebrow">Service checks</p>
          <h2>Runtime Health</h2>
        </div>
        <span className="source-chip">{serviceSource ?? "unknown source"}</span>
      </div>
      <div className="operation-grid">
        {cards.map((card) => (
          <article key={card.label} className={`operation-card ${card.tone}`}>
            <span>{card.label}</span>
            <strong>{card.value}</strong>
            <p>{card.detail}</p>
          </article>
        ))}
        <article className="operation-card">
          <span>Latest Source</span>
          <strong>{displayValue(signalField(sourceRows[0] ?? {}, ["source", "provider", "capability"]))}</strong>
          <p>{displayValue(signalField(sourceRows[0] ?? {}, ["status", "checked_at", "detail"]))}</p>
        </article>
        <article className="operation-card">
          <span>Latest Run</span>
          <strong>{displayValue(signalField(providerRows[0] ?? {}, ["provider", "capability", "status"]))}</strong>
          <p>{displayValue(signalField(providerRows[0] ?? {}, ["finished_at", "detail", "status"]))}</p>
        </article>
      </div>
      <div className="operation-lists">
        <div>
          <h3>Source checks</h3>
          <HealthRows rows={sourceRowsToShow} primaryKeys={["source", "provider", "capability"]} secondaryKeys={["status", "checked_at", "detail"]} />
        </div>
        <div>
          <h3>Provider runs</h3>
          <HealthRows rows={providerRowsToShow} primaryKeys={["provider", "capability"]} secondaryKeys={["status", "finished_at", "detail"]} />
        </div>
      </div>
    </section>
  );
}

function HealthRows({
  rows,
  primaryKeys,
  secondaryKeys,
}: {
  rows: RowRecord[];
  primaryKeys: string[];
  secondaryKeys: string[];
}) {
  if (!rows.length) {
    return <p className="empty-note">No rows returned.</p>;
  }

  return (
    <div className="health-row-list">
      {rows.map((row, index) => {
        const statusValue = signalField(row, ["status", "state", "result"]);
        return (
          <article key={index} className="health-row">
            <span className={`health-dot ${toneForValue(statusValue, "status")}`} />
            <div>
              <strong>{displayValue(signalField(row, primaryKeys))}</strong>
              <p>{displayValue(signalField(row, secondaryKeys))}</p>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function StatusPill({ status }: { status?: ApiStatus }) {
  const ready = Boolean(status?.ready);
  return (
    <span className={`status-pill ${ready ? "ready" : "not-ready"}`}>
      {ready ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
      {ready ? "Ready" : "Setup needed"}
    </span>
  );
}

function SignalsPanel({
  data,
  preferredColumns,
  source,
  onSelectSymbol,
  selectedSymbol,
}: {
  data: RowRecord[];
  preferredColumns: string[];
  source: string;
  onSelectSymbol?: (symbol: string) => void;
  selectedSymbol?: string;
}) {
  return (
    <section className="panel signals-panel">
      <div className="panel-header signals-header">
        <div className="panel-title">
          <Activity size={18} />
          <h2>Signals</h2>
          <span>{data.length}</span>
        </div>
        <span className="source-chip">{source}</span>
      </div>
      <div className="table-frame signals-frame">
        <table>
          <thead>
            <tr>
              {preferredColumns.map((column) => (
                <th key={column}>{titleize(column)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.length ? (
              data.map((row, index) => {
                const symbol = symbolFromRow(row);
                return (
                  <tr
                    key={`${symbol || "signal"}-${index}`}
                    className={symbol && symbol === selectedSymbol ? "selected" : ""}
                    onClick={() => symbol && onSelectSymbol?.(symbol)}
                  >
                    {preferredColumns.map((column) => (
                      <td key={column}>
                        <CellValue value={row[column]} columnId={column} />
                      </td>
                    ))}
                  </tr>
                );
              })
            ) : (
              <tr>
                <td className="empty-cell" colSpan={preferredColumns.length}>
                  No signal rows returned.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function StatusBanner({ status, errors }: { status?: ApiStatus; errors: PanelData["errors"] }) {
  const errorEntries = Object.entries(errors);
  if (status?.ready && errorEntries.length === 0) {
    return null;
  }

  return (
    <section className="status-banner">
      <AlertCircle size={18} />
      <div>
        <strong>{status?.message ?? "Data is not available yet."}</strong>
        {status?.metadata?.setup_instructions ? <p>{displayValue(status.metadata.setup_instructions)}</p> : null}
        {errorEntries.length ? <p>{errorEntries.map(([name, error]) => `${titleize(name)}: ${error}`).join(" | ")}</p> : null}
      </div>
    </section>
  );
}

function MetricCard({
  icon,
  label,
  value,
  tone,
}: {
  icon: ReactNode;
  label: string;
  value: string | number;
  tone?: "ok" | "warn";
}) {
  return (
    <article className={`metric-card ${tone ?? ""}`}>
      <div className="metric-icon">{icon}</div>
      <div>
        <p>{label}</p>
        <strong>{displayValue(value)}</strong>
      </div>
    </article>
  );
}

function InsightCard({ label, value, detail, tone, progress = 0 }: AnalysisCard) {
  return (
    <article className={`insight-card ${tone}`}>
      <div>
        <p>{label}</p>
        <strong>{value}</strong>
      </div>
      <span>{detail}</span>
      <div className="insight-track" aria-hidden="true">
        <i style={{ width: `${clamp(progress, 0, 1) * 100}%` }} />
      </div>
    </article>
  );
}

function DataTable({
  title,
  icon,
  data,
  preferredColumns,
  includeExtraColumns = true,
  onSelectSymbol,
  selectedSymbol,
}: {
  title: string;
  icon: ReactNode;
  data: RowRecord[];
  preferredColumns: string[];
  includeExtraColumns?: boolean;
  onSelectSymbol?: (symbol: string) => void;
  selectedSymbol?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");
  const filterId = `filter-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`;

  const columns = useMemo<ColumnDef<RowRecord, JsonValue | undefined>[]>(() => {
    const keys = includeExtraColumns ? collectColumns(data, preferredColumns) : preferredColumns.filter((key) => data.some((row) => row[key] !== undefined));
    if (!keys.length) {
      return [];
    }
    return keys.map((key) =>
      columnHelper.accessor((row) => row[key], {
        id: key,
        header: titleize(key),
        cell: (info) => <CellValue value={info.getValue()} columnId={key} />,
      }),
    );
  }, [data, includeExtraColumns, preferredColumns]);

  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 8 } },
  });

  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          {icon}
          <h2>{title}</h2>
          <span>{data.length}</span>
        </div>
        <label className="search-box">
          <Search size={15} />
          <input
            id={filterId}
            name={filterId}
            value={globalFilter}
            onChange={(event) => setGlobalFilter(event.target.value)}
            placeholder="Filter"
          />
        </label>
      </div>
      <div className="table-frame">
        <table>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id}>
                    <button type="button" onClick={header.column.getToggleSortingHandler()} disabled={!header.column.getCanSort()}>
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {header.column.getIsSorted() === "asc" ? <ChevronUp size={13} /> : null}
                      {header.column.getIsSorted() === "desc" ? <ChevronDown size={13} /> : null}
                    </button>
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length ? (
              table.getRowModel().rows.map((row) => {
                const symbol = symbolFromRow(row.original);
                return (
                  <tr
                    key={row.id}
                    className={symbol && symbol === selectedSymbol ? "selected" : ""}
                    onClick={() => symbol && onSelectSymbol?.(symbol)}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                    ))}
                  </tr>
                );
              })
            ) : (
              <tr>
                <td className="empty-cell" colSpan={Math.max(columns.length, 1)}>
                  No rows returned.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="table-footer">
        <span>
          Page {table.getState().pagination.pageIndex + 1} of {Math.max(table.getPageCount(), 1)}
        </span>
        <div>
          <button type="button" onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()}>
            Previous
          </button>
          <button type="button" onClick={() => table.nextPage()} disabled={!table.getCanNextPage()}>
            Next
          </button>
        </div>
      </div>
    </section>
  );
}

function CellValue({ value, columnId }: { value: JsonValue | undefined; columnId: string }) {
  const text = formattedCellValue(value, columnId);
  const numeric = numericValue(value);
  const tone = toneForValue(value, columnId);
  const title = fullDisplayValue(value);

  if (isScoreColumn(columnId) && Number.isFinite(numeric)) {
    return (
      <span className={`viz-cell ${tone}`} title={title}>
        <span className="viz-track" aria-hidden="true">
          <span style={{ width: `${clamp(numeric / 100, 0, 1) * 100}%` }} />
        </span>
        <span>{text}</span>
      </span>
    );
  }

  if (isPercentColumn(columnId) && Number.isFinite(numeric)) {
    return (
      <span className={`value-pill ${tone}`} title={title}>
        {text}
      </span>
    );
  }

  if (isBadgeColumn(columnId, value)) {
    return (
      <span className={`badge-cell ${tone}`} title={title}>
        {text}
      </span>
    );
  }

  return (
    <span className="cell-text" title={title}>
      {text}
    </span>
  );
}

function TickerDetail({
  input,
  onInput,
  onSubmit,
  selectedSymbol,
  ticker,
  loading,
  error,
}: {
  input: string;
  onInput: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  selectedSymbol: string;
  ticker: TickerPayload | null;
  loading: boolean;
  error: string;
}) {
  const tables = ticker?.tables ?? {};
  const entries = Object.entries(tables).filter(([, tableRows]) => Array.isArray(tableRows) && tableRows.length);

  return (
    <section className="panel detail-panel">
      <div className="panel-header">
        <div className="panel-title">
          <Activity size={18} />
          <h2>Ticker Detail</h2>
        </div>
      </div>
      <form className="ticker-form" onSubmit={onSubmit}>
        <input
          id="ticker-symbol"
          name="ticker-symbol"
          value={input}
          onChange={(event) => onInput(event.target.value.toUpperCase())}
          placeholder="Symbol"
        />
        <button type="submit">Load</button>
      </form>
      <div className="ticker-state">
        <strong>{selectedSymbol || "No ticker selected"}</strong>
        <span>{loading ? "Loading..." : ticker?.found ? "Matches found" : "No matching rows"}</span>
      </div>
      {error ? <p className="inline-error">{error}</p> : null}
      <div className="detail-list">
        {entries.length ? (
          entries.map(([name, tableRows]) => (
            <div key={name} className="detail-group">
              <h3>{titleize(name)}</h3>
              {(tableRows ?? []).slice(0, 3).map((row, index) => (
                <KeyValueList key={`${name}-${index}`} row={row} limit={6} />
              ))}
            </div>
          ))
        ) : (
          <p className="empty-note">Select a row or enter a symbol to inspect matching candidate, portfolio, thesis, catalyst, and memo records.</p>
        )}
      </div>
    </section>
  );
}

function DashboardPreviews({ dashboard }: { dashboard: DashboardPayload }) {
  const previews = [
    ["Priority Candidates", dashboard.priority_candidates],
    ["Near-Term Catalysts", dashboard.near_term_catalysts],
    ["Portfolio Watch", dashboard.portfolio],
    ["Latest News", dashboard.news],
  ] as const;

  return (
    <section className="panel preview-panel">
      <div className="panel-title">
        <Target size={17} />
        <h2>Dashboard Signals</h2>
      </div>
      {previews.map(([title, previewRows]) => (
        <div className="preview-block" key={title}>
          <h3>{title}</h3>
          {(previewRows ?? []).length ? (
            (previewRows ?? []).slice(0, 4).map((row, index) => <KeyValueList key={`${title}-${index}`} row={row} limit={4} />)
          ) : (
            <p className="empty-note">No rows.</p>
          )}
        </div>
      ))}
    </section>
  );
}

function SourceWorkspace({
  groups,
  activeGroup,
  onActiveGroup,
}: {
  groups: SourceGroup[];
  activeGroup: string;
  onActiveGroup: (group: string) => void;
}) {
  const selected = groups.find((group) => group.name === activeGroup) ?? groups[0];

  return (
    <section className="source-workspace">
      <div className="source-workspace-header">
        <div>
          <p className="eyebrow">Integrated sources</p>
          <h2>Data & Analysis Workspaces</h2>
        </div>
        <div className="group-tabs" role="tablist" aria-label="Data source groups">
          {groups.map((group) => (
            <button
              key={group.name}
              type="button"
              role="tab"
              aria-selected={group.name === selected.name}
              className={group.name === selected.name ? "active" : ""}
              onClick={() => onActiveGroup(group.name)}
            >
              <span>{group.name}</span>
              <strong>{group.count}</strong>
            </button>
          ))}
        </div>
      </div>
      <div className="section-grid source-grid">
        {selected.sections.map((section) => (
          <CompactSection
            key={section.title}
            title={section.title}
            icon={section.icon}
            rows={section.rows}
            preferredColumns={section.preferredColumns}
          />
        ))}
      </div>
    </section>
  );
}

function CompactSection({
  title,
  icon,
  rows,
  preferredColumns,
}: {
  title: string;
  icon: ReactNode;
  rows: RowRecord[];
  preferredColumns: string[];
}) {
  const columns = collectColumns(rows, preferredColumns).slice(0, 4);
  return (
    <section className="panel compact-panel">
      <div className="panel-title">
        {icon}
        <h2>{title}</h2>
        <span>{rows.length}</span>
      </div>
      {rows.length ? (
        <div className="compact-list">
          {rows.slice(0, 4).map((row, index) => (
            <article key={index} className="compact-row">
              {columns.map((column) => (
                <div key={column}>
                  <span>{titleize(column)}</span>
                  <CellValue value={row[column]} columnId={column} />
                </div>
              ))}
            </article>
          ))}
        </div>
      ) : (
        <p className="empty-note">No rows returned.</p>
      )}
    </section>
  );
}

function SettingsSection({
  config,
  integration,
}: {
  config?: Record<string, JsonValue>;
  integration?: Record<string, JsonValue>;
}) {
  return (
    <section className="panel compact-panel">
      <div className="panel-title">
        <Settings size={17} />
        <h2>Settings</h2>
      </div>
      <div className="settings-grid">
        <KeyValueList row={(integration ?? {}) as RowRecord} limit={8} />
        <KeyValueList row={(config ?? {}) as RowRecord} limit={6} />
      </div>
    </section>
  );
}

function KeyValueList({ row, limit }: { row: RowRecord; limit: number }) {
  const entries = Object.entries(row).slice(0, limit);
  if (!entries.length) {
    return <p className="empty-note">No fields.</p>;
  }
  return (
    <dl className="kv-list">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{titleize(key)}</dt>
          <dd>
            <CellValue value={value} columnId={key} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

export { App };
