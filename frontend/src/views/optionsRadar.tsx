import {useMemo, useState } from "react";
import {promoteStrategyMutation } from "@/api";
import {StatusBadge } from "@/components/market/workstation";
import {PanelData } from "@/types";
import {displayField, numberField, textField } from "./rowFormat";
import {formatDate, sessionBadge } from "./optionsRadarFormat";
import {latestBy, latestValidationBy } from "./optionsRadarData";
import {tabButtonClass, rows, rowsForDisplayTime, uniqueText, countWhere, optionThesisAgentState, stateOf } from "./optionsRadar/helpers";
import {SignalBriefPanel } from "./optionsRadar/signalBrief";
import {CandidateEventsTable } from "./optionsRadar/candidateTable";
import {MissedWinnersTable, LearningProgressPanel, CohortResultsTable } from "./optionsRadar/learningPanels";
import {StrategyProposalsTable } from "./optionsRadar/strategyProposals";
import {WorkspacePage, OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};

export function OptionsRadarPage({ data, onOpenTicker, onRefresh }: OptionsRadarPageProps) {
  const [activeTab, setActiveTab] = useState<"signals" | "learning">("signals");
  const [promotingProposal, setPromotingProposal] = useState<string | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const radarAlerts = rows(data.radarAlert);
  const missedWinners = rows(data.missedWinnerEvent);
  const proposals = rows(data.strategyMutationProposal);
  const backtests = rows(data.strategyBacktestResult);
  const forwardTests = rows(data.strategyForwardTestResult);
  const thesisRequests = rows(data.agentThesisRequest);
  const thesisValidations = rows(data.agentThesisValidation);
  const postmortemRequests = rows(data.agentPostmortemRequest).filter((row) => textField(row, ["status"]).toLowerCase() !== "imported");
  const postmortems = rows(data.agentPostmortem).filter((row) => textField(row, ["status"]).toLowerCase() !== "imported");
  const agentTheses = rows(data.agentThesis);
  const candidateMarks = rows(data.candidateEventMark);
  const candidateAttributions = rows(data.candidateEventAttribution);
  const cohortResults = rows(data.strategyCohortResult);
  const opportunityRows = rows(data.optionRadarOpportunity);
  const strategyVersions = rows(data.optionStrategyVersions);
  const radarSummary = rows(data.optionRadarSummary)[0];
  const professionalContract = numberField(radarSummary, ["contract_version"], 0) >= 2;
  const latestCandidateTime = textField(radarSummary, ["publication_cutoff", "latest_candidate_time"]);
  const marketSession = textField(radarSummary, ["market_session"]);
  const frozenToRth = textField(radarSummary, ["frozen_to_last_rth"]) === "Yes";
  const optionThesisAgent = optionThesisAgentState(data);

  const currentOpportunityRows = useMemo(
    () => professionalContract ? opportunityRows : rowsForDisplayTime(opportunityRows, latestCandidateTime),
    [latestCandidateTime, opportunityRows, professionalContract],
  );
  const opportunityCandidates = currentOpportunityRows;
  const enrichedOpportunityCandidates = currentOpportunityRows;
  const opportunityTickers = useMemo(() => uniqueText(opportunityCandidates, "ticker"), [opportunityCandidates]);

  const latestBacktestByProposal = useMemo(() => latestBy(backtests, "proposal_id", "evaluated_at"), [backtests]);
  const latestForwardByProposal = useMemo(() => latestBy(forwardTests, "proposal_id", "evaluated_at"), [forwardTests]);
  const latestCandidateMarkByEvent = useMemo(() => latestBy(candidateMarks, "event_id", "mark_time"), [candidateMarks]);
  const latestCandidateAttributionByEvent = useMemo(() => latestBy(candidateAttributions, "event_id", "snapshot_time"), [candidateAttributions]);
  const latestThesisRequestByEvent = useMemo(() => latestBy(thesisRequests, "event_id", "created_at"), [thesisRequests]);
  const latestThesisValidationByEvent = useMemo(() => latestValidationBy(thesisValidations, "candidate_event_id"), [thesisValidations]);
  const latestAgentThesisByTicker = useMemo(() => latestBy(agentTheses, "ticker", "created_at"), [agentTheses]);

  const opportunityTickerCount = numberField(radarSummary, ["shortlist_count", "opportunity_tickers_current"], opportunityTickers.length);
  const scannedTickerCount = numberField(radarSummary, ["scanned_contracts", "scanned_tickers_current"], 0);
  const fireCount = numberField(radarSummary, ["ready_count", "fire_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "READY"));
  const setupCount = numberField(radarSummary, ["setup_count", "setup_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "SETUP"));

  const latestSnapshot = textField(radarSummary, ["latest_complete_quote_time", "latest_snapshot_time"]);
  const snapshotLabel = textField(radarSummary, ["latest_snapshot_label"]);
  const displayStrategyVersion = textField(radarSummary, ["strategy_version"]);
  const latestStrategy = strategyVersions.find((row) => textField(row, ["strategy_version"]) === displayStrategyVersion) ?? strategyVersions[0];

  async function handlePromoteProposal(proposalId: string) {
    if (!proposalId || promotingProposal) return;
    setPromotingProposal(proposalId);
    setPromotionError(null);
    try {
      await promoteStrategyMutation(proposalId, "joe");
      await onRefresh();
    } catch (error) {
      setPromotionError(error instanceof Error ? error.message : "Promotion failed");
    } finally {
      setPromotingProposal(null);
    }
  }

  return (
    <WorkspacePage
      eyebrow="Options Radar"
      title="Options Decision Radar"
      subtitle="Executable long-option and cash-secured-put setups ranked by quality, risk, collateral, and forward evidence."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          {(() => {
            const badge = sessionBadge(marketSession, frozenToRth, latestSnapshot);
            return <StatusBadge tone={badge.tone}>{badge.label}</StatusBadge>;
          })()}
          <StatusBadge tone="info">{displayField(latestStrategy, ["strategy_version", "strategy_name"], "No strategy")}</StatusBadge>
        </div>
      }
    >
      <SignalBriefPanel
        rows={currentOpportunityRows}
        activeAlertCount={radarAlerts.length}
        fireCount={fireCount}
        setupCount={setupCount}
        scannedTickerCount={scannedTickerCount}
        opportunityTickerCount={opportunityTickerCount}
        latestSnapshot={latestSnapshot}
        snapshotLabel={snapshotLabel}
        latestCandidateTime={latestCandidateTime}
        onOpenTicker={onOpenTicker}
      />
      <div className="flex w-fit rounded-md border border-border bg-muted p-1">
        <button type="button" className={tabButtonClass(activeTab === "signals")} onClick={() => setActiveTab("signals")}>Signals</button>
        <button type="button" className={tabButtonClass(activeTab === "learning")} onClick={() => setActiveTab("learning")}>Learning</button>
      </div>
      {activeTab === "signals" ? (
        <CandidateEventsTable
          rows={enrichedOpportunityCandidates}
          thesisRequestByEvent={latestThesisRequestByEvent}
          latestMarkByEvent={latestCandidateMarkByEvent}
          latestAttributionByEvent={latestCandidateAttributionByEvent}
          latestThesisValidationByEvent={latestThesisValidationByEvent}
          latestAgentThesisByTicker={latestAgentThesisByTicker}
          agentRuntime={optionThesisAgent}
          onOpenTicker={onOpenTicker}
        />
      ) : (
        <div className="space-y-4">
        <LearningProgressPanel
          opportunities={enrichedOpportunityCandidates}
          latestMarkByEvent={latestCandidateMarkByEvent}
          latestAttributionByEvent={latestCandidateAttributionByEvent}
          cohorts={cohortResults}
          proposals={proposals}
          missedWinners={missedWinners}
          postmortemRequests={postmortemRequests}
          postmortems={postmortems}
        />
        <CohortResultsTable rows={cohortResults} />
        {missedWinners.length ? <MissedWinnersTable rows={missedWinners} onOpenTicker={onOpenTicker} /> : null}
        <StrategyProposalsTable
            rows={proposals}
            backtestByProposal={latestBacktestByProposal}
            forwardByProposal={latestForwardByProposal}
            promotingProposal={promotingProposal}
            promotionError={promotionError}
            onPromote={handlePromoteProposal}
          />
      </div>
      )}
    </WorkspacePage>
  );
}
