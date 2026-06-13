import { Activity, AlertTriangle, ArrowDownUp, BrainCircuit, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, GitBranchPlus, Loader2, Search, Target, TrendingUp } from "lucide-react";
import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";

import { acknowledgeRadarAlert, promoteStrategyMutation } from "@/api";
import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { JsonValue, PanelData, RowRecord, TablePayload } from "@/types";
import type { Tone } from "@/ui/tone";
import { displayField, formatMoney, fullField, listField, numberField, textField, titleLabel, toneFromText } from "./rowFormat";
import {
  dateMillis,
  formatDate,
  formatMultiple,
  formatNumber,
  formatRatio,
  formatScore,
  formatShortDate,
  formatSignedRatio,
  moneyField,
  sessionBadge,
  validationMillis,
} from "./optionsRadarFormat";
import {
  arrayText,
  boolFromRecord,
  jsonArrayField,
  jsonRecord,
  latestBy,
  latestValidationBy,
  listFromRecord,
  numberFromRecord,
  recordField,
  stringFromRecord,
  validationHistoryBy,
} from "./optionsRadarData";
import {
  attributionTone,
  reasonLabel,
  stateRank,
  stateTone,
  thesisStateTone,
  thesisValidationLabel,
  tierTone,
  toneText,
  validationStatusLabel,
  validationStatusTone,
  verdictTone,
} from "./optionsRadarTone";
import { Cell, FullText, Head, MetricPill, SectionTitle, TickerButton, Truncated } from "./optionsRadarPrimitives";
import {
  OPPORTUNITY_STATES,
  backtestDetail,
  candidateActionText,
  candidateConviction,
  candidateFamily,
  candidateOpportunityFields,
  cohortDefinition,
  cohortHasMatureEvidence,
  cohortKey,
  cohortObservationStats,
  commonBlockers,
  commonDataContractFailures,
  compactStrategyVersion,
  compareCandidates,
  compareGroupedOpportunities,
  compareNumber,
  compareScore,
  compareText,
  countWhere,
  dataContractFailures,
  dataContractStatus,
  focusCandidateRows,
  formatConfigValue,
  formatObservedWindow,
  forwardDetail,
  impactSummary,
  investmentStateLabel,
  investmentStateTone,
  isOpportunityCandidate,
  isServiceRepair,
  oldestDate,
  optionThesisAgentState,
  opportunityActionText,
  outcomeMaturity,
  postmortemImpact,
  proposalChangeItems,
  proposalChangeNote,
  proposalGateSummary,
  qualityOf,
  readableReasonSummary,
  rows,
  stateOf,
  summarizeReasons,
  tabButtonClass,
  thesisFallbackText,
  thesisId,
  thesisState,
  tierOf,
  uniqueText,
  uniqueValues,
  validationForThesis,
  validationHistoryForThesis,
  valueIsPresent,
} from "./optionsRadar/helpers";
import {
  CANDIDATE_PAGE_SIZE,
  type CandidateFocus,
  type CandidateSort,
  type CandidateStateFilter,
  type FamilyFilter,
  type OptionThesisAgentRuntime,
  type QualityFilter,
  type ThesisFilter,
} from "./optionsRadar/types";
import { RadarAlertPanel, RadarSummaryStrip } from "./optionsRadar/summary";
import { SignalBriefPanel, StrategyExplainer } from "./optionsRadar/signalBrief";
import { CandidateEventsTable } from "./optionsRadar/candidateTable";
import { CohortResultsTable, LearningProgressPanel, MissedWinnersTable, PostmortemRequestsTable, PostmortemsTable } from "./optionsRadar/learningPanels";
import { StrategyProposalsTable } from "./optionsRadar/strategyProposals";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};


