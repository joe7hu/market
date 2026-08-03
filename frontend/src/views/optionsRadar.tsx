import {useEffect, useMemo, useState } from "react";
import {loadOptionsRadarLearning, promoteStrategyMutation } from "@/api";
import {StatusBadge } from "@/components/market/workstation";
import {PanelData, RowRecord } from "@/types";
import {displayField, numberField, textField } from "./rowFormat";
import {formatDate, sessionBadge } from "./optionsRadarFormat";
import {latestBy, latestValidationBy } from "./optionsRadarData";
import {tabButtonClass, rows, rowsForDisplayTime, uniqueText, countWhere, optionThesisAgentState, stateOf } from "./optionsRadar/helpers";
import {SignalBriefPanel } from "./optionsRadar/signalBrief";
import {CandidateEventsTable } from "./optionsRadar/candidateTable";
import {DiscoveryQueue} from "./optionsRadar/discoveryQueue";
import {MissedWinnersTable, LearningProgressPanel, CohortResultsTable } from "./optionsRadar/learningPanels";
import {StrategyProposalsTable } from "./optionsRadar/strategyProposals";
import {RecoveryProgramPanel } from "./optionsRadar/recoveryProgram";
import {WorkspacePage, OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};
const SIGNAL_DETAIL_COLLECTIONS = [
  "candidate_event_mark", "candidate_event_attribution", "agent_thesis",
  "agent_thesis_request", "agent_thesis_validation",
];
const LEARNING_COLLECTIONS = [
  ...SIGNAL_DETAIL_COLLECTIONS, "missed_winner_event", "strategy_mutation_proposal",
  "strategy_backtest_result", "strategy_forward_test_result", "strategy_cohort_result",
  "agent_postmortem_request", "agent_postmortem",
];

