import { ExternalLink } from "lucide-react";

import { resolveTradingViewSymbol, tradingViewEmbedUrl } from "@/adapters/tradingView";
import { DataTableFrame, EvidenceList, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { JsonValue, PanelData, RowRecord, TickerPayload } from "@/types";
import { rows } from "@/utils";
import { displayField, formatPct, fullField, listField, symbolList, textField, toneFromText } from "./rowFormat";
import { WorkspacePage, type MetricSpec, type OpenTicker } from "./workspacePage";

export function TickerPage({ symbol, ticker, data, onOpenTicker }: { symbol: string; ticker: TickerPayload | null; data: PanelData; onOpenTicker: OpenTicker }) {
  const tables = ticker?.tables ?? {};
  const thesisRows = rows(data.thesisMonitor).filter((row) => symbolList(row).includes(symbol));
  const metrics = tickerHeaderMetrics(ticker);
  const title = ticker?.found === false ? `${symbol} not found` : symbol;
  return (
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Authoritative fundamentals, source-backed evidence, thesis state, and decision context." metrics={metrics}>
      <FundamentalsPanel ticker={ticker} />
      {ticker?.decision_brief || ticker?.decision_snapshot ? (
        <div className="mb-4 grid min-w-0 gap-3 lg:grid-cols-2 [&>*]:min-w-0">
          {ticker.decision_brief && <DossierCard title="Decision Brief" row={ticker.decision_brief} />}
          {ticker.decision_snapshot && <DossierCard title="Decision Snapshot" row={ticker.decision_snapshot} />}
        </div>
      ) : null}
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.7fr)]">
        <TradingViewChart symbol={symbol} ticker={ticker} />
        <AnalystEstimatePanel rows={compactRows(tables.analyst_estimates)} />
      </div>
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <ThesisPanel rows={thesisRows.length ? thesisRows : compactRows(tables.thesis_monitor)} />
        <SourceCoveragePanel
          consensusRows={compactRows(tables.source_consensus)}
          signalRows={compactRows(tables.ticker_source_signals)}
          onOpenTicker={onOpenTicker}
        />
      </div>
      <EvidencePanel
        rows={[
          ...compactRows(tables.feed_signals),
          ...compactRows(tables.news),
          ...compactRows(tables.opportunity_sources),
        ]}
      />
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

function FundamentalsPanel({ ticker }: { ticker: TickerPayload | null }) {
  const rows = compactRows(ticker?.tables?.fundamentals);
  const secRows = rows
    .filter((row) => textField(row, ["source"]) === "sec_companyfacts")
    .sort((a, b) => dateSortValue(b, ["filing_date", "period_end"]) - dateSortValue(a, ["filing_date", "period_end"]));
  const latest = secRows[0];
  const metrics = objectField(latest, "metrics");

  const metricRows = presentMetricCells([
    ["Revenue", moneyMetric(metrics, "revenue"), "SEC company facts revenue"],
    ["Revenue YoY", ratioMetric(metrics, "revenue_growth"), "latest annual period"],
    ["Net Income", moneyMetric(metrics, "net_income"), "SEC company facts net income"],
    ["Net Margin", ratioMetric(metrics, "net_margin"), "net income / revenue"],
    ["Free Cash Flow", moneyMetric(metrics, "free_cash_flow"), "operating cash flow minus capex"],
    ["FCF Margin", ratioMetric(metrics, "fcf_margin"), "free cash flow / revenue"],
    ["Assets", moneyMetric(metrics, "assets"), "latest balance sheet"],
    ["Liabilities", moneyMetric(metrics, "liabilities"), "latest balance sheet"],
    ["Cash", moneyMetric(metrics, "cash"), "cash and equivalents"],
    ["Debt / Assets", ratioMetric(metrics, "debt_to_assets"), "liabilities / assets"],
  ]);

  const yfinance = compactRows(ticker?.tables?.universe_screen)[0];
  const marketRows = presentMetricCells([
    ["Market Cap", moneyMetric(yfinance, "market_cap"), "market data"],
    ["P/S", multipleMetric(yfinance, "ps_ratio"), "sales multiple"],
    ["P/E", multipleMetric(yfinance, "pe_ratio"), "earnings multiple"],
    ["Forward P/E", multipleMetric(yfinance, "forward_pe"), textField(yfinance, ["forward_pe_source"], "forward estimate")],
    ["FCF Yield", ratioMetric(yfinance, "fcf_yield"), "free cash flow yield"],
    ["ROIC", percentMetric(yfinance, "roic"), textField(yfinance, ["roic_source"], "capital returns")],
  ]);

  return (
    <DataTableFrame
      title="Authoritative Fundamentals"
      action={latest?.source_url ? (
        <Button asChild type="button" variant="outline" size="sm">
          <a href={String(latest.source_url)} target="_blank" rel="noreferrer"><ExternalLink /> SEC source</a>
        </Button>
      ) : null}
    >
      <div className="grid gap-0 lg:grid-cols-[1fr_0.65fr]">
        <div className="border-b border-border p-4 lg:border-b-0 lg:border-r">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <StatusBadge tone={latest ? "good" : "warn"}>{latest ? textField(latest, ["form_type"], "SEC filing") : "No SEC row"}</StatusBadge>
            <span className="text-sm text-muted-foreground">
              {latest ? `${displayField(latest, ["filing_date", "period_end"])} from SEC company facts` : "Direct company-facts metrics are not loaded for this ticker."}
            </span>
          </div>
          <MetricGrid rows={metricRows} empty="No authoritative SEC metrics are loaded." />
        </div>
        <div className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Market-Derived Complements</h3>
          <MetricGrid rows={marketRows} empty="No market-derived valuation metrics are loaded." />
        </div>
      </div>
    </DataTableFrame>
  );
}

type MetricCell = [label: string, value: string, detail: string];

function presentMetricCells(rows: MetricCell[]): MetricCell[] {
  return rows.filter(([, value]) => value !== "-");
}

function MetricGrid({ rows, empty }: { rows: MetricCell[]; empty: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <dl className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {rows.map(([label, value, detail]) => (
        <div key={label} className="min-h-24 rounded-md border border-border bg-background px-3 py-2">
          <dt className="text-xs font-medium uppercase text-muted-foreground">{label}</dt>
          <dd className="mt-1 break-words text-lg font-semibold tabular-nums">{value}</dd>
          <dd className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</dd>
        </div>
      ))}
    </dl>
  );
}

function AnalystEstimatePanel({ rows }: { rows: RowRecord[] }) {
  const latest = latestRow(rows, ["as_of"]);
  const estimates = objectField(latest, "estimates");
  const earnings = arrayField(estimates, "earnings_estimate");
  const revenue = arrayField(estimates, "revenue_estimate");
  const targets = objectField(estimates, "analyst_price_targets");
  const currentYearEps = estimateForPeriod(earnings, "0y");
  const nextYearEps = estimateForPeriod(earnings, "+1y");
  const currentYearRevenue = estimateForPeriod(revenue, "0y");
  const nextYearRevenue = estimateForPeriod(revenue, "+1y");
  const rowsToShow = presentMetricCells([
    ["CY Revenue", moneyMetric(currentYearRevenue, "avg"), ratioMetric(currentYearRevenue, "growth")],
    ["NY Revenue", moneyMetric(nextYearRevenue, "avg"), ratioMetric(nextYearRevenue, "growth")],
    ["CY EPS", numberMetric(currentYearEps, "avg"), ratioMetric(currentYearEps, "growth")],
    ["NY EPS", numberMetric(nextYearEps, "avg"), ratioMetric(nextYearEps, "growth")],
    ["Target Mean", moneyMetric(targets, "mean"), "analyst price targets"],
    ["Target Range", targetRange(targets), "low / high"],
  ]);

  return (
    <DataTableFrame title="Analyst Estimates">
      <div className="p-4">
        <div className="mb-3 text-sm text-muted-foreground">{latest ? `yfinance snapshot ${displayField(latest, ["as_of"])}` : "No analyst estimate row is loaded."}</div>
        <MetricGrid rows={rowsToShow} empty="No analyst estimate metrics are loaded." />
      </div>
    </DataTableFrame>
  );
}

function ThesisPanel({ rows }: { rows: RowRecord[] }) {
  const visibleRows = rows.slice(0, 4).map((row) => ({
    status: displayField(row, ["needs_review", "review_reason", "status"], "Loaded"),
    thesis: displayField(row, ["thesis", "why_owned_watched", "summary"], "No thesis text loaded"),
    invalidation: displayField(row, ["invalidation", "invalidation_reason", "stale_reason"], "No invalidation row loaded"),
    reviewed: displayField(row, ["last_reviewed", "as_of", "updated_at"], "-"),
  }));
  return (
    <DataTableFrame title="Thesis State">
      <SimpleTable
        rows={visibleRows}
        empty="No ticker thesis state is loaded."
        columns={[
          ["status", "Status"],
          ["thesis", "Thesis"],
          ["invalidation", "Invalidation"],
          ["reviewed", "Reviewed"],
        ]}
      />
    </DataTableFrame>
  );
}

function SourceCoveragePanel({ consensusRows, signalRows, onOpenTicker }: { consensusRows: RowRecord[]; signalRows: RowRecord[]; onOpenTicker: OpenTicker }) {
  const visibleRows = [
    ...consensusRows.slice(0, 8).map((row) => ({
      source: displayField(row, ["source_name"], "Source"),
      type: displayField(row, ["content_type", "source_family"], "-"),
      net: displayField(row, ["net_consensus", "recommendation"], "-"),
      latest: displayField(row, ["latest_at", "observed_at"], "-"),
    })),
    ...signalRows.slice(0, 6).map((row) => ({
      source: displayField(row, ["source_name", "source_id"], "Signal"),
      type: displayField(row, ["signal_type", "source_family"], "-"),
      net: displayField(row, ["sentiment", "direction", "confidence"], "-"),
      latest: displayField(row, ["observed_at", "as_of"], "-"),
    })),
  ];
  const tickers = [...new Set(consensusRows.flatMap(symbolList).filter(Boolean))].slice(0, 8);
  return (
    <DataTableFrame
      title="Source Coverage"
      action={tickers.length ? (
        <div className="flex flex-wrap justify-end gap-1.5">
          {tickers.map((ticker) => (
            <Button key={ticker} type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(ticker)}>{ticker}</Button>
          ))}
        </div>
      ) : null}
    >
      <SimpleTable
        rows={visibleRows}
        empty="No source coverage rows are loaded."
        columns={[
          ["source", "Source"],
          ["type", "Type"],
          ["net", "Signal"],
          ["latest", "Latest"],
        ]}
      />
    </DataTableFrame>
  );
}

