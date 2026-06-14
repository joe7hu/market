import type { PanelData, TickerPayload } from "@/types";
import { rows } from "@/utils";
import { symbolList } from "@/views/rowFormat";
import { WorkspacePage, type OpenTicker } from "@/views/workspacePage";

import { compactRows, tickerHeaderMetrics } from "./data";
import {
  AnalystEstimatePanel,
  DecisionPanel,
  EvidencePanel,
  FundamentalsPanel,
  OptionsIntelligencePanel,
  SourceCoveragePanel,
  ThesisPanel,
  TradingViewChart,
} from "./panels";

export function TickerPage({ symbol, ticker, data, onOpenTicker }: { symbol: string; ticker: TickerPayload | null; data: PanelData; onOpenTicker: OpenTicker }) {
  const tables = ticker?.tables ?? {};
  const thesisRows = rows(data.thesisMonitor).filter((row) => symbolList(row).includes(symbol));
  const metrics = tickerHeaderMetrics(ticker);
  const title = ticker?.found === false ? `${symbol} not found` : symbol;
  return (
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Authoritative fundamentals, source-backed evidence, thesis state, and decision context." metrics={metrics}>
      <FundamentalsPanel ticker={ticker} />
      {ticker?.decision_brief ? <DecisionPanel brief={ticker.decision_brief} /> : null}
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.7fr)]">
        <TradingViewChart symbol={symbol} ticker={ticker} />
        <AnalystEstimatePanel rows={compactRows(tables.analyst_estimates)} />
      </div>
      <OptionsIntelligencePanel ticker={ticker} />
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
