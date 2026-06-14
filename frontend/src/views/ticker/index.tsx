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
  const notFound = ticker?.found === false;
  const title = notFound ? `${symbol} not found` : symbol;
  const portfolio = dossier?.portfolio;
  const showPortfolio = Boolean(
    portfolio &&
      (portfolio.owned ||
        Object.keys(portfolio.fit ?? {}).length ||
        (portfolio.correlations?.length ?? 0) ||
        (portfolio.risk_cards?.length ?? 0)),
  );
  return (
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Authoritative fundamentals, source-backed evidence, thesis state, and decision context." metrics={metrics}>
      {dossier && !notFound ? (
        <>
          <DecisionPanel brief={dossier.decision} />
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
          {showPortfolio ? <PortfolioPanel portfolio={dossier.portfolio} /> : null}
          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            <SourceCoveragePanel sources={dossier.sources} onOpenTicker={onOpenTicker} />
            <EvidencePanel sources={dossier.sources} />
          </div>
        </>
      ) : (
        <div className="rounded-md border border-border bg-background px-4 py-6 text-sm text-muted-foreground">
          No dossier data is loaded for {symbol}.
        </div>
      )}
    </WorkspacePage>
  );
}
