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
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Target,
  UserRoundCog,
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
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
  "signal_grade",
  "confidence",
  "decision",
  "why_now",
  "evidence_count",
  "invalidation",
  "next_action",
  "source_freshness",
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
  const analysisCards = buildAnalysisCards({
    quoteRows,
    sepaRows,
    liquidityRows,
    valuationRows,
    sourceHealthRows,
  });
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
          <h1>Market Workbench</h1>
          <p className="topbar-subtitle">On-demand free-source data, setup analysis, options flow, news, and fundamentals.</p>
        </div>
        <div className="topbar-actions">
          <StatusPill status={status} />
          <button className="icon-button" type="button" onClick={refresh} disabled={loading} title="Refresh data">
            <RefreshCw size={16} className={loading ? "spin" : ""} />
          </button>
        </div>
      </header>

      <StatusBanner status={status} errors={data.errors} />

      <section className="dashboard-grid" aria-label="Dashboard signals">
        <MetricCard icon={<Target size={18} />} label="Candidates" value={getMetric(data.dashboard.metrics, "candidates", candidateRows.length)} />
        <MetricCard icon={<Activity size={18} />} label="Signals" value={signalRows.length} />
        <MetricCard icon={<CircleDollarSign size={18} />} label="Holdings" value={getMetric(data.dashboard.metrics, "holdings", portfolioRows.length)} />
        <MetricCard icon={<ClipboardList size={18} />} label="Theses" value={getMetric(data.dashboard.metrics, "theses", thesisRows.length)} />
        <MetricCard icon={<CalendarClock size={18} />} label="Catalysts" value={getMetric(data.dashboard.metrics, "catalysts", catalystRows.length)} />
        <MetricCard icon={<Database size={18} />} label="Fundamentals" value={getMetric(data.dashboard.metrics, "fundamentals", fundamentalRows.length)} />
        <MetricCard icon={<Activity size={18} />} label="Quotes" value={getMetric(data.dashboard.metrics, "quotes", quoteRows.length)} />
        <MetricCard icon={<Gauge size={18} />} label="SEPA" value={getMetric(data.dashboard.metrics, "sepa", sepaRows.length)} />
        <MetricCard icon={<Activity size={18} />} label="News" value={getMetric(data.dashboard.metrics, "news", newsRows.length)} />
        <MetricCard icon={<ShieldCheck size={18} />} label="Disclosures" value={getMetric(data.dashboard.metrics, "disclosures", disclosureRows.length)} />
        <MetricCard icon={<Database size={18} />} label="Source" value={status?.source ?? "unknown"} tone={status?.ready ? "ok" : "warn"} />
      </section>

      <section className="insight-grid" aria-label="Analysis summary">
        {analysisCards.map((card) => (
          <InsightCard key={card.label} {...card} />
        ))}
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

function buildAnalysisCards({
  quoteRows,
  sepaRows,
  liquidityRows,
  valuationRows,
  sourceHealthRows,
}: {
  quoteRows: RowRecord[];
  sepaRows: RowRecord[];
  liquidityRows: RowRecord[];
  valuationRows: RowRecord[];
  sourceHealthRows: RowRecord[];
}): AnalysisCard[] {
  const positiveQuotes = quoteRows.filter((row) => numericValue(row.change_pct) > 0).length;
  const topMover = [...quoteRows].sort((left, right) => Math.abs(numericValue(right.change_pct)) - Math.abs(numericValue(left.change_pct)))[0];
  const strongSetups = sepaRows.filter((row) => String(row.verdict ?? "").includes("strong")).length;
  const liquidSetups = liquidityRows.filter((row) => String(row.grade ?? "").includes("high")).length;
  const validUpsideRows = valuationRows.filter((row) => typeof row.upside_pct === "number");
  const medianUpside = median(validUpsideRows.map((row) => numericValue(row.upside_pct)));
  const healthySources = sourceHealthRows.filter((row) => ["ok", "verified_docs"].includes(String(row.status ?? ""))).length;

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
    {
      label: "Source Health",
      value: `${healthySources}/${sourceHealthRows.length || 0}`,
      detail: "OK or verified source rows",
      tone: healthySources === sourceHealthRows.length && sourceHealthRows.length ? "positive" : "warning",
      progress: sourceHealthRows.length ? healthySources / sourceHealthRows.length : 0,
    },
  ];
}

function formattedCellValue(value: JsonValue | undefined, columnId: string): string {
  const numeric = numericValue(value);
  if (Number.isFinite(numeric)) {
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
