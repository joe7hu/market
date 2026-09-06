import type { PanelData, TickerPayload } from "@/types";
import { loadTickerDecisionSnapshot, startRefreshJob, type TickerDecisionSnapshot } from "@/api/panel";
import { useEffect, useRef, useState } from "react";
import { WorkspacePage, type OpenTicker } from "@/views/workspacePage";

import { tickerHeaderMetrics } from "./data";
import {
  DecisionPanel,
  TickerDecisionPanel,
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
  const [collecting, setCollecting] = useState<string | null>(null);
  const [decisionSnapshot, setDecisionSnapshot] = useState<TickerDecisionSnapshot | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const snapshotGeneration = useRef(0);
  const decisionRevision = ticker?.ticker_decision?.decision_revision;
  const loadSnapshot = async () => {
    const requestGeneration = ++snapshotGeneration.current;
    setDecisionSnapshot(null);
    if (!decisionRevision) return;
    setSnapshotLoading(true);
    setSnapshotError(null);
    try {
      const loaded = await loadTickerDecisionSnapshot(symbol);
      if (snapshotGeneration.current !== requestGeneration) return;
      if (loaded.ticker.trim().toUpperCase() !== symbol.trim().toUpperCase() || loaded.decision_revision !== decisionRevision) {
        throw new Error("The decision changed. Refresh this ticker before acting.");
      }
      setDecisionSnapshot(loaded);
    } catch (error) {
      if (snapshotGeneration.current === requestGeneration) setSnapshotError(error instanceof Error ? error.message : "Decision details could not load. Retry before acting.");
    } finally {
      if (snapshotGeneration.current === requestGeneration) setSnapshotLoading(false);
    }
  };
  useEffect(() => {
    void loadSnapshot();
    return () => { snapshotGeneration.current += 1; };
  }, [symbol, decisionRevision]);
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
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Investment case, supporting evidence, portfolio effect, and next action." metrics={metrics}>
      {dossier && !notFound ? (
        <>
          <p className="text-xs text-muted-foreground">The header price is a research reference at the displayed observation time. Trade entry prices and option quotes must pass the separate trade checks below.</p>
          <ThesisPanel thesis={dossier.thesis} />
          {ticker?.ticker_decision ? (
            <TickerDecisionPanel
              decision={ticker.ticker_decision}
              snapshot={decisionSnapshot}
              snapshotLoading={snapshotLoading}
              snapshotError={snapshotError}
              onLoadSnapshot={loadSnapshot}
              collecting={collecting}
              onCollect={async (job) => {
                setCollecting(job);
                try {
                  await startRefreshJob(job);
                } finally {
                  setCollecting(null);
                }
              }}
            />
          ) : <DecisionPanel brief={dossier.decision} />}
          <EvidencePanel sources={dossier.sources} thesisEvidence={Array.isArray(dossier.thesis.state?.source_evidence) ? dossier.thesis.state.source_evidence as import("@/types").RowRecord[] : []} />
          <FundamentalsPanel fundamentals={dossier.fundamentals} />
          <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.7fr)]">
            <TradingViewChart symbol={symbol} ticker={ticker} />
            <EstimatesPanel estimates={dossier.estimates} />
          </div>
          <TechnicalsPanel technicals={dossier.technicals} />
          <OptionsIntelligencePanel options={dossier.options} />
          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            <OwnershipPanel ownership={dossier.ownership} />
          </div>
          {showPortfolio ? <PortfolioPanel portfolio={dossier.portfolio} /> : null}
          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            <SourceCoveragePanel sources={dossier.sources} onOpenTicker={onOpenTicker} />
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