async function loadLearningCollectionPage(
  collection: string,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<[string, RowRecord[], string | null, number]> {
  const payload = await loadOptionsRadarLearning(collection, cursor, 100, signal);
  return [collection, payload.items, payload.next_cursor, payload.count];
}

export function OptionsRadarPage({ data, onOpenTicker, onRefresh }: OptionsRadarPageProps) {
  const [activeTab, setActiveTab] = useState<"signals" | "learning">("signals");
  const [promotingProposal, setPromotingProposal] = useState<string | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const [learningRows, setLearningRows] = useState<Record<string, RowRecord[]>>({});
  const [learningCursors, setLearningCursors] = useState<Record<string, string | null>>({});
  const [learningCounts, setLearningCounts] = useState<Record<string, number>>({});
  const [learningLoading, setLearningLoading] = useState(false);
  const [learningReload, setLearningReload] = useState(0);
  const radarAlerts = rows(data.radarAlert);
  const missedWinners = learningRows.missed_winner_event ?? [];
  const proposals = learningRows.strategy_mutation_proposal ?? [];
  const backtests = learningRows.strategy_backtest_result ?? [];
  const forwardTests = learningRows.strategy_forward_test_result ?? [];
  const thesisRequests = learningRows.agent_thesis_request ?? [];
  const thesisValidations = learningRows.agent_thesis_validation ?? [];
  const postmortemRequests = (learningRows.agent_postmortem_request ?? []).filter((row) => textField(row, ["status"]).toLowerCase() !== "imported");
  const postmortems = (learningRows.agent_postmortem ?? []).filter((row) => textField(row, ["status"]).toLowerCase() !== "imported");
  const agentTheses = learningRows.agent_thesis ?? [];
  const candidateMarks = learningRows.candidate_event_mark ?? [];
  const candidateAttributions = learningRows.candidate_event_attribution ?? [];
  const cohortResults = learningRows.strategy_cohort_result ?? [];
  const opportunityRows = rows(data.optionRadarOpportunity);
  const discoveryRows = rows(data.optionDiscoveryCandidate);
  const strategyVersions = rows(data.optionStrategyVersions);
  const recoveryFunnel = rows(data.optionRecoveryFunnel)[0];
  const recoveryEvents = rows(data.optionRecoveryEvent);
  const recoveryOpportunities = rows(data.optionRecoveryOpportunity);
  const recoveryFamilyPerformance = rows(data.optionRecoveryFamilyPerformance);
  const recoveryAgentProvenance = rows(data.optionRecoveryAgentProvenance);
  const radarSummary = rows(data.optionRadarSummary)[0];
  const professionalContract = numberField(radarSummary, ["contract_version"], 0) >= 3;
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
  const symbolsConsidered = numberField(radarSummary, ["symbols_considered"], 0);
  const symbolsWithChains = numberField(radarSummary, ["symbols_with_chains"], 0);
  const contractsEvaluated = numberField(radarSummary, ["contracts_evaluated", "scanned_contracts"], 0);
  const fireCount = numberField(radarSummary, ["ready_count", "fire_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "READY"));
  const setupCount = numberField(radarSummary, ["setup_count", "setup_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "SETUP"));

  const latestSnapshot = textField(radarSummary, ["latest_complete_quote_time", "latest_snapshot_time"]);
  const snapshotLabel = textField(radarSummary, ["latest_snapshot_label"]);
  const displayStrategyVersion = textField(radarSummary, ["strategy_version"]);
  const latestStrategy = strategyVersions.find((row) => textField(row, ["strategy_version"]) === displayStrategyVersion) ?? strategyVersions[0];
  const strategyLabel = professionalContract
    ? `Professional v3 · revision ${numberField(radarSummary, ["strategy_revision"], 0).toLocaleString()}`
    : displayField(latestStrategy, ["strategy_version", "strategy_name"], "No strategy");

  useEffect(() => {
    const controller = new AbortController();
    const collections = activeTab === "learning" ? LEARNING_COLLECTIONS : SIGNAL_DETAIL_COLLECTIONS;
    if (!collections.length) return;
    if (activeTab === "learning") setLearningLoading(true);
    Promise.all(collections.map((collection) => loadLearningCollectionPage(collection, null, controller.signal)))
      .then((payloads) => {
        const loaded = Object.fromEntries(payloads.map(([collection, items]) => [collection, items]));
        const cursors = Object.fromEntries(payloads.map(([collection, , cursor]) => [collection, cursor]));
        const counts = Object.fromEntries(payloads.map(([collection, , , count]) => [collection, count]));
        setLearningRows((current) => ({ ...current, ...loaded }));
        setLearningCursors((current) => ({ ...current, ...cursors }));
        setLearningCounts((current) => ({ ...current, ...counts }));
      })
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) setPromotionError("Learning data could not be loaded.");
      })
      .finally(() => setLearningLoading(false));
    return () => controller.abort();
  }, [activeTab, data, latestCandidateTime, learningReload]);

  async function handleLoadMoreLearning() {
    const pending = Object.entries(learningCursors).filter(([, cursor]) => cursor !== null);
    if (!pending.length || learningLoading) return;
    setLearningLoading(true);
    try {
      const payloads = await Promise.all(
        pending.map(([collection, cursor]) => loadLearningCollectionPage(collection, cursor, undefined)),
      );
      setLearningRows((current) => {
        const next = { ...current };
        for (const [collection, items] of payloads) next[collection] = [...(next[collection] ?? []), ...items];
        return next;
      });
      setLearningCursors((current) => ({
        ...current,
        ...Object.fromEntries(payloads.map(([collection, , cursor]) => [collection, cursor])),
      }));
      setLearningCounts((current) => ({
        ...current,
        ...Object.fromEntries(payloads.map(([collection, , , count]) => [collection, count])),
      }));
    } catch {
      setLearningRows({});
      setLearningCursors({});
      setLearningCounts({});
      setLearningReload((value) => value + 1);
      setPromotionError("The review snapshot expired or changed; learning history was reloaded.");
    } finally {
      setLearningLoading(false);
    }
  }

  async function handlePromoteProposal(proposalId: string) {
    if (!proposalId || promotingProposal) return;
    setPromotingProposal(proposalId);
    setPromotionError(null);
    try {
      await promoteStrategyMutation(proposalId, "joe");
      await onRefresh();
      setLearningRows({});
      setLearningCursors({});
      setLearningCounts({});
      setLearningReload((value) => value + 1);
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
          <StatusBadge tone="info">{strategyLabel}</StatusBadge>
        </div>
      }
    >
      <SignalBriefPanel
        rows={currentOpportunityRows}
        activeAlertCount={radarAlerts.length}
        fireCount={fireCount}
        setupCount={setupCount}
        symbolsConsidered={symbolsConsidered}
        symbolsWithChains={symbolsWithChains}
        contractsEvaluated={contractsEvaluated}
        opportunityTickerCount={opportunityTickerCount}
        latestSnapshot={latestSnapshot}
        snapshotLabel={snapshotLabel}
        latestCandidateTime={latestCandidateTime}
        onOpenTicker={onOpenTicker}
      />
      <RecoveryProgramPanel
        funnel={recoveryFunnel}
        events={recoveryEvents}
        opportunities={recoveryOpportunities}
        familyPerformance={recoveryFamilyPerformance}
        agentProvenance={recoveryAgentProvenance}
        onOpenTicker={onOpenTicker}
      />
      <DiscoveryQueue rows={discoveryRows} onOpenTicker={onOpenTicker} />
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
        {learningLoading ? <p className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">Loading bounded learning cohorts…</p> : null}
        <LearningProgressPanel
          opportunities={enrichedOpportunityCandidates}
          latestMarkByEvent={latestCandidateMarkByEvent}
          latestAttributionByEvent={latestCandidateAttributionByEvent}
          cohorts={cohortResults}
          proposals={proposals}
          missedWinners={missedWinners}
          postmortemRequests={postmortemRequests}
          postmortems={postmortems}
          totals={learningCounts}
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
        {Object.values(learningCursors).some((cursor) => cursor !== null) ? (
          <button
            type="button"
            className="rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
            disabled={learningLoading}
            onClick={handleLoadMoreLearning}
          >
            {learningLoading ? "Loading…" : "Load more review history"}
          </button>
        ) : null}
      </div>
      )}
    </WorkspacePage>
  );
}