function EvidencePanel({ rows }: { rows: RowRecord[] }) {
  const visibleRows = rows
    .map((row) => ({
      source: displayField(row, ["source_name", "source", "source_key"], "Source"),
      title: displayField(row, ["title", "event", "summary", "reason"], "Evidence item"),
      signal: displayField(row, ["sentiment", "decision", "action", "confidence"], "-"),
      date: displayField(row, ["published_at", "observed_at", "event_date", "as_of"], "-"),
    }))
    .filter((row) => usefulEvidence(row))
    .slice(0, 12);
  return (
    <DataTableFrame title="Evidence">
      <SimpleTable
        rows={visibleRows}
        empty="No ticker evidence rows are loaded."
        columns={[
          ["source", "Source"],
          ["title", "Item"],
          ["signal", "Signal"],
          ["date", "Date"],
        ]}
      />
    </DataTableFrame>
  );
}

function SimpleTable({ rows, columns, empty }: { rows: Array<Record<string, string>>; columns: Array<[key: string, label: string]>; empty: string }) {
  if (!rows.length) return <div className="px-4 py-6 text-sm text-muted-foreground">{empty}</div>;
  return (
    <table className="w-full min-w-full text-sm">
      <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
        <tr>{columns.map(([, label]) => <th key={label} className="px-3 py-3 font-medium">{label}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index} className="border-b border-border align-top">
            {columns.map(([key]) => (
              <td key={key} className="max-w-[480px] px-3 py-3 leading-6">
                {(key === "signal" || key === "status") && row[key] !== "-" ? <StatusBadge tone={toneFromText(row[key])}>{row[key]}</StatusBadge> : row[key]}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function dossierSummary(row: RowRecord): string {
  const verdict = objectField(row, "verdict");
  if (Object.keys(verdict).length) {
    const parts = [
      textField(verdict, ["summary"]),
      textField(verdict, ["action"]),
      textField(verdict, ["next_action"]),
    ].filter(Boolean);
    return truncateText(parts.join(" | ") || "Decision brief loaded.", 280);
  }

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
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
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

function tickerHeaderMetrics(ticker: TickerPayload | null): MetricSpec[] {
  const tables = ticker?.tables ?? {};
  const quote = latestRow(compactRows(tables.quotes), ["observed_at", "as_of", "date"]);
  const decision = compactRows(tables.decision_queue)[0] ?? compactRows(tables.symbol_decision_snapshot)[0];
  const fundamentals = compactRows(tables.fundamentals)
    .filter((row) => textField(row, ["source"]) === "sec_companyfacts")
    .sort((a, b) => dateSortValue(b, ["filing_date", "period_end"]) - dateSortValue(a, ["filing_date", "period_end"]))[0];
  const metrics = objectField(fundamentals, "metrics");
  const sourceCount = compactRows(tables.source_consensus).length + compactRows(tables.ticker_source_signals).length;
  return [
    ["Price", moneyMetric(quote, "price"), displayField(quote, ["observed_at", "as_of"], "No quote timestamp"), toneFromText(displayField(quote, ["freshness_status"], "loaded"))],
    ["Action", displayField(decision, ["action_grade", "decision_bucket", "decision"], "Not loaded"), displayField(decision, ["freshness_status", "overall_decision_freshness"], "No decision freshness"), toneFromText(displayField(decision, ["action_grade", "freshness_status"], "info"))],
    ["Revenue YoY", ratioMetric(metrics, "revenue_growth"), "SEC company facts", toneFromText(ratioTone(numberFrom(metrics.revenue_growth)))],
    ["Sources", sourceCount ? String(sourceCount) : "0", "consensus and ticker signals", sourceCount ? "good" : "warn"],
  ];
}

function objectField(row: RowRecord | undefined, key: string): RowRecord {
  const value = row?.[key];
  if (value && typeof value === "object" && !Array.isArray(value)) return value as RowRecord;
  if (typeof value === "string" && value.trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(value) as JsonValue;
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as RowRecord : {};
    } catch {
      return {};
    }
  }
  return {};
}

function arrayField(row: RowRecord | undefined, key: string): RowRecord[] {
  const value = row?.[key];
  if (Array.isArray(value)) return value.filter((item) => Boolean(item) && typeof item === "object" && !Array.isArray(item)) as RowRecord[];
  return [];
}

function latestRow(rows: RowRecord[], keys: string[]): RowRecord | undefined {
  return rows.length ? [...rows].sort((a, b) => dateSortValue(b, keys) - dateSortValue(a, keys))[0] : undefined;
}

function dateSortValue(row: RowRecord | undefined, keys: string[]): number {
  if (!row) return 0;
  for (const key of keys) {
    const value = row[key];
    if (!value) continue;
    const date = new Date(String(value));
    if (!Number.isNaN(date.getTime())) return date.getTime();
  }
  return 0;
}

function estimateForPeriod(rows: RowRecord[], period: string): RowRecord {
  return rows.find((row) => textField(row, ["period"]) === period) ?? {};
}

function moneyMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : formatCompactMoney(value);
}

function ratioMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : formatPct(value * 100);
}

function percentMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : formatPct(value);
}

function multipleMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}x`;
}

function numberMetric(row: RowRecord | undefined, key: string): string {
  const value = numberFrom(row?.[key]);
  return value === null ? "-" : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function targetRange(row: RowRecord | undefined): string {
  const low = moneyMetric(row, "low");
  const high = moneyMetric(row, "high");
  return low === "-" && high === "-" ? "-" : `${low} / ${high}`;
}

function numberFrom(value: RowRecord[string]): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function ratioTone(value: number | null): string {
  if (value === null) return "warn";
  return value > 0 ? "good" : value < 0 ? "bad" : "info";
}

function usefulEvidence(row: Record<string, string>): boolean {
  const source = row.source.toLowerCase();
  const title = row.title.toLowerCase();
  const genericTitle = ["technical setups", "liquidity", "earnings setups", "options payoff", "trader filings", "evidence item"].includes(title);
  const genericSource = ["technical", "liquidity", "earnings_setup", "options_payoff", "filings"].includes(source);
  const hasSignal = row.signal !== "-";
  const hasDate = row.date !== "-";
  return !genericTitle && (!genericSource || hasSignal || hasDate);
}

function formatCompactMoney(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}T`;
  if (abs >= 1_000_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}B`;
  if (abs >= 1_000_000) return `${value < 0 ? "-" : ""}$${(abs / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}M`;
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: abs > 1000 ? 0 : 2 });
}
