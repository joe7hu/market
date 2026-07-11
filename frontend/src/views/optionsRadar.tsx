import { RefreshCw } from "lucide-react";
import {useCallback, useEffect, useMemo, useRef, useState } from "react";
import {loadRefreshJobs, promoteStrategyMutation, startRefreshJob, type RefreshJob } from "@/api";
import {StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import {PanelData, RowRecord } from "@/types";
import {displayField, numberField, textField } from "./rowFormat";
import {formatDate, sessionBadge } from "./optionsRadarFormat";
import {latestBy, latestValidationBy } from "./optionsRadarData";
import {tabButtonClass, rows, rowsForDisplayTime, isOpportunityCandidate, uniqueText, candidateOpportunityFields, countWhere, optionThesisAgentState, stateOf } from "./optionsRadar/helpers";
import {SignalBriefPanel, StrategyExplainer } from "./optionsRadar/signalBrief";
import {CandidateEventsTable } from "./optionsRadar/candidateTable";
import {MissedWinnersTable, LearningProgressPanel, CohortResultsTable, ExplorationGatePanel, PostmortemRequestsTable, PostmortemsTable } from "./optionsRadar/learningPanels";
import {StrategyProposalsTable } from "./optionsRadar/strategyProposals";
import {WorkspacePage, OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};

type HardRefreshStatus = "checking" | "idle" | "starting" | "running" | "succeeded" | "failed";

const HARD_REFRESH_JOB = "options_radar_hard_refresh";
const HARD_REFRESH_POLL_MS = 5000;

export function OptionsRadarPage({ data, onOpenTicker, onRefresh }: OptionsRadarPageProps) {
  const [activeTab, setActiveTab] = useState<"signals" | "learning">("signals");
  const [promotingProposal, setPromotingProposal] = useState<string | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const hardRefreshJobId = useRef<string | null>(null);
  const [hardRefreshStatus, setHardRefreshStatus] = useState<HardRefreshStatus>("checking");
  const [hardRefreshError, setHardRefreshError] = useState<string | null>(null);
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
  const explorationGate = rows(data.explorationGateReport);
  const opportunityRows = rows(data.optionRadarOpportunity);
  const strategyVersions = rows(data.optionStrategyVersions);
  const radarSummary = rows(data.optionRadarSummary)[0];
  const latestCandidateTime = textField(radarSummary, ["latest_candidate_time"]);
  const marketSession = textField(radarSummary, ["market_session"]);
  const frozenToRth = textField(radarSummary, ["frozen_to_last_rth"]) === "Yes";
  const optionThesisAgent = optionThesisAgentState(data);

  const opportunityCandidates = useMemo(
    () => rowsForDisplayTime(candidates.filter(isOpportunityCandidate), latestCandidateTime),
    [candidates, latestCandidateTime],
  );
  const currentOpportunityRows = useMemo(
    () => rowsForDisplayTime(opportunityRows, latestCandidateTime),
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

  const opportunityTickerCount = numberField(radarSummary, ["opportunity_tickers_current"], opportunityTickers.length);
  const scannedTickerCount = numberField(radarSummary, ["scanned_tickers_current"], 0);
  const fireCount = numberField(radarSummary, ["fire_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "FIRE"));
  const setupCount = numberField(radarSummary, ["setup_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "SETUP"));

  const latestSnapshot = textField(radarSummary, ["latest_snapshot_time"]);
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

  const applyHardRefreshJob = useCallback(async (job: RefreshJob, options: { refreshOnSuccess?: boolean } = {}) => {
    if (job.id) hardRefreshJobId.current = job.id;
    if (job.status === "running") {
      setHardRefreshStatus("running");
      setHardRefreshError(null);
      return;
    }
    if (job.status === "failed") {
      hardRefreshJobId.current = null;
      setHardRefreshStatus("failed");
      setHardRefreshError(refreshFailureMessage(job));
      return;
    }
    if (job.status === "succeeded") {
      const failure = refreshFailureMessage(job);
      hardRefreshJobId.current = null;
      if (failure) {
        setHardRefreshStatus("failed");
        setHardRefreshError(failure);
        return;
      }
      setHardRefreshStatus("succeeded");
      setHardRefreshError(null);
      if (options.refreshOnSuccess ?? true) await onRefresh();
    }
  }, [onRefresh]);

  const updateHardRefreshStatus = useCallback(async () => {
    const payload = await loadRefreshJobs();
    const jobRows = payload.rows ?? [];
    const job = (hardRefreshJobId.current
      ? jobRows.find((row) => row.id === hardRefreshJobId.current)
      : null) ?? latestJobByName(jobRows, HARD_REFRESH_JOB);
    if (!job) return;
    await applyHardRefreshJob(job);
  }, [applyHardRefreshJob]);

  const startHardRefresh = useCallback(async () => {
    if (hardRefreshStatus === "checking" || hardRefreshStatus === "starting" || hardRefreshStatus === "running") return;
    setHardRefreshStatus("starting");
    setHardRefreshError(null);
    try {
      const job = await startRefreshJob(HARD_REFRESH_JOB);
      await applyHardRefreshJob(job);
    } catch (error) {
      hardRefreshJobId.current = null;
      setHardRefreshStatus("failed");
      setHardRefreshError(error instanceof Error ? error.message : "Hard refresh failed");
    }
  }, [applyHardRefreshJob, hardRefreshStatus]);

  useEffect(() => {
    let cancelled = false;
    loadRefreshJobs()
      .then((payload) => {
        if (cancelled) return;
        const runningJob = latestRunningJobByName(payload.rows ?? [], HARD_REFRESH_JOB);
        if (!runningJob) {
          setHardRefreshStatus("idle");
          return;
        }
        if (runningJob.id) hardRefreshJobId.current = runningJob.id;
        setHardRefreshStatus("running");
        setHardRefreshError(null);
      })
      .catch(() => {
        if (!cancelled) setHardRefreshStatus("idle");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (hardRefreshStatus !== "running") return;
    const id = window.setInterval(() => {
      void updateHardRefreshStatus().catch((error) => {
        setHardRefreshStatus("failed");
        setHardRefreshError(error instanceof Error ? error.message : "Hard refresh status check failed");
      });
    }, HARD_REFRESH_POLL_MS);
    return () => window.clearInterval(id);
  }, [hardRefreshStatus, updateHardRefreshStatus]);

  const hardRefreshBusy = hardRefreshStatus === "checking" || hardRefreshStatus === "starting" || hardRefreshStatus === "running";
  const hardRefreshSpinning = hardRefreshStatus === "starting" || hardRefreshStatus === "running";

  return (
    <WorkspacePage
      eyebrow="Options Radar"
      title="10x Options Radar"
      subtitle="Layered signal brief for extreme options setups: strength, blockers, learning impact, and thesis validation."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={hardRefreshBusy}
            onClick={() => void startHardRefresh()}
            title={hardRefreshStatus === "checking" ? "Checking current refresh status" : "Pull fresh Robinhood option chains and rebuild the options radar"}
          >
            <RefreshCw className={hardRefreshSpinning ? "animate-spin" : undefined} />
            Hard refresh
          </Button>
          {hardRefreshStatus === "checking" ? <StatusBadge tone="info">Checking</StatusBadge> : null}
          {hardRefreshStatus === "starting" ? <StatusBadge tone="info">Starting</StatusBadge> : null}
          {hardRefreshStatus === "running" ? <StatusBadge tone="info">Refreshing</StatusBadge> : null}
          {hardRefreshStatus === "succeeded" ? <StatusBadge tone="good">Updated</StatusBadge> : null}
          {hardRefreshStatus === "failed" ? <StatusBadge tone="bad">{hardRefreshError ?? "Refresh failed"}</StatusBadge> : null}
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
        <StrategyExplainer strategy={latestStrategy} />
        <ExplorationGatePanel rows={explorationGate} />
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

function latestJobByName(jobs: RefreshJob[], jobName: string): RefreshJob | null {
  return jobs
    .filter((job) => job.job_name === jobName)
    .sort((a, b) => (dateValue(b.started_at) ?? 0) - (dateValue(a.started_at) ?? 0))[0] ?? null;
}

function latestRunningJobByName(jobs: RefreshJob[], jobName: string): RefreshJob | null {
  return jobs
    .filter((job) => job.job_name === jobName && job.status === "running")
    .sort((a, b) => (dateValue(b.started_at) ?? 0) - (dateValue(a.started_at) ?? 0))[0] ?? null;
}

function refreshFailureMessage(job: RefreshJob): string {
  if (job.error) return job.error;
  const summary = isRecord(job.summary) ? job.summary : null;
  const error = typeof summary?.error === "string" ? summary.error : null;
  const failedStep = typeof summary?.failedStep === "string" ? summary.failedStep : null;
  return error || (failedStep ? `Refresh failed at ${failedStep}` : "Hard refresh failed");
}

function dateValue(value: string | null | undefined): number | null {
  if (!value) return null;
  const date = new Date(value).getTime();
  return Number.isNaN(date) ? null : date;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
