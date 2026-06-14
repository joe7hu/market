import type { PanelData, TickerPayload } from "@/types";
import { WorkspacePage, type OpenTicker } from "@/views/workspacePage";

import { tickerHeaderMetrics } from "./data";
import {
  DecisionPanel,
  EstimatesPanel,
  EvidencePanel,
  FundamentalsPanel,
  OptionsIntelligencePanel,
  OwnershipPanel,
  PortfolioPanel,
  SourceCoveragePanel,
  TechnicalsPanel,
  ThesisPanel,
  TradingViewChart,
} from "./panels";

export function TickerPage({ symbol, ticker, onOpenTicker }: { symbol: string; ticker: TickerPayload | null; data: PanelData; onOpenTicker: OpenTicker }) {
  const dossier = ticker?.dossier;
  const metrics = tickerHeaderMetrics(ticker);
  const title = ticker?.found === false ? `${symbol} not found` : symbol;
  return (
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Authoritative fundamentals, source-backed evidence, thesis state, and decision context." metrics={metrics}>
      {dossier ? (
        <>
          {dossier.decision ? <DecisionPanel brief={dossier.decision} /> : null}
          <FundamentalsPanel fundamentals={dossier.fundamentals} />
          <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.7fr)]">
            <TradingViewChart symbol={symbol} ticker={ticker} />
            <EstimatesPanel estimates={dossier.estimates} />
          </div>
          <TechnicalsPanel technicals={dossier.technicals} />
          <OptionsIntelligencePanel options={dossier.options} />
          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            <ThesisPanel thesis={dossier.thesis} />
            <OwnershipPanel ownership={dossier.ownership} />
          </div>
          {dossier.portfolio.owned ? <PortfolioPanel portfolio={dossier.portfolio} /> : null}
          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            <SourceCoveragePanel sources={dossier.sources} onOpenTicker={onOpenTicker} />
            <EvidencePanel sources={dossier.sources} />
          </div>
        </>
      ) : null}
    </WorkspacePage>
  );
}
