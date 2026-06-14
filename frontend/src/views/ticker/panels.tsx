import { ExternalLink } from "lucide-react";

import { resolveTradingViewSymbol, tradingViewEmbedUrl } from "@/adapters/tradingView";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { RowRecord, TickerDossier, TickerPayload } from "@/types";
import { displayField, listField, symbolList, textField, titleLabel, toneFromText } from "@/views/rowFormat";
import type { OpenTicker } from "@/views/workspacePage";

import { CoverageBadge, DecisionStat, MetricGrid, ReasonList, SimpleTable } from "./cells";
import {
  arrayField,
  estimateForPeriod,
  moneyMetric,
  moneyOrNumber,
  multipleMetric,
  numberFrom,
  numberMetric,
  objectField,
  optionMove,
  percentMetric,
  presentMetricCells,
  ratioMetric,
  rowList,
  scoreMetric,
  skewDetail,
  targetRange,
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
    <DataTableFrame title="Decision" action={<StatusBadge tone={toneFromText(action)}>{action}</StatusBadge>}>
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
            <SimpleTable rows={setupRows} empty="No decision setup is loaded." columns={[["label", "Plan"], ["value", "Value"]]} />
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

export function FundamentalsPanel({ fundamentals }: { fundamentals: TickerDossier["fundamentals"] }) {
  const sec = fundamentals.sec ?? {};
  const market = fundamentals.market ?? {};
  const hasSec = numberFrom(sec.revenue) !== null;

  const metricRows = presentMetricCells([
    ["Revenue", moneyMetric(sec, "revenue"), "SEC company facts revenue"],
    ["Revenue YoY", ratioMetric(sec, "revenue_growth"), "latest annual period"],
    ["Net Income", moneyMetric(sec, "net_income"), "SEC company facts net income"],
    ["Net Margin", ratioMetric(sec, "net_margin"), "net income / revenue"],
    ["Free Cash Flow", moneyMetric(sec, "free_cash_flow"), "operating cash flow minus capex"],
    ["FCF Margin", ratioMetric(sec, "fcf_margin"), "free cash flow / revenue"],
    ["Assets", moneyMetric(sec, "assets"), "latest balance sheet"],
    ["Liabilities", moneyMetric(sec, "liabilities"), "latest balance sheet"],
    ["Cash", moneyMetric(sec, "cash"), "cash and equivalents"],
    ["Debt / Assets", ratioMetric(sec, "debt_to_assets"), "liabilities / assets"],
  ]);

  const marketRows = presentMetricCells([
    ["Market Cap", moneyMetric(market, "market_cap"), "market data"],
    ["P/S", multipleMetric(market, "ps_ratio"), "sales multiple"],
    ["P/E", multipleMetric(market, "pe_ratio"), "earnings multiple"],
    ["Forward P/E", multipleMetric(market, "forward_pe"), textField(market, ["forward_pe_source"], "forward estimate")],
    ["FCF Yield", ratioMetric(market, "fcf_yield"), "free cash flow yield"],
    ["ROIC", percentMetric(market, "roic"), textField(market, ["roic_source"], "capital returns")],
  ]);

  return (
    <DataTableFrame
      title="Authoritative Fundamentals"
      action={sec.source_url ? (
        <Button asChild type="button" variant="outline" size="sm">
          <a href={String(sec.source_url)} target="_blank" rel="noreferrer"><ExternalLink /> SEC source</a>
        </Button>
      ) : <CoverageBadge coverage={fundamentals.coverage} />}
    >
      <div className="grid gap-0 lg:grid-cols-[1fr_0.65fr]">
        <div className="border-b border-border p-4 lg:border-b-0 lg:border-r">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <StatusBadge tone={hasSec ? "good" : "warn"}>{hasSec ? textField(sec, ["form_type"], "SEC filing") : "No SEC row"}</StatusBadge>
            <span className="text-sm text-muted-foreground">
              {hasSec ? `${displayField(sec, ["filing_date", "period_end"])} from SEC company facts` : "Direct company-facts metrics are not loaded for this ticker."}
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

export function TechnicalsPanel({ technicals }: { technicals: TickerDossier["technicals"] }) {
  const trend = technicals.trend ?? {};
  const momentum = technicals.momentum ?? {};
  const sepa = technicals.sepa ?? {};
  const liquidity = technicals.liquidity ?? {};

  const trendRows = presentMetricCells([
    ["Close", moneyMetric(trend, "close"), displayField(momentum, ["as_of"], "latest bar")],
    ["20D MA", moneyMetric(trend, "ma20"), "short-term trend"],
    ["50D MA", moneyMetric(trend, "ma50"), "intermediate trend"],
    ["200D MA", moneyMetric(trend, "ma200"), "long-term trend"],
    ["Drawdown", ratioMetric(trend, "drawdown_from_high"), "from 52w high"],
    ["Tech Score", scoreMetric(momentum, "technical_score"), "composite momentum"],
  ]);
  const momentumRows = presentMetricCells([
    ["20D Return", ratioMetric(momentum, "return_20d"), "1 month"],
    ["3M Return", ratioMetric(momentum, "return_3m"), "quarter"],
    ["YTD Return", ratioMetric(momentum, "return_ytd"), "year to date"],
    ["1Y Return", ratioMetric(momentum, "return_1y"), "trailing year"],
    ["Rel Volume", numberMetric(momentum, "rel_volume_1m"), "1m vs baseline"],
    ["ATR %", ratioMetric(momentum, "atr_pct_1m"), "1m average true range"],
  ]);
  const checklist = objectField(sepa, "checklist");
  const checklistRows = Object.entries(checklist).map(([key, value]) => ({
    check: titleLabel(key),
    status: value === true ? "Pass" : value === false ? "Fail" : displayField({ value } as RowRecord, ["value"], "-"),
  }));

  return (
    <DataTableFrame
      title="Technicals & Trend"
      action={<CoverageBadge coverage={technicals.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <h3 className="mb-3 text-sm font-semibold">Trend Structure</h3>
          <MetricGrid rows={trendRows} empty="No technical trend metrics are loaded." />
          <h3 className="mb-3 mt-4 text-sm font-semibold">Momentum</h3>
          <MetricGrid rows={momentumRows} empty="No momentum metrics are loaded." />
        </div>
        <div className="p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">SEPA Stage</h3>
            {sepa.stage ? <StatusBadge tone={toneFromText(textField(sepa, ["verdict", "stage"], "info"))}>{titleLabel(textField(sepa, ["stage"], "-"))}</StatusBadge> : null}
            <span className="text-sm text-muted-foreground">{displayField(sepa, ["verdict"])} · score {scoreMetric(sepa, "score")}</span>
          </div>
          <SimpleTable rows={checklistRows} empty="No SEPA checklist is loaded." columns={[["check", "Trend check"], ["status", "Status"]]} />
          <h3 className="mb-3 mt-4 text-sm font-semibold">Liquidity</h3>
          <MetricGrid
            rows={presentMetricCells([
              ["Grade", displayField(liquidity, ["grade"], "-").replace(/_/g, " "), "tradeability"],
              ["Avg $ Vol", moneyMetric(liquidity, "avg_dollar_volume"), "60d average"],
              ["1% ADV Impact", liquidity.impact_1pct_adv_bps != null ? `${numberMetric(liquidity, "impact_1pct_adv_bps")} bps` : "-", "modeled slippage"],
            ])}
            empty="No liquidity metrics are loaded."
          />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function EstimatesPanel({ estimates }: { estimates: TickerDossier["estimates"] }) {
  const analyst = estimates.analyst ?? {};
  const setup = estimates.earnings_setup ?? {};
  const event = estimates.earnings_event ?? {};
  const earnings = arrayField(analyst as RowRecord, "earnings_estimate");
  const revenue = arrayField(analyst as RowRecord, "revenue_estimate");
  const targets = objectField(analyst as RowRecord, "price_targets");
  const currentYearEps = estimateForPeriod(earnings, "0y");
  const nextYearEps = estimateForPeriod(earnings, "+1y");
  const currentYearRevenue = estimateForPeriod(revenue, "0y");
  const nextYearRevenue = estimateForPeriod(revenue, "+1y");
  const estimateRows = presentMetricCells([
    ["CY Revenue", moneyMetric(currentYearRevenue, "avg"), ratioMetric(currentYearRevenue, "growth")],
    ["NY Revenue", moneyMetric(nextYearRevenue, "avg"), ratioMetric(nextYearRevenue, "growth")],
    ["CY EPS", numberMetric(currentYearEps, "avg"), ratioMetric(currentYearEps, "growth")],
    ["NY EPS", numberMetric(nextYearEps, "avg"), ratioMetric(nextYearEps, "growth")],
    ["Target Mean", moneyMetric(targets, "mean"), "analyst price targets"],
    ["Target Range", targetRange(targets), "low / high"],
  ]);
  const setupRows = presentMetricCells([
    ["Next Event", displayField(event, ["event_date"], "Not scheduled"), displayField(event, ["event_type"], "earnings")],
    ["Setup", displayField(setup, ["verdict"], "Not loaded"), `score ${scoreMetric(setup, "score")}`],
    ["Revision", scoreMetric(setup, "revision_score"), "estimate revisions"],
    ["Surprise", scoreMetric(setup, "surprise_score"), "historical surprise"],
    ["Sentiment", scoreMetric(setup, "sentiment_score"), "pre-earnings sentiment"],
    ["Est. Spread", scoreMetric(setup, "estimate_spread_score"), "analyst dispersion"],
  ]);
  return (
    <DataTableFrame title="Estimates & Earnings" action={<CoverageBadge coverage={estimates.coverage} />}>
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <div className="mb-3 text-sm text-muted-foreground">{analyst.as_of ? `yfinance snapshot ${displayField(analyst as RowRecord, ["as_of"])}` : "No analyst estimate row is loaded."}</div>
          <MetricGrid rows={estimateRows} empty="No analyst estimate metrics are loaded." />
        </div>
        <div className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Earnings Setup</h3>
          <MetricGrid rows={setupRows} empty="No earnings setup is loaded." />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function OptionsIntelligencePanel({ options }: { options: TickerDossier["options"] }) {
  const signal = options.signal ?? {};
  const expiries = rowList(options.expiries).slice(0, 8);
  const capability = rowList(options.capabilities).find((row) => textField(row, ["provider"]) === "tradingview");
  const unavailableRows = rowList(options.unavailable_signals).slice(0, 6).map((row) => ({
    signal: displayField(row, ["signal"], "Signal"),
    reason: displayField(row, ["reason"], "Unavailable from TradingView V1"),
  }));
  const metrics = presentMetricCells([
    ["Status", displayField(signal, ["status"], "Missing"), displayField(signal, ["source"], "No options signal row")],
    ["ATM IV", ratioMetric(signal, "atm_iv"), displayField(signal, ["iv_regime"], "IV regime unavailable")],
    ["Expected Move", optionMove(signal), displayField(signal, ["nearest_expiry"], "No expiry")],
    ["Skew", displayField(signal, ["skew_signal"], "-"), skewDetail(signal)],
    ["Spread", displayField(signal, ["spread_quality"], "-"), "bid/ask quality"],
    ["Hedge", displayField(signal, ["hedge_summary"], "-"), "25-delta put candidate"],
    ["Income", displayField(signal, ["income_summary"], "-"), "30-delta call candidate"],
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
      action={capability ? <StatusBadge tone={capability.supports_open_interest ? "good" : "warn"}>{displayField(capability, ["status"], "limited")}</StatusBadge> : <CoverageBadge coverage={options.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.95fr)_minmax(360px,0.65fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No options signal row is loaded for this ticker." />
          <div className="mt-4 overflow-x-auto">
            <SimpleTable
              rows={expiryRows}
              empty="No expiry-level options signals are loaded."
              columns={[["expiry", "Expiry"], ["dte", "DTE"], ["atm", "ATM"], ["iv", "IV"], ["move", "Move"], ["skew", "Skew"], ["spread", "Spread"]]}
            />
          </div>
        </div>
        <div className="p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">Positioning Gates</h3>
            <StatusBadge tone="warn">OI/volume missing</StatusBadge>
          </div>
          <SimpleTable rows={unavailableRows} empty="No unavailable options signal metadata is loaded." columns={[["signal", "Signal"], ["reason", "Why"]]} />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function ThesisPanel({ thesis }: { thesis: TickerDossier["thesis"] }) {
  const state = thesis.state ?? {};
  const packet = thesis.research_packet ?? {};
  const bull = listField(packet as RowRecord, ["bull_case"]).slice(0, 4);
  const bear = listField(packet as RowRecord, ["bear_case"]).slice(0, 4);
  const whyNow = listField(packet as RowRecord, ["why_now"]).slice(0, 3);
  const entry = objectField(packet as RowRecord, "entry_plan");
  const stateRows = [
    { field: "Decision", value: displayField(packet as RowRecord, ["decision"], displayField(state, ["status"], "-")) },
    { field: "Conviction", value: displayField(packet as RowRecord, ["conviction"], "-") },
    { field: "Thesis", value: displayField(state, ["thesis"], "No thesis text loaded") },
    { field: "Invalidation", value: displayField(state, ["invalidation"], "No invalidation loaded") },
    { field: "Ideal Entry", value: displayField(entry, ["ideal_entry"], "Review required") },
    { field: "Reviewed", value: displayField(state, ["last_reviewed"], displayField(packet as RowRecord, ["created_at"], "-")) },
  ];
  return (
    <DataTableFrame
      title="Thesis & Research"
      action={state.needs_review ? <StatusBadge tone="warn">Needs review</StatusBadge> : <CoverageBadge coverage={thesis.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.9fr)_minmax(320px,0.7fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <SimpleTable rows={stateRows} empty="No ticker thesis state is loaded." columns={[["field", "Field"], ["value", "Value"]]} />
        </div>
        <div className="grid gap-4 p-4">
          <ReasonList title="Bull Case" rows={bull} empty="No bull case loaded." />
          <ReasonList title="Bear Case" rows={bear} empty="No bear case loaded." />
          {whyNow.length ? <ReasonList title="Why Now" rows={whyNow} empty="" /> : null}
        </div>
      </div>
    </DataTableFrame>
  );
}

export function OwnershipPanel({ ownership }: { ownership: TickerDossier["ownership"] }) {
  const institutional = ownership.institutional ?? {};
  const filings = rowList(ownership.filings).slice(0, 12);
  const metrics = presentMetricCells([
    ["Tracked Holders", numberMetric(institutional, "holders"), "13F + disclosures"],
    ["Net Activity", numberMetric(institutional, "net_activity"), `${numberMetric(institutional, "net_buys")} buys / ${numberMetric(institutional, "net_sells")} sells`],
    ["Disclosed Value", moneyMetric(institutional, "total_value"), "estimated notional"],
    ["Latest Filed", displayField(institutional, ["latest_filed"], "-"), "most recent filing"],
  ]);
  const filingRows = filings.map((row) => ({
    filer: displayField(row, ["filer_name", "trader_name"], "Filer"),
    action: displayField(row, ["action"], "-"),
    amount: displayField(row, ["amount"], "-"),
    date: displayField(row, ["filed_date", "event_date"], "-"),
  }));
  const holders = listField(institutional as RowRecord, ["holder_names", "investors"]).slice(0, 8);
  return (
    <DataTableFrame
      title="Ownership & Filings"
      action={<CoverageBadge coverage={ownership.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No ownership consensus is loaded." />
          {holders.length ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {holders.map((holder) => <StatusBadge key={holder} tone="info">{holder}</StatusBadge>)}
            </div>
          ) : null}
        </div>
        <div className="overflow-x-auto p-0">
          <SimpleTable
            rows={filingRows}
            empty="No tracked insider or institutional filings are loaded."
            columns={[["filer", "Filer"], ["action", "Action"], ["amount", "Amount"], ["date", "Filed"]]}
          />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function PortfolioPanel({ portfolio }: { portfolio: TickerDossier["portfolio"] }) {
  const position = portfolio.position ?? {};
  const fit = portfolio.fit ?? {};
  const correlations = rowList(portfolio.correlations).slice(0, 6);
  const riskCards = rowList(portfolio.risk_cards).slice(0, 4);
  const metrics = presentMetricCells([
    ["Market Value", moneyMetric(position, "market_value"), "current position"],
    ["Weight", percentMetric(position, "weight"), "of portfolio"],
    ["Quantity", numberMetric(position, "quantity"), "shares"],
    ["Cost Basis", moneyMetric(position, "cost_basis"), "average price"],
    ["Unrealized", ratioMetric(position, "unrealized_pnl_pct"), "open P&L"],
    ["Exposure", displayField(fit, ["current_exposure"], "-"), displayField(fit, ["theme_concentration"], "")],
  ]);
  const correlationRows = correlations.map((row) => ({
    peer: displayField(row, ["peer_symbol"], "-"),
    correlation: numberMetric(row, "correlation"),
  }));
  const riskRows = riskCards.map((row) => ({
    risk: displayField(row, ["risk_level", "title", "name"], "Risk"),
    detail: displayField(row, ["summary", "action", "detail"], "-"),
  }));
  return (
    <DataTableFrame
      title="Portfolio Fit"
      action={<StatusBadge tone={portfolio.owned ? "good" : "muted"}>{portfolio.owned ? "Owned" : "Unowned"}</StatusBadge>}
    >
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No portfolio position is loaded." />
        </div>
        <div className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Risk & Correlation</h3>
          <SimpleTable rows={riskRows} empty="No portfolio risk cards are loaded." columns={[["risk", "Risk"], ["detail", "Detail"]]} />
          {correlationRows.length ? (
            <div className="mt-3 overflow-x-auto">
              <SimpleTable rows={correlationRows} empty="" columns={[["peer", "Peer"], ["correlation", "Correlation"]]} />
            </div>
          ) : null}
        </div>
      </div>
    </DataTableFrame>
  );
}

export function SourceCoveragePanel({ sources, onOpenTicker }: { sources: TickerDossier["sources"]; onOpenTicker: OpenTicker }) {
  const consensusRows = rowList(sources.consensus);
  const signalRows = rowList(sources.signals);
  const visibleRows = [
    ...consensusRows.slice(0, 8).map((row) => ({
      source: displayField(row, ["source_name"], "Source"),
      type: displayField(row, ["content_type"], "-"),
      net: displayField(row, ["net_consensus"], "-"),
      latest: displayField(row, ["latest_at"], "-"),
    })),
    ...signalRows.slice(0, 6).map((row) => ({
      source: displayField(row, ["source_name"], "Signal"),
      type: displayField(row, ["signal_type"], "-"),
      net: displayField(row, ["sentiment", "confidence"], "-"),
      latest: displayField(row, ["observed_at"], "-"),
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
      ) : <CoverageBadge coverage={sources.coverage} />}
    >
      <SimpleTable
        rows={visibleRows}
        empty="No source coverage rows are loaded."
        columns={[["source", "Source"], ["type", "Type"], ["net", "Signal"], ["latest", "Latest"]]}
      />
    </DataTableFrame>
  );
}

export function EvidencePanel({ sources }: { sources: TickerDossier["sources"] }) {
  const visibleRows = rowList(sources.evidence)
    .map((row) => ({
      source: displayField(row, ["source"], "Source"),
      title: displayField(row, ["title"], "Evidence item"),
      signal: displayField(row, ["signal"], "-"),
      date: displayField(row, ["date"], "-"),
    }))
    .filter((row) => row.title !== "Evidence item" || row.signal !== "-" || row.date !== "-")
    .slice(0, 12);
  return (
    <DataTableFrame title="Evidence">
      <SimpleTable
        rows={visibleRows}
        empty="No ticker evidence rows are loaded."
        columns={[["source", "Source"], ["title", "Item"], ["signal", "Signal"], ["date", "Date"]]}
      />
    </DataTableFrame>
  );
}
