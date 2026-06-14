import { ExternalLink } from "lucide-react";

import { resolveTradingViewSymbol, tradingViewEmbedUrl } from "@/adapters/tradingView";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { RowRecord, TickerPayload } from "@/types";
import { displayField, listField, symbolList, textField, toneFromText } from "@/views/rowFormat";
import type { OpenTicker } from "@/views/workspacePage";

import { DecisionStat, MetricGrid, ReasonList, SimpleTable } from "./cells";
import {
  arrayField,
  compactRows,
  dateSortValue,
  estimateForPeriod,
  latestRow,
  liquidityDetail,
  moneyMetric,
  moneyOrNumber,
  multipleMetric,
  numberMetric,
  objectField,
  optionMove,
  percentMetric,
  presentMetricCells,
  ratioMetric,
  skewDetail,
  targetRange,
  unavailableSignals,
  usefulEvidence,
} from "./data";

export function DecisionPanel({ brief }: { brief: RowRecord }) {
  const verdict = objectField(brief, "verdict");
  const setup = objectField(brief, "setup");
  const riskPlan = objectField(brief, "risk_plan");
  const quote = objectField(brief, "canonical_quote");
  const action = displayField(verdict, ["action"], "Watch");
  const supports = listField(brief, ["evidence_for"]).slice(0, 4);
  const concerns = listField(brief, ["evidence_against"]).slice(0, 4);
  const unknowns = listField(brief, ["unknowns"]).slice(0, 3);
  const setupRows = [
    { label: "Entry", value: displayField(setup, ["entry_zone"], "No entry plan loaded") },
    { label: "Invalidation", value: displayField(riskPlan, ["invalidation"], displayField(setup, ["invalidation_level"], "No invalidation loaded")) },
    { label: "Target", value: displayField(setup, ["target_range"], "No target loaded") },
    { label: "Review", value: displayField(setup, ["review_date"], "No review date loaded") },
  ];
  return (
    <DataTableFrame
      title="Decision"
      action={<StatusBadge tone={toneFromText(action)}>{action}</StatusBadge>}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.85fr)_minmax(360px,0.65fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <div className="mb-4 grid gap-2 sm:grid-cols-3">
            <DecisionStat label="Confidence" value={displayField(verdict, ["confidence"], "-")} detail={displayField(verdict, ["freshness"], "freshness not loaded")} />
            <DecisionStat label="Price" value={moneyMetric(quote, "price")} detail={displayField(quote, ["observed_at"], "quote timestamp missing")} />
            <DecisionStat label="Timeframe" value={displayField(setup, ["timeframe"], "-")} detail={displayField(setup, ["catalyst"], "no catalyst loaded")} />
          </div>
          <p className="mb-3 text-base font-medium leading-7">{displayField(verdict, ["summary"], "No decision summary loaded.")}</p>
          <p className="text-sm leading-6 text-muted-foreground">{displayField(verdict, ["next_action"], "No next action loaded.")}</p>
          <div className="mt-4 overflow-x-auto">
            <SimpleTable
              rows={setupRows}
              empty="No decision setup is loaded."
              columns={[
                ["label", "Plan"],
                ["value", "Value"],
              ]}
            />
          </div>
        </div>
        <div className="grid gap-4 p-4">
          <ReasonList title="Why It Could Work" rows={supports} empty="No positive evidence loaded." />
          <ReasonList title="Why It Is Gated" rows={concerns} empty="No risk evidence loaded." />
          {unknowns.length ? <ReasonList title="Still Unknown" rows={unknowns} empty="" /> : null}
        </div>
      </div>
    </DataTableFrame>
  );
}

export function TradingViewChart({ symbol, ticker }: { symbol: string; ticker: TickerPayload | null }) {
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

export function FundamentalsPanel({ ticker }: { ticker: TickerPayload | null }) {
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

export function AnalystEstimatePanel({ rows }: { rows: RowRecord[] }) {
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

export function OptionsIntelligencePanel({ ticker }: { ticker: TickerPayload | null }) {
  const tickerSignal = latestRow(compactRows(ticker?.tables?.options_ticker_signals), ["as_of"]);
  const expiries = compactRows(ticker?.tables?.options_expiry_signals)
    .sort((a, b) => dateSortValue(a, ["expiry"]) - dateSortValue(b, ["expiry"]))
    .slice(0, 8);
  const capability = compactRows(ticker?.tables?.options_provider_capabilities).find((row) => textField(row, ["provider"]) === "tradingview");
  const unavailableRows = unavailableSignals(tickerSignal).slice(0, 6).map((row) => ({
    signal: displayField(row, ["signal"], "Signal"),
    reason: displayField(row, ["reason"], "Unavailable from TradingView V1"),
  }));
  const metrics = presentMetricCells([
    ["Status", displayField(tickerSignal, ["status"], "Missing"), displayField(tickerSignal, ["source"], "No options signal row")],
    ["ATM IV", ratioMetric(tickerSignal, "atm_iv"), displayField(tickerSignal, ["iv_regime"], "IV regime unavailable")],
    ["Expected Move", optionMove(tickerSignal), displayField(tickerSignal, ["nearest_expiry"], "No expiry")],
    ["Skew", displayField(tickerSignal, ["skew_signal"], "-"), skewDetail(tickerSignal)],
    ["Spread", displayField(tickerSignal, ["spread_quality"], "-"), liquidityDetail(tickerSignal)],
    ["Hedge", displayField(tickerSignal, ["hedge_summary"], "-"), "25-delta put candidate"],
    ["Income", displayField(tickerSignal, ["income_summary"], "-"), "30-delta call candidate"],
  ]);
  const expiryRows = expiries.map((row) => ({
    expiry: displayField(row, ["expiry"], "-"),
    dte: displayField(row, ["dte"], "-"),
    atm: moneyOrNumber(row, "atm_strike"),
    iv: ratioMetric(row, "atm_iv"),
    move: optionMove(row),
    skew: skewDetail(row),
    spread: displayField(row, ["spread_quality"], "-"),
  }));
  return (
    <DataTableFrame
      title="Options Intelligence"
      action={capability ? <StatusBadge tone={capability.supports_open_interest ? "good" : "warn"}>{displayField(capability, ["status"], "limited")}</StatusBadge> : null}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.95fr)_minmax(360px,0.65fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No options signal row is loaded for this ticker." />
          <div className="mt-4 overflow-x-auto">
            <SimpleTable
              rows={expiryRows}
              empty="No expiry-level options signals are loaded."
              columns={[
                ["expiry", "Expiry"],
                ["dte", "DTE"],
                ["atm", "ATM"],
                ["iv", "IV"],
                ["move", "Move"],
                ["skew", "Skew"],
                ["spread", "Spread"],
              ]}
            />
          </div>
        </div>
        <div className="p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">Positioning Gates</h3>
            <StatusBadge tone="warn">OI/volume missing</StatusBadge>
          </div>
          <SimpleTable
            rows={unavailableRows}
            empty="No unavailable options signal metadata is loaded."
            columns={[
              ["signal", "Signal"],
              ["reason", "Why"],
            ]}
          />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function ThesisPanel({ rows }: { rows: RowRecord[] }) {
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

export function SourceCoveragePanel({ consensusRows, signalRows, onOpenTicker }: { consensusRows: RowRecord[]; signalRows: RowRecord[]; onOpenTicker: OpenTicker }) {
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

export function EvidencePanel({ rows }: { rows: RowRecord[] }) {
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