export function OptionsRadarPage({ data, onOpenTicker, onRefresh }: OptionsRadarPageProps) {
  const [activeTab, setActiveTab] = useState<"signals" | "learning">("signals");
  const [promotingProposal, setPromotingProposal] = useState<string | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const [acknowledgingAlert, setAcknowledgingAlert] = useState<string | null>(null);
  const candidates = rows(data.candidateEvent);
  const radarAlerts = rows(data.radarAlert);
  const missedWinners = rows(data.missedWinnerEvent);
  const proposals = rows(data.strategyMutationProposal);
  const backtests = rows(data.strategyBacktestResult);
  const forwardTests = rows(data.strategyForwardTestResult);
  const thesisRequests = rows(data.agentThesisRequest);
  const thesisValidations = rows(data.agentThesisValidation);
  const postmortemRequests = rows(data.agentPostmortemRequest);
  const postmortems = rows(data.agentPostmortem);
  const agentTheses = rows(data.agentThesis);
  const candidateMarks = rows(data.candidateEventMark);
  const candidateAttributions = rows(data.candidateEventAttribution);
  const cohortResults = rows(data.strategyCohortResult);
  const opportunityRows = rows(data.optionRadarOpportunity);
  const strategyVersions = rows(data.optionStrategyVersions);
  const radarSummary = rows(data.optionRadarSummary)[0];
  const latestCandidateTime = textField(radarSummary, ["latest_candidate_time"]);
  const marketSession = textField(radarSummary, ["market_session"]);
  const frozenToRth = textField(radarSummary, ["frozen_to_last_rth"]) === "Yes";
  const optionThesisAgent = optionThesisAgentState(data);

  const opportunityCandidates = useMemo(
    () => candidates.filter((row) => isOpportunityCandidate(row) && (!latestCandidateTime || textField(row, ["snapshot_time"]) === latestCandidateTime)),
    [candidates, latestCandidateTime],
  );
  const currentOpportunityRows = useMemo(
    () => opportunityRows.filter((row) => !latestCandidateTime || textField(row, ["snapshot_time"]) === latestCandidateTime),
    [latestCandidateTime, opportunityRows],
  );
  const opportunityByEvent = useMemo(
    () => new Map(currentOpportunityRows.map((row) => [textField(row, ["primary_event_id"]), row]).filter(([eventId]) => Boolean(eventId)) as Array<[string, RowRecord]>),
    [currentOpportunityRows],
  );
  const enrichedOpportunityCandidates = useMemo(
    () => opportunityCandidates.map((row) => ({ ...row, ...candidateOpportunityFields(opportunityByEvent.get(textField(row, ["event_id"]))) })),
    [opportunityByEvent, opportunityCandidates],
  );
  const opportunityTickers = useMemo(() => uniqueText(opportunityCandidates, "ticker"), [opportunityCandidates]);

  const latestBacktestByProposal = useMemo(() => latestBy(backtests, "proposal_id", "evaluated_at"), [backtests]);
  const latestForwardByProposal = useMemo(() => latestBy(forwardTests, "proposal_id", "evaluated_at"), [forwardTests]);
  const latestCandidateMarkByEvent = useMemo(() => latestBy(candidateMarks, "event_id", "mark_time"), [candidateMarks]);
  const latestCandidateAttributionByEvent = useMemo(() => latestBy(candidateAttributions, "event_id", "snapshot_time"), [candidateAttributions]);
  const latestThesisRequestByEvent = useMemo(() => latestBy(thesisRequests, "event_id", "created_at"), [thesisRequests]);
  const latestThesisValidationByEvent = useMemo(() => latestValidationBy(thesisValidations, "candidate_event_id"), [thesisValidations]);
  const latestAgentThesisByTicker = useMemo(() => latestBy(agentTheses, "ticker", "created_at"), [agentTheses]);

  const opportunityCount = numberField(radarSummary, ["opportunity_rows_current"], opportunityCandidates.length);
  const opportunityTickerCount = numberField(radarSummary, ["opportunity_tickers_current"], opportunityTickers.length);
  const scannedTickerCount = numberField(radarSummary, ["scanned_tickers_current"], 0);
  const fireCount = numberField(radarSummary, ["fire_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "FIRE"));
  const setupCount = numberField(radarSummary, ["setup_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "SETUP"));
  const exceptionalCount = numberField(radarSummary, ["exceptional_opportunities_current"], countWhere(currentOpportunityRows, (row) => tierOf(row) === "Exceptional"));
  const researchCount = numberField(radarSummary, ["research_opportunities_current"], countWhere(currentOpportunityRows, (row) => tierOf(row) === "Research"));
  const repairCount = numberField(radarSummary, ["repair_opportunities_current"], countWhere(currentOpportunityRows, (row) => isServiceRepair(row)));
  const groupedOpportunityCount = exceptionalCount + researchCount + repairCount || currentOpportunityRows.length;

  const latestSnapshot = textField(radarSummary, ["latest_snapshot_time"]);
  const latestStrategy = strategyVersions[0];

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

  async function handleAcknowledgeAlert(alertId: string) {
    if (!alertId || acknowledgingAlert) return;
    setAcknowledgingAlert(alertId);
    try {
      await acknowledgeRadarAlert(alertId);
      await onRefresh();
    } finally {
      setAcknowledgingAlert(null);
    }
  }

  return (
    <WorkspacePage
      eyebrow="Options Radar"
      title="10x Options Radar"
      subtitle="Layered signal brief for extreme options setups: strength, blockers, learning impact, and thesis validation."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          {(() => {
            const badge = sessionBadge(marketSession, frozenToRth, latestSnapshot);
            return <StatusBadge tone={badge.tone}>{badge.label}</StatusBadge>;
          })()}
          <StatusBadge tone="muted">{latestSnapshot ? `Snapshot ${formatDate(latestSnapshot)}` : "No snapshots"}</StatusBadge>
          <StatusBadge tone="info">{displayField(latestStrategy, ["strategy_version", "strategy_name"], "No strategy")}</StatusBadge>
        </div>
      }
    >
      <RadarAlertPanel alerts={radarAlerts} acknowledgingAlert={acknowledgingAlert} onAcknowledge={handleAcknowledgeAlert} />
      <RadarSummaryStrip
        opportunityCount={opportunityCount}
        opportunityTickerCount={opportunityTickerCount}
        scannedTickerCount={scannedTickerCount}
        fireCount={fireCount}
        setupCount={setupCount}
        exceptionalCount={exceptionalCount}
        researchCount={researchCount}
        repairCount={repairCount}
        groupedOpportunityCount={groupedOpportunityCount}
        />
      <SignalBriefPanel
        rows={currentOpportunityRows}
        fireCount={fireCount}
        setupCount={setupCount}
        latestSnapshot={latestSnapshot}
        latestCandidateTime={latestCandidateTime}
        optionThesisAgent={optionThesisAgent}
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
        <StrategyExplainer strategy={latestStrategy} />
        <CohortResultsTable rows={cohortResults} />
        {missedWinners.length ? <MissedWinnersTable rows={missedWinners} onOpenTicker={onOpenTicker} /> : null}
        {postmortems.length ? <PostmortemsTable rows={postmortems} onOpenTicker={onOpenTicker} /> : null}
        {postmortemRequests.length ? <PostmortemRequestsTable rows={postmortemRequests} onOpenTicker={onOpenTicker} /> : null}
        {proposals.length ? (
          <StrategyProposalsTable
            rows={proposals}
            backtestByProposal={latestBacktestByProposal}
            forwardByProposal={latestForwardByProposal}
            promotingProposal={promotingProposal}
            promotionError={promotionError}
            onPromote={handlePromoteProposal}
          />
        ) : null}
      </div>
      )}
    </WorkspacePage>
  );
}

