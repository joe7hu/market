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
import { WorkspacePage, type OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};

type OptionThesisAgentRuntime = {
  active: boolean;
  enabled: boolean;
  configured: boolean;
  status: string;
  limit: number;
  requestCap: number;
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

function RadarSummaryStrip({
  opportunityCount,
  opportunityTickerCount,
  scannedTickerCount,
  fireCount,
  setupCount,
  exceptionalCount,
  researchCount,
  repairCount,
  groupedOpportunityCount,
}: {
  opportunityCount: number;
  opportunityTickerCount: number;
  scannedTickerCount: number;
  fireCount: number;
  setupCount: number;
  exceptionalCount: number;
  researchCount: number;
  repairCount: number;
  groupedOpportunityCount: number;
}) {
  const items: Array<[string, string, Tone]> = [
    ["Trade-Ready", exceptionalCount.toLocaleString(), exceptionalCount ? "good" : "muted"],
    ["Research", researchCount.toLocaleString(), researchCount ? "info" : "muted"],
    ["Data Blocked", repairCount.toLocaleString(), repairCount ? "bad" : "good"],
    ["Coverage", `${scannedTickerCount.toLocaleString()} scanned / ${opportunityTickerCount.toLocaleString()} tickers`, scannedTickerCount >= 20 ? "good" : scannedTickerCount ? "warn" : "muted"],
  ];
  return (
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      {items.map(([label, value, tone]) => (
        <div key={label} className="rounded-md border border-border bg-card px-3 py-2">
          <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
          <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
        </div>
      ))}
    </div>
  );
}

function RadarAlertPanel({
  alerts,
  acknowledgingAlert,
  onAcknowledge,
}: {
  alerts: RowRecord[];
  acknowledgingAlert: string | null;
  onAcknowledge: (alertId: string) => void;
}) {
  if (!alerts.length) return null;
  return (
    <section className="rounded-md border border-border bg-card p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <AlertTriangle className="size-4 text-amber-600" />
          <h2 className="text-sm font-semibold">Active Radar Alerts</h2>
          <StatusBadge tone="warn">{alerts.length.toLocaleString()}</StatusBadge>
        </div>
      </div>
      <div className="mt-3 grid gap-2 lg:grid-cols-2">
        {alerts.slice(0, 6).map((alert) => {
          const alertId = textField(alert, ["alert_id"]);
          const severity = textField(alert, ["severity"], "info").toLowerCase();
          const tone: Tone = severity === "critical" ? "bad" : severity === "warning" ? "warn" : "info";
          return (
            <div key={alertId || `${textField(alert, ["ticker"])}-${textField(alert, ["alert_type"])}`} className="flex min-w-0 items-start justify-between gap-3 rounded-md border border-border/70 bg-background px-3 py-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <StatusBadge tone={tone}>{titleLabel(textField(alert, ["alert_type"], "alert"))}</StatusBadge>
                  <span className="text-sm font-semibold">{displayField(alert, ["title"])}</span>
                </div>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{displayField(alert, ["detail"])}</p>
              </div>
              <Button type="button" variant="outline" size="sm" className="h-8 shrink-0" disabled={!alertId || acknowledgingAlert === alertId} onClick={() => onAcknowledge(alertId)}>
                {acknowledgingAlert === alertId ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />}
                <span>Ack</span>
              </Button>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function tabButtonClass(active: boolean): string {
  return cn(
    "rounded-sm px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
    active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
  );
}

function SignalBriefPanel({
  rows,
  fireCount,
  setupCount,
  latestSnapshot,
  latestCandidateTime,
  optionThesisAgent,
  onOpenTicker,
}: {
  rows: RowRecord[];
  fireCount: number;
  setupCount: number;
  latestSnapshot: string;
  latestCandidateTime: string;
  optionThesisAgent: OptionThesisAgentRuntime;
  onOpenTicker: OpenTicker;
}) {
  const ranked = useMemo(() => [...rows].sort(compareGroupedOpportunities), [rows]);
  const strongest = ranked.find((row) => !isServiceRepair(row)) ?? ranked[0];
  const repairRows = rows.filter(isServiceRepair);
  const exceptionalRows = rows.filter((row) => tierOf(row) === "Exceptional");
  const researchRows = rows.filter((row) => tierOf(row) === "Research");
  const topBlockers = commonBlockers(rows).slice(0, 3);
  const dataFailures = commonDataContractFailures(repairRows).slice(0, 3);
  const decisionTone: Tone = exceptionalRows.length ? "good" : repairRows.length ? "bad" : researchRows.length ? "info" : "muted";
  const decisionLabel = exceptionalRows.length
    ? `${exceptionalRows.length} trade-ready opportunit${exceptionalRows.length === 1 ? "y" : "ies"}`
    : repairRows.length
      ? `${repairRows.length} data contract issue${repairRows.length === 1 ? "" : "s"}`
      : researchRows.length
        ? `${researchRows.length} research opportunit${researchRows.length === 1 ? "y" : "ies"}`
        : "No grouped opportunities";
  const fireGap = fireCount > 0 && exceptionalRows.length === 0;
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={decisionTone}>{decisionLabel}</StatusBadge>
            {fireGap ? <StatusBadge tone="warn">{fireCount.toLocaleString()} FIRE contract{fireCount === 1 ? "" : "s"} blocked from trade-ready</StatusBadge> : null}
            <StatusBadge tone="muted">{latestCandidateTime ? `Candidate run ${formatDate(latestCandidateTime)}` : "No candidate run"}</StatusBadge>
            <StatusBadge tone="muted">{latestSnapshot ? `Option data ${formatDate(latestSnapshot)}` : "No option data"}</StatusBadge>
          </div>
          {strongest ? (
            <div className="mt-4">
              <div className="flex flex-wrap items-center gap-2">
                <TickerButton ticker={textField(strongest, ["ticker"])} onOpenTicker={onOpenTicker} />
                <StatusBadge tone={tierTone(tierOf(strongest))}>{tierOf(strongest)}</StatusBadge>
                <StatusBadge tone={stateTone(textField(strongest, ["primary_state"]).toUpperCase())}>{titleLabel(displayField(strongest, ["primary_state"], "watch"))}</StatusBadge>
              </div>
              <p className="mt-2 max-w-5xl text-sm leading-6 text-foreground">{opportunityActionText(strongest)}</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                <MetricBox label="Conviction" value={formatScore(numberField(strongest, ["conviction_score"], Number.NaN))} />
                <MetricBox label="Required Move" value={formatRatio(numberField(strongest, ["required_move_pct"], Number.NaN))} />
                <MetricBox label="Premium" value={moneyField(strongest, ["premium_mid"])} />
                <MetricBox label="Buy Under" value={moneyField(strongest, ["buy_under"])} />
              </div>
            </div>
          ) : (
            <p className="mt-4 text-sm text-muted-foreground">No current opportunity read model is available for this radar run.</p>
          )}
        </div>
        <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-1">
          <BriefCallout
            label="Decision Impact"
            tone={decisionTone}
            value={strongest ? impactSummary(strongest, fireCount, setupCount) : "Wait for the next radar run."}
          />
          <BriefCallout
            label={repairRows.length ? "Data Blocker" : "Main Blocker"}
            tone={repairRows.length ? "bad" : topBlockers.length ? "warn" : "good"}
            value={repairRows.length ? summarizeReasons(dataFailures, "Data contract is clean.") : summarizeReasons(topBlockers, "Strict gates are clean.")}
          />
          <BriefCallout
            label="Agent Cadence"
            tone={optionThesisAgent.active ? "info" : "muted"}
            value={`Runs once premarket; batch limit ${optionThesisAgent.limit}. Hourly refresh stays deterministic.`}
          />
        </div>
      </div>
    </section>
  );
}

function BriefCallout({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="rounded-md border border-border/70 bg-background px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm leading-5", toneText(tone))}>{value}</div>
    </div>
  );
}

function summarizeReasons(reasons: Array<[string, number]>, empty: string): string {
  if (!reasons.length) return empty;
  return reasons.map(([reason, count]) => `${reasonLabel(reason)} (${count})`).join("; ");
}

function impactSummary(row: RowRecord, fireCount = 0, setupCount = 0): string {
  if (isServiceRepair(row)) return "Do not interpret as a trade setup until the data contract is fixed.";
  if (tierOf(row) === "Exceptional") return "Candidate is allowed into trade review because data, asymmetry, entry, and evidence gates are aligned.";
  if (tierOf(row) === "Research") {
    const contractMix = fireCount ? `${fireCount.toLocaleString()} FIRE contract${fireCount === 1 ? "" : "s"}` : `${setupCount.toLocaleString()} setup contract${setupCount === 1 ? "" : "s"}`;
    return `${contractMix} exist, but the grouped ticker is still Research because strict evidence, thesis, regime, or blocker gates are not all clean.`;
  }
  return "Watch only; signal is not yet strong enough for research priority.";
}

function ReadableReasonGroup({ label, reasons, tone }: { label: string; reasons: string[]; tone: Tone }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {reasons.map((reason) => <ReasonChip key={`${label}-${reason}`} reason={reason} tone={tone} />)}
      </div>
    </div>
  );
}

function OpportunityThesisSummary({
  row,
  request,
  validation,
  thesis,
  agentRuntime,
}: {
  row: RowRecord;
  request: RowRecord | undefined;
  validation: RowRecord | undefined;
  thesis: RowRecord | undefined;
  agentRuntime: OptionThesisAgentRuntime;
}) {
  const validationReason = fullField(validation, ["reason"], "");
  const coreThesis = fullField(thesis, ["core_thesis"], "");
  const requestStatus = textField(request, ["status"]);
  const requestCreated = textField(request, ["created_at"]);
  const summary = validationReason || coreThesis || thesisFallbackText(row, request, agentRuntime);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        <StatusBadge tone={thesisStateTone(textField(validation, ["state"]))}>{thesisValidationLabel(validation)}</StatusBadge>
        {requestStatus ? <StatusBadge tone={toneFromText(requestStatus)}>{titleLabel(requestStatus)}</StatusBadge> : null}
        {textField(validation, ["red_team_status"]) ? (
          <StatusBadge tone={validationStatusTone(textField(validation, ["red_team_status"]))}>{validationStatusLabel(textField(validation, ["red_team_status"]))}</StatusBadge>
        ) : null}
      </div>
      <FullText>{summary}</FullText>
      {coreThesis && validationReason && coreThesis !== validationReason ? (
        <InsightLine label="Core thesis" value={coreThesis} />
      ) : null}
      {requestCreated ? <div className="text-xs text-muted-foreground">Request {formatDate(requestCreated)}</div> : null}
    </div>
  );
}

function thesisFallbackText(row: RowRecord, request: RowRecord | undefined, agentRuntime: OptionThesisAgentRuntime): string {
  const requestStatus = textField(request, ["status"]);
  if (requestStatus.toLowerCase() === "open") {
    return agentRuntime.active
      ? `Premarket synthesis is queued for this ${titleLabel(displayField(row, ["primary_state"], "signal"))} contract.`
      : "Premarket synthesis is queued, but the thesis worker is paused.";
  }
  if (requestStatus) return `Latest thesis request is ${titleLabel(requestStatus)}.`;
  if (isServiceRepair(row)) return "Thesis is blocked until the data contract is repaired.";
  return "No linked thesis validation is stored for this grouped opportunity.";
}

function compareGroupedOpportunities(left: RowRecord, right: RowRecord): number {
  const leftState = textField(left, ["primary_state"]).toUpperCase();
  const rightState = textField(right, ["primary_state"]).toUpperCase();
  return (
    stateRank(leftState) - stateRank(rightState) ||
    compareNumber(numberField(right, ["conviction_score"], Number.NEGATIVE_INFINITY), numberField(left, ["conviction_score"], Number.NEGATIVE_INFINITY)) ||
    compareText(textField(left, ["ticker"]), textField(right, ["ticker"]))
  );
}

function InlineMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-muted px-2 py-1.5">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function MobileSection({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-sm leading-5">{children}</div>
    </div>
  );
}

function opportunityActionText(row: RowRecord): string {
  const tier = tierOf(row);
  if (isServiceRepair(row)) {
    return displayField(row, ["service_repair_summary"], "Service bug blocks trade-state computation.");
  }
  const blockers = arrayText(row, "blockers");
  const reasons = arrayText(row, "top_reasons");
  if (tier === "Exceptional" && !blockers.length) {
    const why = reasons.slice(0, 3).map(reasonLabel).join(", ");
    return why ? `Trade-ready candidate: ${why}.` : "Trade-ready candidate: strict gates passed.";
  }
  if (blockers.length) {
    const why = blockers.slice(0, 3).map(reasonLabel).join(", ");
    return `Current state is not trade-ready: ${why}.`;
  }
  return displayField(row, ["why_now"], "Review setup details.");
}

function StrategyExplainer({ strategy }: { strategy: RowRecord | undefined }) {
  const params = recordField(strategy, "parameters");
  const strategyName = displayField(strategy, ["strategy_name"], "LEAP 10x reversal");
  const version = displayField(strategy, ["strategy_version"], "No strategy loaded");
  const status = textField(strategy, ["status"], "shadow");
  const rules = [
    ["Contract", `${titleLabel(stringFromRecord(params, "option_type", "call"))} options`],
    ["Delta", `${formatNumber(numberFromRecord(params, "delta_min"), 2)}-${formatNumber(numberFromRecord(params, "delta_max"), 2)}`],
    ["DTE", `${formatNumber(numberFromRecord(params, "dte_min"), 0)}-${formatNumber(numberFromRecord(params, "dte_max"), 0)} days`],
    ["Spread", `Fire <= ${formatRatio(numberFromRecord(params, "max_spread_pct"))}; reject > ${formatRatio(numberFromRecord(params, "reject_spread_pct"))}`],
    ["Liquidity", `OI >= ${formatNumber(numberFromRecord(params, "min_open_interest"), 0)}; volume >= ${formatNumber(numberFromRecord(params, "min_volume"), 0)}`],
    ["IV", `Fire <= ${formatNumber(numberFromRecord(params, "max_iv_percentile"), 0)} pctile; reject > ${formatNumber(numberFromRecord(params, "reject_iv_percentile"), 0)}`],
    ["Trend", `${boolFromRecord(params, "require_price_above_ma50") ? "Above 50D" : "50D optional"}; ${boolFromRecord(params, "require_rs_improving") ? "RS vs QQQ improving" : "RS optional"}`],
    ["10x cap", `Underlying move <= ${formatRatio(numberFromRecord(params, "max_required_move_pct"))}`],
  ];
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{strategyName}</h2>
            <StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge>
            <StatusBadge tone="info">{version}</StatusBadge>
          </div>
          <p className="mt-2 max-w-5xl text-sm leading-6 text-muted-foreground">
            A shadow-only LEAP call screen looking for contracts where a large underlying move could make intrinsic value roughly 10x the option mid. The visible signal rank uses opportunity conviction when the read model has it; queue state still separates FIRE, SETUP, and WATCH. Agents only receive the current top-ranked queue, and promotion requires deterministic backtest, forward shadow test, and human approval.
          </p>
        </div>
        <div className="shrink-0 text-xs text-muted-foreground">
          {displayField(strategy, ["notes"], "Strategy metadata is stored with the radar snapshot.")}
        </div>
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {rules.map(([label, value]) => (
          <div key={label} className="rounded-md border border-border/70 bg-background px-3 py-2">
            <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
            <div className="mt-1 text-sm font-medium">{value}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

type CandidateSort = "conviction-desc" | "ticker-asc" | "move-asc" | "premium-asc" | "expiry-asc" | "state";
type CandidateStateFilter = "all" | "FIRE" | "SETUP" | "WATCH";
type CandidateFocus = "top25" | "top-per-ticker" | "all";
type ThesisFilter = "all" | "needs" | "requested" | "attached";
type QualityFilter = "all" | "ok" | "caution" | "bad";
type FamilyFilter = "all" | string;

const CANDIDATE_PAGE_SIZE = 25;

function CandidateEventsTable({
  rows,
  thesisRequestByEvent,
  latestMarkByEvent,
  latestAttributionByEvent,
  latestThesisValidationByEvent,
  latestAgentThesisByTicker,
  agentRuntime,
  onOpenTicker,
}: {
  rows: RowRecord[];
  thesisRequestByEvent: Map<string, RowRecord>;
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  latestThesisValidationByEvent: Map<string, RowRecord>;
  latestAgentThesisByTicker: Map<string, RowRecord>;
  agentRuntime: OptionThesisAgentRuntime;
  onOpenTicker: OpenTicker;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<CandidateStateFilter>("all");
  const [thesisFilter, setThesisFilter] = useState<ThesisFilter>("all");
  const [qualityFilter, setQualityFilter] = useState<QualityFilter>("all");
  const [familyFilter, setFamilyFilter] = useState<FamilyFilter>("all");
  const [focus, setFocus] = useState<CandidateFocus>("top-per-ticker");
  const [sort, setSort] = useState<CandidateSort>("state");
  const [page, setPage] = useState(0);
  const [expandedThesisEvent, setExpandedThesisEvent] = useState<string | null>(null);

  const filteredRows = useMemo(() => {
    const normalizedQuery = query.trim().toUpperCase();
    return rows
      .filter((row) => {
        if (stateFilter !== "all" && stateOf(row) !== stateFilter) return false;
        if (qualityFilter !== "all" && qualityOf(row) !== qualityFilter) return false;
        if (familyFilter !== "all" && candidateFamily(row) !== familyFilter) return false;
        if (thesisFilter !== "all" && thesisState(row, thesisRequestByEvent).kind !== thesisFilter) return false;
        if (!normalizedQuery) return true;
        const haystack = [
          textField(row, ["ticker"]),
          textField(row, ["contract_id"]),
          textField(row, ["strategy_version"]),
          readableReasonSummary(row),
        ].join(" ").toUpperCase();
        return haystack.includes(normalizedQuery);
      });
  }, [familyFilter, query, qualityFilter, rows, stateFilter, thesisFilter, thesisRequestByEvent]);

  const focusedRows = useMemo(
    () => focusCandidateRows(filteredRows, focus).sort((left, right) => compareCandidates(left, right, sort)),
    [filteredRows, focus, sort],
  );

  useEffect(() => {
    setPage(0);
    setExpandedThesisEvent(null);
  }, [focus, query, qualityFilter, sort, stateFilter, thesisFilter]);

  const pageCount = Math.max(1, Math.ceil(focusedRows.length / CANDIDATE_PAGE_SIZE));
  const boundedPage = Math.min(page, pageCount - 1);
  const visibleRows = focusedRows.slice(boundedPage * CANDIDATE_PAGE_SIZE, (boundedPage + 1) * CANDIDATE_PAGE_SIZE);
  const tickerCount = uniqueText(focusedRows, "ticker").length;
  const familyOptions = useMemo(() => uniqueValues(rows.map(candidateFamily)).filter(Boolean), [rows]);

  if (!rows.length) {
    return <EmptyState title="No candidate events" detail="No options radar candidates are stored yet." icon={Target} />;
  }

  return (
    <DataTableFrame
      title={<SectionTitle title="Top Ranked Signals" count={focusedRows.length} />}
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{tickerCount.toLocaleString()} tickers</span>
          <span>{filteredRows.length.toLocaleString()} matched</span>
        </div>
      }
    >
      <div className="border-b border-border p-3">
        <div className="grid gap-2 lg:grid-cols-[minmax(220px,1fr)_155px_150px_150px_160px_160px_190px_auto]">
          <div className="relative min-w-0">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker, contract, or thesis blocker" aria-label="Filter signal evidence" />
          </div>
          <Select value={focus} onValueChange={(value) => setFocus(value as CandidateFocus)}>
            <SelectTrigger aria-label="Candidate focus"><SelectValue placeholder="Focus" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="top-per-ticker">Top per ticker</SelectItem>
              <SelectItem value="top25">Top 25 contracts</SelectItem>
              <SelectItem value="all">All ranked contracts</SelectItem>
            </SelectContent>
          </Select>
          <Select value={stateFilter} onValueChange={(value) => setStateFilter(value as CandidateStateFilter)}>
            <SelectTrigger aria-label="State filter"><SelectValue placeholder="State" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All states</SelectItem>
              <SelectItem value="FIRE">Fire</SelectItem>
              <SelectItem value="SETUP">Setup</SelectItem>
              <SelectItem value="WATCH">Watch</SelectItem>
            </SelectContent>
          </Select>
          <Select value={thesisFilter} onValueChange={(value) => setThesisFilter(value as ThesisFilter)}>
            <SelectTrigger aria-label="Thesis filter"><SelectValue placeholder="Thesis" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any thesis</SelectItem>
              <SelectItem value="needs">Needs evidence</SelectItem>
              <SelectItem value="requested">Thesis requested</SelectItem>
              <SelectItem value="attached">Thesis attached</SelectItem>
            </SelectContent>
          </Select>
          <Select value={qualityFilter} onValueChange={(value) => setQualityFilter(value as QualityFilter)}>
            <SelectTrigger aria-label="Quality filter"><SelectValue placeholder="Quality" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any quality</SelectItem>
              <SelectItem value="ok">Provider OK</SelectItem>
              <SelectItem value="caution">Caution</SelectItem>
              <SelectItem value="bad">Bad data</SelectItem>
            </SelectContent>
          </Select>
          <Select value={familyFilter} onValueChange={(value) => setFamilyFilter(value)}>
            <SelectTrigger aria-label="Family filter"><SelectValue placeholder="Family" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All families</SelectItem>
              {familyOptions.map((family) => <SelectItem key={family} value={family}>{family}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(value) => setSort(value as CandidateSort)}>
            <SelectTrigger aria-label="Sort candidates"><SelectValue placeholder="Sort" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="conviction-desc">Conviction high to low</SelectItem>
              <SelectItem value="move-asc">Required move low to high</SelectItem>
              <SelectItem value="premium-asc">Option mid low to high</SelectItem>
              <SelectItem value="expiry-asc">Expiration soonest</SelectItem>
              <SelectItem value="state">Queue rank</SelectItem>
              <SelectItem value="ticker-asc">Ticker A to Z</SelectItem>
            </SelectContent>
          </Select>
          <Button type="button" variant="outline" size="sm" className="h-9" onClick={() => {
            setQuery("");
            setStateFilter("all");
            setThesisFilter("all");
            setQualityFilter("all");
            setFamilyFilter("all");
            setFocus("top-per-ticker");
            setSort("state");
          }}>
            <ArrowDownUp className="size-4" />
            <span>Reset</span>
          </Button>
        </div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          One primary surface for current ranked contracts. Each row combines the contract signal, evidence, thesis validation, observed outcome, and next action.
        </p>
      </div>
      <div className="space-y-3 p-3 lg:hidden">
        {visibleRows.map((row) => {
          const ticker = textField(row, ["ticker"]);
          const eventId = textField(row, ["event_id"]);
          const thesisExpanded = expandedThesisEvent === eventId;
          return (
            <CandidateMobileCard
              key={textField(row, ["event_id"], `${ticker}-${textField(row, ["contract_id"])}`)}
              row={row}
              request={thesisRequestByEvent.get(eventId)}
              validation={latestThesisValidationByEvent.get(eventId)}
              thesis={latestAgentThesisByTicker.get(ticker)}
              mark={latestMarkByEvent.get(eventId)}
              attribution={latestAttributionByEvent.get(eventId)}
              agentRuntime={agentRuntime}
              expanded={thesisExpanded}
              onToggleThesis={() => setExpandedThesisEvent(thesisExpanded ? null : eventId)}
              onOpenTicker={onOpenTicker}
            />
          );
        })}
      </div>
      <table className="hidden w-full min-w-[1780px] table-fixed text-sm lg:table">
        <colgroup>
          <col className="w-[8rem]" />
          <col className="w-[9rem]" />
          <col className="w-[16rem]" />
          <col className="w-[10rem]" />
          <col className="w-[11rem]" />
          <col className="w-[24rem]" />
          <col className="w-[28rem]" />
          <col className="w-[15rem]" />
          <col className="w-[22rem]" />
        </colgroup>
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Signal State</Head>
            <Head>Contract</Head>
            <Head className="text-right"><HelpLabel label="Option Mid" detail="Current option midpoint from the stored chain snapshot. Est fill adds the strategy slippage assumption used by shadow entries." /></Head>
            <Head className="text-right"><HelpLabel label="10x Math" detail="Underlying stock price where intrinsic value is about ten times the option mid. Fill target uses the estimated fill premium when it differs. Cap room is how far the estimated fill is below the strategy premium ceiling." /></Head>
            <Head>Signal Evidence</Head>
            <Head>Thesis</Head>
            <Head>Impact So Far</Head>
            <Head>Next Action</Head>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => {
            const ticker = textField(row, ["ticker"]);
            const state = stateOf(row);
            const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
            const qualityFlags = listField(row, ["quality_flags"]);
            const eventId = textField(row, ["event_id"]);
            const mark = latestMarkByEvent.get(eventId);
            const attribution = latestAttributionByEvent.get(eventId);
            const request = thesisRequestByEvent.get(eventId);
            const validation = latestThesisValidationByEvent.get(eventId);
            const thesis = latestAgentThesisByTicker.get(ticker);
            const thesisExpanded = expandedThesisEvent === eventId;
            const rowKey = textField(row, ["event_id"], `${ticker}-${textField(row, ["contract_id"])}`);
            return (
              <Fragment key={rowKey}>
                <tr
                  className={cn(
                    "border-b border-border align-top transition-colors hover:bg-accent/40",
                    qualityStatus === "bad" && "bg-destructive/5",
                    qualityStatus === "caution" && "bg-amber-500/5",
                  )}
                >
                  <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                  <Cell>
                    <div className="space-y-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge>
                        <QualityIndicator status={qualityStatus} flags={qualityFlags} />
                      </div>
                      <div className="text-xs text-muted-foreground">conviction {formatScore(candidateConviction(row))}</div>
                    </div>
                  </Cell>
                  <Cell>
                    <FullText>{displayField(row, ["contract_id"])}</FullText>
                    <div className="mt-1 text-xs text-muted-foreground">{candidateFamily(row)} | {titleLabel(stringFromRecord(recordField(row, "raw"), "option_type", "call"))} {formatShortDate(stringFromRecord(recordField(row, "raw"), "expiration"))}</div>
                  </Cell>
                  <Cell className="text-right tabular-nums">
                    <div>{moneyField(row, ["premium_mid"])}</div>
                    <div className="text-xs text-muted-foreground">fill {moneyField(row, ["premium_fill_assumption"])}</div>
                  </Cell>
                  <Cell className="text-right tabular-nums">
                    <div>{moneyField(row, ["required_10x_price"])}</div>
                    <FillTarget row={row} />
                    <PremiumCapHint row={row} />
                  </Cell>
                  <Cell><CandidateSignalEvidence row={row} /></Cell>
                  <Cell>
                    <ThesisCompactSummary
                      request={request}
                      validation={validation}
                      thesis={thesis}
                      expanded={thesisExpanded}
                      onToggle={() => setExpandedThesisEvent(thesisExpanded ? null : eventId)}
                    />
                  </Cell>
                  <Cell><OpportunityOutcome mark={mark} attribution={attribution} /></Cell>
                  <Cell><FullText>{candidateActionText(row, validation)}</FullText></Cell>
                </tr>
                {thesisExpanded ? (
                  <tr className="border-b border-border bg-muted/20">
                    <td colSpan={9} className="px-3 py-4">
                      <div className="max-w-5xl rounded-md border border-border bg-card p-4">
                        <OpportunityThesisSummary row={row} request={request} validation={validation} thesis={thesis} agentRuntime={agentRuntime} />
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <div className="flex flex-col gap-2 border-t border-border p-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
        <div>
          Showing {focusedRows.length ? (boundedPage * CANDIDATE_PAGE_SIZE + 1).toLocaleString() : "0"}-{Math.min((boundedPage + 1) * CANDIDATE_PAGE_SIZE, focusedRows.length).toLocaleString()} of {focusedRows.length.toLocaleString()}
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" size="sm" disabled={boundedPage === 0} onClick={() => setPage((current) => Math.max(0, current - 1))} aria-label="Previous candidate page">
            <ChevronLeft className="size-4" />
          </Button>
          <span className="min-w-24 text-center tabular-nums">Page {boundedPage + 1} / {pageCount}</span>
          <Button type="button" variant="outline" size="sm" disabled={boundedPage >= pageCount - 1} onClick={() => setPage((current) => Math.min(pageCount - 1, current + 1))} aria-label="Next candidate page">
            <ChevronRight className="size-4" />
          </Button>
        </div>
      </div>
    </DataTableFrame>
  );
}

function CandidateMobileCard({
  row,
  request,
  validation,
  thesis,
  mark,
  attribution,
  agentRuntime,
  expanded,
  onToggleThesis,
  onOpenTicker,
}: {
  row: RowRecord;
  request: RowRecord | undefined;
  validation: RowRecord | undefined;
  thesis: RowRecord | undefined;
  mark: RowRecord | undefined;
  attribution: RowRecord | undefined;
  agentRuntime: OptionThesisAgentRuntime;
  expanded: boolean;
  onToggleThesis: () => void;
  onOpenTicker: OpenTicker;
}) {
  const state = stateOf(row);
  const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
  const qualityFlags = listField(row, ["quality_flags"]);
  return (
    <article className={cn("rounded-md border border-border bg-card p-3", qualityStatus === "bad" && "border-destructive/40 bg-destructive/5", qualityStatus === "caution" && "border-amber-500/30 bg-amber-50/30 dark:bg-amber-950/10")}>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <TickerButton ticker={textField(row, ["ticker"])} onOpenTicker={onOpenTicker} />
          <FullText>{displayField(row, ["contract_id"])}</FullText>
          <div className="mt-1 text-xs text-muted-foreground">
            {titleLabel(stringFromRecord(recordField(row, "raw"), "option_type", "call"))} {formatShortDate(stringFromRecord(recordField(row, "raw"), "expiration"))}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <div className="flex items-center gap-1.5">
            <StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge>
            <QualityIndicator status={qualityStatus} flags={qualityFlags} />
          </div>
          <div className="text-xs text-muted-foreground">conviction {formatScore(candidateConviction(row))}</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2">
        <InlineMetric label="Premium" value={moneyField(row, ["premium_mid"])} />
        <InlineMetric label="10x Price" value={moneyField(row, ["required_10x_price"])} />
        <InlineMetric label="Move" value={formatRatio(numberField(row, ["required_move_pct"], Number.NaN))} />
      </div>

      <div className="mt-3 space-y-3">
        <MobileSection label="Signal Evidence"><CandidateSignalEvidence row={row} /></MobileSection>
        <MobileSection label="Thesis">
          <ThesisCompactSummary request={request} validation={validation} thesis={thesis} expanded={expanded} onToggle={onToggleThesis} />
          {expanded ? (
            <div className="mt-3 rounded-md border border-border bg-background p-3">
              <OpportunityThesisSummary row={row} request={request} validation={validation} thesis={thesis} agentRuntime={agentRuntime} />
            </div>
          ) : null}
        </MobileSection>
        <MobileSection label="Impact So Far"><OpportunityOutcome mark={mark} attribution={attribution} /></MobileSection>
        <MobileSection label="Next Action"><FullText>{candidateActionText(row, validation)}</FullText></MobileSection>
      </div>
    </article>
  );
}

function ThesisCompactSummary({
  request,
  validation,
  thesis,
  expanded,
  onToggle,
}: {
  request: RowRecord | undefined;
  validation: RowRecord | undefined;
  thesis: RowRecord | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  const requestStatus = textField(request, ["status"]);
  const hasDetail = Boolean(validation || thesis || request);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        <StatusBadge tone={thesisStateTone(textField(validation, ["state"]))}>{thesisValidationLabel(validation)}</StatusBadge>
        {requestStatus ? <StatusBadge tone={toneFromText(requestStatus)}>{titleLabel(requestStatus)}</StatusBadge> : null}
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-8"
        onClick={onToggle}
        disabled={!hasDetail}
        aria-expanded={expanded}
      >
        <ChevronDown className={cn("size-4 transition-transform", expanded && "rotate-180")} />
        <span>{expanded ? "Hide thesis" : hasDetail ? "Expand thesis" : "No thesis detail"}</span>
      </Button>
    </div>
  );
}

function CandidateSignalEvidence({ row }: { row: RowRecord }) {
  const raw = recordField(row, "raw");
  const hardRejects = listFromRecord(raw, "hard_rejects");
  const blockers = listFromRecord(raw, "blockers");
  const positives = listFromRecord(raw, "positives");
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-1.5">
        <MetricPill label="Conviction" value={formatScore(candidateConviction(row))} />
        <MetricPill label="Move" value={formatRatio(numberField(row, ["required_move_pct"], Number.NaN))} />
      </div>
      {hardRejects.length ? <ReadableReasonGroup label="Hard rejects" reasons={hardRejects} tone="bad" /> : null}
      {blockers.length ? <ReadableReasonGroup label="Blockers" reasons={blockers} tone="warn" /> : null}
      {positives.length ? <ReadableReasonGroup label="Supports" reasons={positives} tone="good" /> : null}
      {!hardRejects.length && !blockers.length && !positives.length ? <StatusBadge tone="muted">No stored evidence</StatusBadge> : null}
    </div>
  );
}

function candidateActionText(row: RowRecord, validation: RowRecord | undefined): string {
  const state = stateOf(row);
  const validationState = textField(validation, ["state"]).toLowerCase();
  const redTeam = textField(validation, ["red_team_status"]).toLowerCase();
  const raw = recordField(row, "raw");
  const hardRejects = listFromRecord(raw, "hard_rejects");
  const blockers = listFromRecord(raw, "blockers");
  if (hardRejects.length) return `Do not advance: ${hardRejects.slice(0, 3).map(reasonLabel).join(", ")}.`;
  if (validationState.includes("invalidated") || redTeam.includes("hard_risk")) return "Do not advance until thesis invalidation or hard red-team risk is resolved.";
  if (state === "FIRE" && blockers.length) return `Research, not trade-ready: ${blockers.slice(0, 3).map(reasonLabel).join(", ")}.`;
  if (state === "FIRE") return "Top signal: review thesis expansion, fill discipline, and kill switch before any trade review.";
  if (state === "SETUP") return blockers.length ? `Monitor setup: ${blockers.slice(0, 3).map(reasonLabel).join(", ")}.` : "Monitor setup until it clears FIRE gates.";
  return "Watch only until ranking, data quality, and thesis gates improve.";
}

function QualityIndicator({ status, flags }: { status: string; flags: string[] }) {
  if (status === "ok" || !flags.length) return null;
  const title = flags.map(titleLabel).join(", ");
  return (
    <span
      className={cn(
        "inline-flex h-5 w-5 items-center justify-center rounded-full",
        status === "bad" ? "text-destructive" : "text-amber-600",
      )}
      title={title}
      aria-label={title}
    >
      <AlertTriangle className="h-4 w-4" aria-hidden="true" />
    </span>
  );
}

function HelpLabel({ label, detail }: { label: string; detail: string }) {
  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex cursor-help items-center justify-end gap-1 underline decoration-dotted underline-offset-4">{label}</span>
        </TooltipTrigger>
        <TooltipContent className="max-w-80 text-xs leading-5">{detail}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function FillTarget({ row }: { row: RowRecord }) {
  const strike = numberFromRecord(recordField(row, "raw"), "strike");
  const fill = numberField(row, ["premium_fill_assumption"], Number.NaN);
  const midTarget = numberField(row, ["required_10x_price"], Number.NaN);
  if (!Number.isFinite(strike) || !Number.isFinite(fill)) return null;
  const fillTarget = strike + fill * 10;
  if (!Number.isFinite(fillTarget) || Math.abs(fillTarget - midTarget) < 0.01) return null;
  return <div className="text-xs text-muted-foreground">fill {formatMoney(fillTarget)}</div>;
}

function PremiumCapHint({ row }: { row: RowRecord }) {
  const cap = numberField(row, ["buy_under"], Number.NaN);
  const fill = numberField(row, ["premium_fill_assumption"], Number.NaN);
  if (!Number.isFinite(cap) || !Number.isFinite(fill)) return null;
  const room = cap - fill;
  if (!Number.isFinite(room)) return null;
  return (
    <div className={cn("text-xs", room >= 0 ? "text-muted-foreground" : "text-destructive")}>
      {room >= 0 ? `cap room ${formatMoney(room)}` : `over cap ${formatMoney(Math.abs(room))}`}
    </div>
  );
}

function OpportunityOutcome({ mark, attribution }: { mark: RowRecord | undefined; attribution: RowRecord | undefined }) {
  if (!mark) {
    return (
      <div className="min-w-0">
        <StatusBadge tone="muted">Waiting for next price</StatusBadge>
        <div className="mt-1 text-xs text-muted-foreground">No later option snapshot yet</div>
      </div>
    );
  }
  const maturity = outcomeMaturity(mark);
  const currentReturn = numberField(mark, ["current_return"], Number.NaN);
  const maxReturn = numberField(mark, ["max_return_since_alert"], Number.NaN);
  const label = textField(attribution, ["label"]);
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge tone={maturity.tone}>{maturity.label}</StatusBadge>
        {label ? <StatusBadge tone={attributionTone(label)}>{titleLabel(label)}</StatusBadge> : null}
      </div>
      <div className="mt-1 truncate text-xs text-muted-foreground">
        Now {formatSignedRatio(currentReturn)} / max {formatMultiple(maxReturn)}
      </div>
    </div>
  );
}

function ReasonChips({ row }: { row: RowRecord }) {
  const raw = recordField(row, "raw");
  const hardRejects = listFromRecord(raw, "hard_rejects");
  const blockers = listFromRecord(raw, "blockers");
  const positives = listFromRecord(raw, "positives");
  const negativeReasons = [...hardRejects, ...blockers];
  const visibleReasons = negativeReasons.length ? negativeReasons.slice(0, 3) : positives.slice(0, 3);
  const extraCount = (negativeReasons.length ? negativeReasons.length : positives.length) - visibleReasons.length;
  return (
    <div className="flex flex-wrap gap-1.5">
      {visibleReasons.length ? visibleReasons.map((reason) => (
        <ReasonChip key={reason} reason={reason} tone={negativeReasons.includes(reason) ? "warn" : "good"} />
      )) : <StatusBadge tone="good">Core gates passed</StatusBadge>}
      {extraCount > 0 ? <span className="rounded-md border border-border px-2 py-0.5 text-xs text-muted-foreground">+{extraCount}</span> : null}
    </div>
  );
}

function ReasonChip({ reason, tone }: { reason: string; tone: Tone }) {
  return (
    <span
      className={cn(
        "rounded-md border px-2 py-0.5 text-xs font-medium",
        tone === "good" && "border-green-500/30 bg-green-50/30 text-foreground dark:bg-green-950/20",
        tone === "bad" && "border-destructive/30 bg-destructive/5 text-destructive",
        tone !== "good" && tone !== "bad" && "border-amber-500/30 bg-amber-50/40 text-foreground dark:bg-amber-950/20",
      )}
      title={reason}
    >
      {reasonLabel(reason)}
    </span>
  );
}

function MissedWinnersTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No missed winners" detail="No unalerted 5x or 10x contracts are stored." icon={TrendingUp} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Missed Winners" count={rows.length} />}>
      <table className="w-full min-w-[1160px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Threshold</Head>
            <Head className="text-right">Max Return</Head>
            <Head>Filter Reason</Head>
            <Head>Strategy Family</Head>
            <Head className="text-right">Entry</Head>
            <Head className="text-right">Winner</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            return (
              <tr key={textField(row, ["missed_id"], `${ticker}-${textField(row, ["contract_id"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={textField(row, ["winner_threshold"]).toLowerCase() === "10x" ? "bad" : "warn"}>{displayField(row, ["winner_threshold"])}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{formatMultiple(numberField(row, ["max_return_seen"], Number.NaN))}</Cell>
                <Cell className="max-w-[360px]"><Truncated>{displayField(row, ["filter_reason"])}</Truncated></Cell>
                <Cell className="max-w-[240px]"><Truncated>{displayField(row, ["proposed_strategy_family"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["entry_price_assumption"])}</Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["winner_price"])}</Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function LearningProgressPanel({
  opportunities,
  latestMarkByEvent,
  latestAttributionByEvent,
  cohorts,
  proposals,
  missedWinners,
  postmortemRequests,
  postmortems,
}: {
  opportunities: RowRecord[];
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  cohorts: RowRecord[];
  proposals: RowRecord[];
  missedWinners: RowRecord[];
  postmortemRequests: RowRecord[];
  postmortems: RowRecord[];
}) {
  const currentMarks = opportunities.map((row) => latestMarkByEvent.get(textField(row, ["event_id"]))).filter(Boolean) as RowRecord[];
  const oneDay = countWhere(currentMarks, (row) => Number.isFinite(numberField(row, ["return_1d"], Number.NaN)));
  const fiveDay = countWhere(currentMarks, (row) => Number.isFinite(numberField(row, ["return_5d"], Number.NaN)));
  const attributed = countWhere(opportunities, (row) => latestAttributionByEvent.has(textField(row, ["event_id"])));
  const matureCohorts = cohorts.filter(cohortHasMatureEvidence).length;
  const openPostmortems = countWhere(postmortemRequests, (row) => textField(row, ["status"], "open").toLowerCase() === "open");
  const readyProposals = countWhere(proposals, (row) => textField(row, ["status"]).toLowerCase() === "ready_for_human_review");
  const items: Array<[string, string, string, Tone]> = [
    ["Current outcomes", `${oneDay}/${opportunities.length}`, "current opportunities with at least 1D observed", oneDay ? "good" : "warn"],
    ["5D outcomes", `${fiveDay}/${opportunities.length}`, "current opportunities with a 5D read", fiveDay ? "good" : "muted"],
    ["Attribution", `${attributed}/${opportunities.length}`, "current opportunities with an explained move", attributed ? "good" : "muted"],
    ["Cohorts ready", `${matureCohorts}/${cohorts.length}`, "cohorts with enough post-entry evidence to interpret", matureCohorts ? "good" : "warn"],
    ["Missed winners", missedWinners.length.toLocaleString(), "unalerted 5x/10x contracts found", missedWinners.length ? "warn" : "muted"],
    ["Strategy changes", readyProposals.toLocaleString(), "proposals through deterministic gates", readyProposals ? "good" : proposals.length ? "warn" : "muted"],
    ["Postmortems", postmortems.length.toLocaleString(), "completed agent reviews of important outcomes", postmortems.length ? "good" : openPostmortems ? "warn" : "muted"],
  ];
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Learning Progress</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Learning becomes actionable only after candidates have post-entry outcome windows. Current cohort hit rates are withheld until the cohort has mature evidence, so pending contracts are not shown as failures.
          </p>
        </div>
        <StatusBadge tone={matureCohorts ? "good" : "warn"}>{matureCohorts ? "Evidence available" : "Collecting outcomes"}</StatusBadge>
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {items.map(([label, value, detail, tone]) => (
          <div key={label} className="rounded-md border border-border/70 bg-background px-3 py-2">
            <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
            <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
            <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function CohortResultsTable({ rows }: { rows: RowRecord[] }) {
  if (!rows.length) {
    return <EmptyState title="No cohort results" detail="No deterministic setup cohorts have enough shadow outcomes yet." icon={Activity} />;
  }

  const matureRows = rows.filter(cohortHasMatureEvidence);
  const collectingRows = rows.filter((row) => !cohortHasMatureEvidence(row));
  const topCollectingRows = collectingRows
    .slice()
    .sort((left, right) => numberField(right, ["candidate_count"], 0) - numberField(left, ["candidate_count"], 0))
    .slice(0, 6);

  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Cohort Learning</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Cohorts are grouped by setup, IV, liquidity, move burden, and market regime. Hit-rate and drawdown numbers are shown only after the outcome window is mature.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone={matureRows.length ? "good" : "warn"}>{matureRows.length ? `${matureRows.length} readable` : "Collecting outcomes"}</StatusBadge>
          <StatusBadge tone="muted">{rows.length.toLocaleString()} cohorts</StatusBadge>
        </div>
      </div>

      {!matureRows.length ? (
        <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-3 text-sm leading-6 text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-100">
          No cohort has a mature post-entry window yet. Current rows are useful for coverage only; zero hit rates here mean "not observed long enough," not "strategy failed."
        </div>
      ) : (
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          {matureRows.slice(0, 6).map((row) => (
            <CohortInsightCard key={cohortKey(row)} row={row} mature />
          ))}
        </div>
      )}

      {topCollectingRows.length ? (
        <div className="mt-4">
          <div className="mb-2 text-xs font-semibold uppercase text-muted-foreground">Largest Collecting Cohorts</div>
          <div className="grid gap-2 lg:grid-cols-3">
            {topCollectingRows.map((row) => (
              <CohortInsightCard key={cohortKey(row)} row={row} mature={false} />
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function CohortInsightCard({ row, mature }: { row: RowRecord; mature: boolean }) {
  const stats = cohortObservationStats(row);
  return (
    <article className="rounded-md border border-border/70 bg-background p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{titleLabel(displayField(row, ["cohort_value"]))}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{titleLabel(displayField(row, ["cohort_type"]))}</div>
        </div>
        <StatusBadge tone={mature ? "good" : "warn"}>{mature ? "Readable" : "Collecting"}</StatusBadge>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <MetricPill label="Signals" value={formatNumber(numberField(row, ["candidate_count"], Number.NaN), 0)} />
        <MetricPill label="Observed" value={formatObservedWindow(stats.maxObservationDays)} />
        <MetricPill label="Sample" value={stats.sampleCount ? stats.sampleCount.toLocaleString() : "-"} />
      </div>
      {mature ? (
        <div className="mt-3 grid grid-cols-3 gap-2">
          <MetricPill label="2x" value={formatRatio(numberField(row, ["hit_rate_2x"], Number.NaN))} />
          <MetricPill label="5x" value={formatRatio(numberField(row, ["hit_rate_5x"], Number.NaN))} />
          <MetricPill label="Max" value={formatMultiple(numberField(row, ["median_max_return"], Number.NaN))} />
        </div>
      ) : null}
      <p className="mt-3 text-sm leading-6 text-muted-foreground">{mature ? cohortDefinition(row) : "Waiting for at least one trading-day outcome before judging hit rate or drawdown."}</p>
    </article>
  );
}

function PostmortemRequestsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No postmortem requests" detail="No important outcomes are queued for agent postmortem." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Premarket Review Queue" count={rows.length} />}>
      <table className="w-full min-w-[880px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head>Outcome</Head>
            <Head className="text-right">Priority</Head>
            <Head>Created</Head>
            <Head>Decision Impact</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const status = textField(row, ["status"], "open");
            return (
              <tr key={textField(row, ["request_id"], `${ticker}-${textField(row, ["source_id"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["source_type"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["priority_score"], Number.NaN))}</Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                <Cell className="max-w-[360px]"><Truncated>{postmortemImpact(row)}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function postmortemImpact(row: RowRecord): string {
  const sourceType = textField(row, ["source_type"]);
  if (sourceType.includes("winner")) return "Explain what made the move work before changing strategy gates.";
  if (sourceType.includes("loser")) return "Explain whether the setup failed, aged out, or exposed a bad gate.";
  return "Summarize only if it changes ranking, risk, or strategy rules.";
}

function PostmortemsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No postmortems" detail="No structured agent postmortems are stored." icon={BrainCircuit} />;
  }

  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Structured Postmortems</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Completed reviews are summarized as decision takeaways: why the setup failed, what rule would change, and what tradeoff that introduces.
          </p>
        </div>
        <StatusBadge tone="good">{rows.length.toLocaleString()} completed</StatusBadge>
      </div>
      <div className="mt-4 grid gap-3 xl:grid-cols-2">
        {rows.slice(0, 8).map((row) => (
          <PostmortemInsightCard key={textField(row, ["postmortem_id"], `${textField(row, ["ticker"])}-${textField(row, ["source_id"])}`)} row={row} onOpenTicker={onOpenTicker} />
        ))}
      </div>
    </section>
  );
}

function PostmortemInsightCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: OpenTicker }) {
  const ticker = textField(row, ["ticker"]);
  const evidence = jsonArrayField(row, "evidence").filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  const failure = displayField(row, ["failure_type"], "Unclassified failure");
  const confidence = numberField(row, ["confidence"], Number.NaN);
  const ruleChange = displayField(row, ["proposed_rule_change"]);
  const expectedEffect = displayField(row, ["expected_effect"]);
  const risk = displayField(row, ["risk"]);
  return (
    <article className="rounded-md border border-border/70 bg-background p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : <span className="font-semibold">Unknown</span>}
            <StatusBadge tone="bad">{titleLabel(displayField(row, ["outcome_type"], "Outcome"))}</StatusBadge>
            <StatusBadge tone={Number.isFinite(confidence) && confidence >= 0.8 ? "good" : "warn"}>{Number.isFinite(confidence) ? `${Math.round(confidence * 100)}% confidence` : "Confidence pending"}</StatusBadge>
          </div>
          <h3 className="mt-2 text-sm font-semibold leading-6">{titleLabel(failure)}</h3>
        </div>
        <div className="text-xs text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</div>
      </div>

      {evidence.length ? (
        <ul className="mt-3 space-y-1 text-sm leading-6 text-muted-foreground">
          {evidence.slice(0, 2).map((item) => (
            <li key={item}>- {item}</li>
          ))}
        </ul>
      ) : null}

      <div className="mt-3 space-y-2 text-sm leading-6">
        {ruleChange ? <InsightLine label="Rule change" value={ruleChange} /> : null}
        {expectedEffect ? <InsightLine label="Decision impact" value={expectedEffect} /> : null}
        {risk ? <InsightLine label="Tradeoff" value={risk} /> : null}
      </div>
    </article>
  );
}

function StrategyProposalsTable({
  rows,
  backtestByProposal,
  forwardByProposal,
  promotingProposal,
  promotionError,
  onPromote,
}: {
  rows: RowRecord[];
  backtestByProposal: Map<string, RowRecord>;
  forwardByProposal: Map<string, RowRecord>;
  promotingProposal: string | null;
  promotionError: string | null;
  onPromote: (proposalId: string) => Promise<void> | void;
}) {
  if (!rows.length) {
    return <EmptyState title="No strategy proposals" detail="No mutation proposals are waiting in the learning engine." icon={GitBranchPlus} />;
  }

  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Strategy Mutation Gates</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Agent proposals are advisory until deterministic backtest, forward shadow test, and human approval all clear. Failed gates are shown as blockers, not action items.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone="muted">{rows.length.toLocaleString()} proposals</StatusBadge>
          {promotionError ? <StatusBadge tone="bad">{promotionError}</StatusBadge> : null}
        </div>
      </div>
      <div className="mt-4 grid gap-3 xl:grid-cols-2">
        {rows.slice(0, 8).map((row) => {
          const proposalId = textField(row, ["proposal_id"]);
          return (
            <StrategyProposalCard
              key={proposalId || textField(row, ["proposed_strategy_version"])}
              row={row}
              backtest={backtestByProposal.get(proposalId)}
              forward={forwardByProposal.get(proposalId)}
              isPromoting={promotingProposal === proposalId}
              onPromote={onPromote}
            />
          );
        })}
      </div>
    </section>
  );
}

function StrategyProposalCard({
  row,
  backtest,
  forward,
  isPromoting,
  onPromote,
}: {
  row: RowRecord;
  backtest: RowRecord | undefined;
  forward: RowRecord | undefined;
  isPromoting: boolean;
  onPromote: (proposalId: string) => Promise<void> | void;
}) {
  const proposalId = textField(row, ["proposal_id"]);
  const status = textField(row, ["status"], "pending");
  const human = textField(row, ["human_approval_status"], "pending");
  const backtestVerdict = textField(backtest, ["verdict"]).toLowerCase();
  const forwardVerdict = textField(forward, ["verdict", "status"]).toLowerCase();
  const approvedBy = textField(row, ["approved_by"]);
  const approvedAt = textField(row, ["approved_at"]);
  const canPromote =
    Boolean(proposalId) &&
    status === "ready_for_human_review" &&
    human !== "approved" &&
    backtestVerdict === "pass" &&
    forwardVerdict === "pass";
  const gate = proposalGateSummary(row, backtest, forward);
  const changes = proposalChangeItems(row);
  const note = proposalChangeNote(row);

  return (
    <article className="rounded-md border border-border/70 bg-background p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={gate.tone}>{gate.label}</StatusBadge>
            <StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge>
            <StatusBadge tone={human === "approved" ? "good" : human === "rejected" ? "bad" : "warn"}>{titleLabel(human)}</StatusBadge>
          </div>
          <h3 className="mt-2 text-sm font-semibold leading-6">{compactStrategyVersion(displayField(row, ["proposed_strategy_version"]))}</h3>
          {approvedBy || approvedAt ? (
            <div className="mt-1 text-xs text-muted-foreground">{approvedBy ? `approved by ${approvedBy}` : "approved"}{approvedAt ? ` ${formatDate(approvedAt)}` : ""}</div>
          ) : null}
        </div>
        {human === "approved" ? (
          <StatusBadge tone="good">Promoted</StatusBadge>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9 min-h-9 w-fit"
            disabled={!canPromote || isPromoting}
            title={canPromote ? "Promote strategy" : gate.detail}
            onClick={() => void onPromote(proposalId)}
          >
            {isPromoting ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
            <span>Promote</span>
          </Button>
        )}
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-3">
        <GatePill label="Backtest" row={backtest} keys={["verdict"]} detail={backtestDetail(backtest)} />
        <GatePill label="Forward" row={forward} keys={["verdict", "status"]} detail={forwardDetail(forward)} />
        <div className="rounded-md bg-muted px-2 py-2">
          <div className="text-[10px] font-semibold uppercase text-muted-foreground">Gate Meaning</div>
          <div className={cn("mt-1 text-xs font-semibold", toneText(gate.tone))}>{gate.detail}</div>
        </div>
      </div>

      {note ? <InsightLine className="mt-3" label="Agent hypothesis" value={note} /> : null}
      {changes.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {changes.slice(0, 6).map((change) => (
            <span key={change} className="rounded-md border border-border/70 bg-card px-2 py-1 text-xs text-muted-foreground">{change}</span>
          ))}
        </div>
      ) : null}
      <div className="mt-3 space-y-2 text-sm leading-6">
        <InsightLine label="Why proposed" value={displayField(row, ["rationale"])} />
        <InsightLine label="Risk" value={displayField(row, ["risk"])} />
      </div>
    </article>
  );
}

function GatePill({ label, row, keys, detail }: { label: string; row: RowRecord | undefined; keys: string[]; detail: string }) {
  const verdict = row ? textField(row, keys, "pending") : "pending";
  return (
    <div className="rounded-md bg-muted px-2 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm font-semibold", toneText(verdictTone(verdict)))}>{titleLabel(verdict)}</div>
      <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
    </div>
  );
}

function InsightLine({ label, value, className }: { label: string; value: string; className?: string }) {
  if (!value) return null;
  return (
    <div className={cn("text-sm leading-6", className)}>
      <span className="font-semibold text-foreground">{label}: </span>
      <span className="text-muted-foreground">{value}</span>
    </div>
  );
}

function cohortKey(row: RowRecord): string {
  return textField(row, ["cohort_id"], `${textField(row, ["cohort_type"])}-${textField(row, ["cohort_value"])}`);
}

function cohortObservationStats(row: RowRecord): { sampleCount: number; maxObservationDays: number; matureCount: number } {
  const raw = recordField(row, "raw");
  const outcomes = Array.isArray(raw?.sample_outcomes) ? raw.sample_outcomes : [];
  let maxObservationDays = Number.NaN;
  for (const outcome of outcomes) {
    if (!outcome || typeof outcome !== "object" || Array.isArray(outcome)) continue;
    const record = outcome as Record<string, JsonValue>;
    const days = typeof record.observation_days === "number" ? record.observation_days : Number.NaN;
    const hours = typeof record.observation_hours === "number" ? record.observation_hours : Number.NaN;
    const normalizedDays = Number.isFinite(days) ? days : Number.isFinite(hours) ? hours / 24 : Number.NaN;
    if (Number.isFinite(normalizedDays) && (!Number.isFinite(maxObservationDays) || normalizedDays > maxObservationDays)) {
      maxObservationDays = normalizedDays;
    }
  }
  const maturity = jsonRecord(raw?.maturity);
  return {
    sampleCount: outcomes.length,
    maxObservationDays,
    matureCount: numberFromRecord(maturity, "mature_count"),
  };
}

function formatObservedWindow(days: number): string {
  if (!Number.isFinite(days)) return "-";
  if (days <= 0) return "<1d";
  if (days < 1) return `${Math.max(1, Math.round(days * 24))}h`;
  return `${days.toFixed(days >= 10 ? 0 : 1)}d`;
}

function proposalGateSummary(row: RowRecord, backtest: RowRecord | undefined, forward: RowRecord | undefined): { label: string; detail: string; tone: Tone } {
  const human = textField(row, ["human_approval_status"], "pending").toLowerCase();
  if (human === "approved") return { label: "Approved", detail: "Human approval is recorded.", tone: "good" };
  const backtestVerdict = textField(backtest, ["verdict"]).toLowerCase();
  const forwardVerdict = textField(forward, ["verdict", "status"]).toLowerCase();
  if (!backtest) return { label: "Waiting backtest", detail: "No deterministic backtest result yet.", tone: "warn" };
  if (backtestVerdict === "fail") return { label: "Blocked", detail: "Backtest failed; do not promote.", tone: "bad" };
  if (backtestVerdict !== "pass") return { label: "Backtest pending", detail: "Needs a passing deterministic backtest.", tone: "warn" };
  if (!forward) return { label: "Waiting forward test", detail: "No forward shadow test result yet.", tone: "warn" };
  if (forwardVerdict === "pass") return { label: "Human review", detail: "Deterministic gates passed; needs approval.", tone: "good" };
  if (forwardVerdict.includes("collecting") || forwardVerdict === "active") return { label: "Collecting", detail: forwardDetail(forward), tone: "warn" };
  if (forwardVerdict === "fail") return { label: "Blocked", detail: "Forward shadow test failed.", tone: "bad" };
  return { label: "Pending", detail: "Gate state is still unresolved.", tone: "warn" };
}

function proposalChangeItems(row: RowRecord): string[] {
  const changes = recordField(row, "proposed_parameter_changes");
  if (!changes) return [];
  return Object.entries(changes)
    .filter(([key, value]) => !["candidate_note", "filter_reason", "setup_type"].includes(key) && valueIsPresent(value))
    .map(([key, value]) => `${titleLabel(key)}: ${formatConfigValue(value)}`);
}

function proposalChangeNote(row: RowRecord): string {
  const changes = recordField(row, "proposed_parameter_changes");
  return stringFromRecord(changes, "candidate_note") || stringFromRecord(changes, "filter_reason") || stringFromRecord(changes, "setup_type");
}

function compactStrategyVersion(value: string): string {
  if (!value) return "Proposed strategy change";
  return titleLabel(value.replace(/^leap_10x_reversal_v1_?/, "").replace(/_agent_proposed_v\d+$/, "") || value);
}

function backtestDetail(row: RowRecord | undefined): string {
  if (!row) return "Waiting for deterministic replay.";
  const baseline = numberField(row, ["baseline_candidate_count"], Number.NaN);
  const proposed = numberField(row, ["proposed_candidate_count"], Number.NaN);
  const verdict = textField(row, ["verdict"]).toLowerCase();
  const countDetail = Number.isFinite(baseline) && Number.isFinite(proposed) ? `${formatNumber(baseline, 0)} -> ${formatNumber(proposed, 0)} candidates` : "Candidate impact unavailable";
  if (verdict === "fail") return `${countDetail}; no proven improvement.`;
  if (verdict === "pass") return `${countDetail}; passed replay.`;
  return countDetail;
}

function forwardDetail(row: RowRecord | undefined): string {
  if (!row) return "Waiting for shadow comparison.";
  const days = numberField(row, ["days_observed"], Number.NaN);
  const raw = recordField(row, "raw");
  const minimumDays = numberFromRecord(raw, "min_forward_test_days");
  if (Number.isFinite(days) && Number.isFinite(minimumDays)) return `${formatNumber(days, 0)}/${formatNumber(minimumDays, 0)} days observed`;
  if (Number.isFinite(days)) return `${formatNumber(days, 0)} days observed`;
  return "Observation window pending.";
}

function valueIsPresent(value: JsonValue | undefined): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function formatConfigValue(value: JsonValue): string {
  if (typeof value === "boolean") return value ? "required" : "off";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  if (typeof value === "string") return titleLabel(value);
  if (Array.isArray(value)) return `${value.length} items`;
  if (value && typeof value === "object") return "configured";
  return "-";
}

function ThesisPipelinePanel({
  requests,
  theses,
  validations,
  agentRuntime,
}: {
  requests: RowRecord[];
  theses: RowRecord[];
  validations: RowRecord[];
  agentRuntime: OptionThesisAgentRuntime;
}) {
  const open = countWhere(requests, (row) => textField(row, ["status"], "open").toLowerCase() === "open");
  const failed = countWhere(requests, (row) => textField(row, ["status"]).toLowerCase().includes("failed"));
  const validated = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "validated");
  const tracking = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "tracking");
  const validationPending = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "pending");
  const oldestOpen = oldestDate(requests.filter((row) => textField(row, ["status"], "open").toLowerCase() === "open"), "created_at");
  const queueAtCap = open >= agentRuntime.requestCap;
  const items: Array<[string, string, string, Tone]> = [
    ["Premarket queue", open.toLocaleString(), queueAtCap ? `at current queue cap of ${agentRuntime.requestCap}` : "top-ranked signals awaiting synthesis", open ? "warn" : "muted"],
    ["Agent cadence", agentRuntime.active ? "Daily" : "Paused", agentRuntime.active ? `premarket batch limit ${agentRuntime.limit}` : "option thesis command is not enabled", agentRuntime.active ? "good" : open ? "warn" : "muted"],
    ["Oldest open", oldestOpen ? formatDate(oldestOpen) : "-", "age of the oldest unfulfilled request", oldestOpen ? "warn" : "muted"],
    ["Completed hypotheses", theses.length.toLocaleString(), "structured hypotheses returned by agents", theses.length ? "good" : "muted"],
    ["Tracking", tracking.toLocaleString(), "active hypotheses being checked against price, proof, and invalidation gates", tracking ? "info" : "muted"],
    ["Needs proof", validationPending.toLocaleString(), "hypotheses blocked by missing deterministic proof support", validationPending ? "warn" : "muted"],
    ["Validated", validated.toLocaleString(), "theses passing deterministic checks", validated ? "good" : validations.length ? "warn" : "muted"],
    ["Failed", failed.toLocaleString(), "agent calls needing attention", failed ? "bad" : "good"],
  ];
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Thesis Checks</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Agent synthesis runs once before the market review window. Deterministic validation decides whether each thesis actually supports the option signal.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone={agentRuntime.active ? "good" : open ? "warn" : "muted"}>{agentRuntime.active ? "Worker active" : "Worker paused"}</StatusBadge>
          {queueAtCap ? <StatusBadge tone="warn">At request cap</StatusBadge> : null}
          <StatusBadge tone={theses.length ? "good" : open ? "warn" : "muted"}>{theses.length ? "Hypotheses available" : open ? "Requests open" : "No open requests"}</StatusBadge>
        </div>
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
        {items.map(([label, value, detail, tone]) => (
          <div key={label} className="rounded-md border border-border/70 bg-background px-3 py-2">
            <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
            <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
            <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ThesisRequestsTable({
  rows,
  eventById,
  onOpenTicker,
  agentRuntime,
}: {
  rows: RowRecord[];
  eventById: Map<string, RowRecord>;
  onOpenTicker: OpenTicker;
  agentRuntime: OptionThesisAgentRuntime;
}) {
  if (!rows.length) {
    return <EmptyState title="No active thesis requests" detail="No current top-ranked candidates are waiting for agent thesis generation." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame
      title={<SectionTitle title="Premarket Thesis Queue" count={rows.length} />}
      action={<StatusBadge tone={agentRuntime.active ? "good" : "warn"}>{agentRuntime.active ? "Worker active" : "Worker paused"}</StatusBadge>}
    >
      <div className="border-b border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
        Current top-ranked signals waiting for daily premarket synthesis. Completed hypotheses appear below only after deterministic validation links them back to a current candidate.
      </div>
      <table className="w-full min-w-[960px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head className="text-right">Priority</Head>
            <Head>Candidate State</Head>
            <Head>Created</Head>
            <Head>Candidate</Head>
            <Head>Next Step</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const event = eventById.get(textField(row, ["event_id"]));
            const status = textField(row, ["status"], "open");
            const contract = displayField(event, ["contract_id"], textField(row, ["event_id"]));
            return (
              <tr key={textField(row, ["request_id"], `${ticker}-${textField(row, ["created_at"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["priority_score"], Number.NaN))}</Cell>
                <Cell><StatusBadge tone={stateTone(stateOf(event))}>{titleLabel(stateOf(event) || "pending")}</StatusBadge></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                <Cell className="max-w-[260px]"><Truncated>{contract}</Truncated></Cell>
                <Cell className="max-w-[320px]"><Truncated>{status.toLowerCase() === "open" ? "Wait for daily premarket thesis worker" : titleLabel(status)}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function AgentThesisBrowser({ theses, validations, onOpenTicker }: { theses: RowRecord[]; validations: RowRecord[]; onOpenTicker: OpenTicker }) {
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const validationHistoryByThesis = useMemo(() => validationHistoryBy(validations, "thesis_id"), [validations]);
  const latestValidationByThesis = useMemo(() => latestValidationBy(validations, "thesis_id"), [validations]);
  const legacyValidationByTicker = useMemo(() => latestValidationBy(validations.filter((row) => !textField(row, ["thesis_id"])), "ticker"), [validations]);
  const thesisRows = useMemo(
    () => [...theses].sort((left, right) => dateMillis(textField(right, ["created_at"])) - dateMillis(textField(left, ["created_at"]))),
    [theses],
  );

  useEffect(() => {
    if (!thesisRows.length) {
      if (selectedId) setSelectedId("");
      return;
    }
    if (!selectedId || !thesisRows.some((row) => thesisId(row) === selectedId)) {
      setSelectedId(thesisId(thesisRows[0]));
    }
  }, [selectedId, thesisRows]);

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return thesisRows.filter((row) => {
      const history = validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker);
      const validation = history[0];
      const validationState = textField(validation, ["state"], "pending").toLowerCase();
      const redTeam = textField(validation, ["red_team_status"]).toLowerCase();
      if (stateFilter === "tracking" && validationState !== "tracking") return false;
      if (stateFilter === "validated" && validationState !== "validated") return false;
      if (stateFilter === "pending" && validationState !== "pending") return false;
      if (stateFilter === "invalidated" && !validationState.includes("invalidated")) return false;
      if (stateFilter === "fundamental-risk" && !redTeam.includes("hard_risk")) return false;
      if (!normalizedQuery) return true;
      return [
        textField(row, ["ticker"]),
        fullField(row, ["core_thesis"], ""),
        fullField(row, ["bear_case"], ""),
        fullField(row, ["catalyst_summary", "catalysts"], ""),
        fullField(row, ["invalidation_conditions"], ""),
        fullField(validation, ["reason"], ""),
      ].join(" ").toLowerCase().includes(normalizedQuery);
    });
  }, [legacyValidationByTicker, query, stateFilter, thesisRows, validationHistoryByThesis]);

  const selected = filtered.find((row) => thesisId(row) === selectedId) ?? filtered[0];
  const selectedValidation = selected ? validationForThesis(selected, latestValidationByThesis, legacyValidationByTicker) : undefined;
  const selectedValidationHistory = selected ? validationHistoryForThesis(selected, validationHistoryByThesis, legacyValidationByTicker) : [];
  const validated = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase() === "validated");
  const tracking = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase() === "tracking");
  const validationPending = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase() === "pending");
  const invalidated = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase().includes("invalidated"));
  const hardRisk = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["red_team_status"]).toLowerCase().includes("hard_risk"));

  if (!thesisRows.length) {
    return <EmptyState title="No completed theses" detail="No structured agent hypotheses are stored yet." icon={BrainCircuit} />;
  }

  return (
    <section className="overflow-hidden rounded-md border border-border bg-card">
      <div className="border-b border-border p-4">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold">Completed Hypotheses</h2>
              <StatusBadge tone={thesisRows.length ? "good" : "muted"}>{thesisRows.length.toLocaleString()} completed</StatusBadge>
            </div>
            <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
              Browse returned hypotheses with deterministic validation, proof requirements, catalysts, invalidation, and bear case in the same view.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2 text-right sm:min-w-[540px] sm:grid-cols-5">
            <BrowserStat label="Validated" value={validated} tone={validated ? "good" : "muted"} />
            <BrowserStat label="Tracking" value={tracking} tone={tracking ? "info" : "muted"} />
            <BrowserStat label="Needs Proof" value={validationPending} tone={validationPending ? "warn" : "muted"} />
            <BrowserStat label="Invalidated" value={invalidated} tone={invalidated ? "bad" : "muted"} />
            <BrowserStat label="Hard Risk" value={hardRisk} tone={hardRisk ? "warn" : "muted"} />
          </div>
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_220px]">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search theses, catalysts, bear cases..." className="pl-9" />
          </div>
          <Select value={stateFilter} onValueChange={setStateFilter}>
            <SelectTrigger>
              <SelectValue placeholder="Validation state" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All states</SelectItem>
              <SelectItem value="tracking">Tracking</SelectItem>
              <SelectItem value="pending">Needs proof</SelectItem>
              <SelectItem value="validated">Validated</SelectItem>
              <SelectItem value="invalidated">Invalidated</SelectItem>
              <SelectItem value="fundamental-risk">Fundamental risk</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="grid min-h-[640px] xl:grid-cols-[380px_minmax(0,1fr)]">
        <div className="border-b border-border xl:border-b-0 xl:border-r">
          <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-muted-foreground">
            <span>{filtered.length.toLocaleString()} shown</span>
            <span>{stateFilter === "all" ? "Latest first" : stateFilter === "pending" ? "Needs proof" : titleLabel(stateFilter)}</span>
          </div>
          <div className="max-h-[640px] overflow-y-auto">
            {filtered.length ? (
              filtered.map((row) => {
                const id = thesisId(row);
                const history = validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker);
                const validation = history[0];
                return (
                  <div key={id}>
                    <button
                      type="button"
                      className={cn(
                        "block w-full border-b border-border px-4 py-3 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        selected && id === thesisId(selected) ? "bg-accent/70" : "bg-transparent",
                      )}
                      onClick={() => setSelectedId(id)}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-semibold">{displayField(row, ["ticker"], "Unknown")}</span>
                            <StatusBadge tone={thesisStateTone(textField(validation, ["state"]))}>{thesisValidationLabel(validation)}</StatusBadge>
                            {history.length > 1 ? <span className="text-xs text-muted-foreground">{history.length} checks</span> : null}
                          </div>
                          <p className="mt-2 line-clamp-3 text-sm leading-5 text-muted-foreground">{displayField(row, ["core_thesis"], "No core thesis")}</p>
                        </div>
                        <div className="shrink-0 text-right text-xs tabular-nums">
                          <div className="font-semibold">{formatScore(numberField(row, ["confidence"], Number.NaN))}</div>
                          <div className="text-muted-foreground">conf</div>
                        </div>
                      </div>
                      <div className="mt-3 grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                        <span>{moneyField(row, ["bull_target_price"])} bull</span>
                        <span>{moneyField(row, ["base_target_price"])} base</span>
                        <span>{formatShortDate(textField(row, ["bull_target_date"]))}</span>
                      </div>
                    </button>
                    {selected && id === thesisId(selected) ? (
                      <div className="border-b border-border bg-background xl:hidden">
                        <ThesisDetailPane thesis={selected} validation={selectedValidation} validationHistory={selectedValidationHistory} onOpenTicker={onOpenTicker} />
                      </div>
                    ) : null}
                  </div>
                );
              })
            ) : (
              <div className="px-4 py-10 text-sm text-muted-foreground">No theses match the current filter.</div>
            )}
          </div>
        </div>

        {selected ? (
          <div className="hidden xl:block">
          <ThesisDetailPane thesis={selected} validation={selectedValidation} validationHistory={selectedValidationHistory} onOpenTicker={onOpenTicker} />
          </div>
        ) : (
          <div className="p-8 text-sm text-muted-foreground">No thesis matches the current filter. Clear search or choose another validation state.</div>
        )}
      </div>
    </section>
  );
}

function ThesisDetailPane({ thesis, validation, validationHistory, onOpenTicker }: { thesis: RowRecord; validation: RowRecord | undefined; validationHistory: RowRecord[]; onOpenTicker: OpenTicker }) {
  const ticker = textField(thesis, ["ticker"]);
  const state = textField(validation, ["state"]);
  const redTeam = textField(validation, ["red_team_status"], "not_checked");
  return (
    <div className="min-w-0">
      <div className="border-b border-border p-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : <span className="text-lg font-semibold">Unknown ticker</span>}
              <StatusBadge tone={thesisStateTone(state)}>{thesisValidationLabel(validation)}</StatusBadge>
              <StatusBadge tone={validationStatusTone(redTeam)}>{validationStatusLabel(redTeam)}</StatusBadge>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>Created {formatDate(textField(thesis, ["created_at"]))}</span>
              <span>Validation {formatShortDate(textField(validation, ["validation_date"]))}</span>
              <span>{displayField(thesis, ["agent_version"], "Agent version unknown")}</span>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:min-w-[520px]">
            <MetricBox label="Bull Target" value={moneyField(thesis, ["bull_target_price"])} />
            <MetricBox label="Base Target" value={moneyField(thesis, ["base_target_price"])} />
            <MetricBox label="Bull Date" value={formatShortDate(textField(thesis, ["bull_target_date"]))} />
            <MetricBox label="Confidence" value={formatScore(numberField(thesis, ["confidence"], Number.NaN))} />
          </div>
        </div>
      </div>

      <div className="space-y-6 p-5">
        <ReadableSection title="Core Thesis">
          <p className="whitespace-pre-wrap text-sm leading-7 text-foreground">{displayField(thesis, ["core_thesis"], "No core thesis text.")}</p>
        </ReadableSection>

        <div className="grid gap-5 xl:grid-cols-2">
          <ReadableSection title="Required Proofs">
            <ReadableList items={listField(thesis, ["required_proofs"])} empty="No proof points stored." />
          </ReadableSection>
          <ReadableSection title="Catalysts">
            <CatalystList thesis={thesis} />
          </ReadableSection>
          <ReadableSection title="Invalidation">
            <ReadableList items={listField(thesis, ["invalidation_conditions", "invalidation"])} empty="No invalidation conditions stored." />
          </ReadableSection>
          <ReadableSection title="Bear Case">
            <p className="whitespace-pre-wrap text-sm leading-7 text-foreground">{displayField(thesis, ["bear_case"], "No bear case stored.")}</p>
          </ReadableSection>
        </div>

        <ReadableSection title="Validation">
          <div className="flex flex-wrap gap-2">
            <StatusBadge tone={validationStatusTone(textField(validation, ["proof_status"]))}>Proofs {titleLabel(displayField(validation, ["proof_status"], "unknown"))}</StatusBadge>
            <StatusBadge tone={validationStatusTone(textField(validation, ["catalyst_status"]))}>Catalysts {titleLabel(displayField(validation, ["catalyst_status"], "unknown"))}</StatusBadge>
            <StatusBadge tone={validationStatusTone(textField(validation, ["invalidation_status"]))}>Invalidation {titleLabel(displayField(validation, ["invalidation_status"], "unknown"))}</StatusBadge>
            <StatusBadge tone={validationStatusTone(textField(validation, ["evidence_status"]))}>Evidence {titleLabel(displayField(validation, ["evidence_status"], "unknown"))}</StatusBadge>
          </div>
          <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-foreground">{displayField(validation, ["reason"], "No validation reason stored.")}</p>
        </ReadableSection>

        <ReadableSection title="Validation History">
          {validationHistory.length ? (
            <div className="divide-y divide-border rounded-md border border-border">
              {validationHistory.map((row) => (
                <div key={textField(row, ["validation_id"], `${textField(row, ["strategy_version"])}-${textField(row, ["validation_date"])}`)} className="grid gap-3 px-3 py-3 text-sm lg:grid-cols-[150px_190px_minmax(0,1fr)]">
                  <div className="text-xs text-muted-foreground">
                    <div>{formatShortDate(textField(row, ["validation_date"]))}</div>
                    <div>{formatDate(textField(row, ["validated_at"]))}</div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <StatusBadge tone={thesisStateTone(textField(row, ["state"]))}>{thesisValidationLabel(row)}</StatusBadge>
                    <StatusBadge tone={validationStatusTone(textField(row, ["red_team_status"]))}>{validationStatusLabel(displayField(row, ["red_team_status"], "unknown"))}</StatusBadge>
                  </div>
                  <div className="min-w-0">
                    <div className="truncate text-xs text-muted-foreground">{displayField(row, ["strategy_version"], "Strategy unknown")}</div>
                    <p className="mt-1 whitespace-pre-wrap leading-6">{displayField(row, ["reason"], "No validation reason stored.")}</p>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No validation checks are stored for this thesis.</p>
          )}
        </ReadableSection>

        <ReadableSection title="Evidence References">
          <ReadableList items={listField(thesis, ["evidence_refs"])} empty="No evidence references stored." />
        </ReadableSection>
      </div>
    </div>
  );
}

function BrowserStat({ label, value, tone }: { label: string; value: number; tone: Tone }) {
  return (
    <div className="border-l border-border pl-3">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value.toLocaleString()}</div>
    </div>
  );
}

function MetricBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-background px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function ReadableSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0">
      <h3 className="text-xs font-semibold uppercase text-muted-foreground">{title}</h3>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function ReadableList({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ul className="space-y-2">
      {items.map((item, index) => (
        <li key={`${index}-${item.slice(0, 32)}`} className="grid grid-cols-[18px_minmax(0,1fr)] gap-2 text-sm leading-6">
          <span className="mt-2 size-1.5 rounded-full bg-muted-foreground" />
          <span className="whitespace-pre-wrap">{item}</span>
        </li>
      ))}
    </ul>
  );
}

function CatalystList({ thesis }: { thesis: RowRecord }) {
  const catalysts = jsonArrayField(thesis, "catalysts");
  if (!catalysts.length) return <p className="text-sm text-muted-foreground">No catalysts stored.</p>;
  return (
    <div className="space-y-3">
      {catalysts.map((item, index) => {
        const record = jsonRecord(item);
        if (!record) {
          return <p key={index} className="text-sm leading-6">{String(item)}</p>;
        }
        return (
          <div key={`${index}-${stringFromRecord(record, "type", "catalyst")}`} className="border-l-2 border-border pl-3">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge tone="info">{titleLabel(stringFromRecord(record, "type", "Catalyst"))}</StatusBadge>
              <span className="text-xs text-muted-foreground">{stringFromRecord(record, "expected_window", "Window unknown")}</span>
            </div>
            <p className="mt-2 text-sm leading-6">{stringFromRecord(record, "what_to_watch", fullField({ value: item } as RowRecord, ["value"]))}</p>
          </div>
        );
      })}
    </div>
  );
}

function thesisId(row: RowRecord | undefined): string {
  return textField(row, ["thesis_id"], `${textField(row, ["ticker"])}-${textField(row, ["created_at"])}`);
}

function validationForThesis(thesis: RowRecord | undefined, byThesis: Map<string, RowRecord>, byTicker: Map<string, RowRecord>): RowRecord | undefined {
  const id = thesisId(thesis);
  return byThesis.get(id) ?? byTicker.get(textField(thesis, ["ticker"]));
}

function validationHistoryForThesis(thesis: RowRecord | undefined, byThesis: Map<string, RowRecord[]>, legacyByTicker: Map<string, RowRecord>): RowRecord[] {
  const id = thesisId(thesis);
  const rows = byThesis.get(id);
  if (rows?.length) return rows;
  const legacy = legacyByTicker.get(textField(thesis, ["ticker"]));
  return legacy ? [legacy] : [];
}

function VerdictBadge({ row, keys }: { row: RowRecord | undefined; keys: string[] }) {
  if (!row) return <StatusBadge tone="muted">Pending</StatusBadge>;
  const verdict = textField(row, keys, "pending");
  return <StatusBadge tone={verdictTone(verdict)}>{titleLabel(verdict)}</StatusBadge>;
}

function SectionTitle({ title, count }: { title: string; count: number }) {
  return (
    <span className="flex items-center gap-2">
      <span>{title}</span>
      <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground" aria-label={`${count} rows`}>
        {count.toLocaleString()}
      </span>
    </span>
  );
}

function TickerButton({ ticker, onOpenTicker }: { ticker: string; onOpenTicker: OpenTicker }) {
  return (
    <Button type="button" variant="ghost" size="sm" className="-ml-2 h-7 font-semibold tracking-normal" onClick={() => onOpenTicker(ticker)}>
      {ticker}
    </Button>
  );
}

function Head({ children, className }: { children: ReactNode; className?: string }) {
  return <th className={cn("px-3 py-3 font-semibold", className)}>{children}</th>;
}

function Cell({ children, className }: { children: ReactNode; className?: string }) {
  return <td className={cn("px-3 py-3 leading-6", className)}>{children}</td>;
}

function Truncated({ children }: { children: ReactNode }) {
  return <div className="min-w-0 truncate" title={typeof children === "string" ? children : undefined}>{children}</div>;
}

function FullText({ children }: { children: ReactNode }) {
  return <div className="min-w-0 whitespace-pre-wrap break-words leading-6">{children}</div>;
}

function MetricPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-20 rounded-md bg-muted px-2 py-1 text-right">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function rows(table: TablePayload): RowRecord[] {
  return table.rows ?? [];
}

const OPPORTUNITY_STATES = new Set(["FIRE", "SETUP", "WATCH", "HOLD", "TRIM"]);

function isOpportunityCandidate(row: RowRecord): boolean {
  return OPPORTUNITY_STATES.has(stateOf(row));
}

function uniqueText(items: RowRecord[], key: string): string[] {
  return Array.from(new Set(items.map((row) => textField(row, [key])).filter(Boolean)));
}

function uniqueValues(items: string[]): string[] {
  return Array.from(new Set(items.filter(Boolean))).sort((left, right) => left.localeCompare(right));
}

function candidateOpportunityFields(opportunity: RowRecord | undefined): RowRecord {
  if (!opportunity) return {};
  return {
    opportunity_tier: opportunity.tier,
    opportunity_conviction_score: opportunity.conviction_score,
    opportunity_data_contract_status: opportunity.data_contract_status,
    opportunity_id: opportunity.opportunity_id,
  };
}

function candidateConviction(row: RowRecord): number {
  return numberField(row, ["opportunity_conviction_score", "conviction_score", "score"], Number.NEGATIVE_INFINITY);
}

function candidateFamily(row: RowRecord): string {
  const raw = recordField(row, "raw");
  const rawFamily = stringFromRecord(raw, "strategy_family");
  if (rawFamily) return rawFamily;
  const version = textField(row, ["strategy_version"], "leap_10x_reversal_v1");
  return version.replace(/_v\d+$/, "");
}

function countWhere(items: RowRecord[], predicate: (row: RowRecord) => boolean): number {
  return items.reduce((count, row) => count + (predicate(row) ? 1 : 0), 0);
}

function optionThesisAgentState(data: PanelData): OptionThesisAgentRuntime {
  const metadata = data.dashboard.status?.metadata;
  const agents = jsonRecord(metadata?.agents);
  const optionThesis = jsonRecord(agents?.option_thesis);
  const requestCap = numberFromRecord(optionThesis, "request_cap");
  const limit = numberFromRecord(optionThesis, "limit");
  return {
    active: boolFromRecord(optionThesis, "active"),
    enabled: boolFromRecord(optionThesis, "enabled"),
    configured: boolFromRecord(optionThesis, "configured"),
    status: stringFromRecord(optionThesis, "status", "paused"),
    limit: Number.isFinite(limit) ? limit : 20,
    requestCap: Number.isFinite(requestCap) ? requestCap : 12,
  };
}

function stateOf(row: RowRecord | undefined): string {
  return textField(row, ["state"]).toUpperCase();
}

function tierOf(row: RowRecord | undefined): string {
  return textField(row, ["tier"], "Watch");
}

function qualityOf(row: RowRecord | undefined): QualityFilter {
  const quality = textField(row, ["quality_status"], "ok").toLowerCase();
  if (quality === "bad" || quality === "caution" || quality === "ok") return quality;
  return "ok";
}

function outcomeMaturity(mark: RowRecord): { label: string; tone: Tone } {
  if (Number.isFinite(numberField(mark, ["return_20d"], Number.NaN))) return { label: "20D observed", tone: "good" };
  if (Number.isFinite(numberField(mark, ["return_5d"], Number.NaN))) return { label: "5D observed", tone: "good" };
  if (Number.isFinite(numberField(mark, ["return_1d"], Number.NaN))) return { label: "1D observed", tone: "info" };
  return { label: "Waiting <1D", tone: "warn" };
}

function cohortHasMatureEvidence(row: RowRecord): boolean {
  const candidateCount = numberField(row, ["candidate_count"], Number.NaN);
  const raw = recordField(row, "raw");
  const maturity = jsonRecord(raw?.maturity);
  const matureCount = numberFromRecord(maturity, "mature_count");
  if (Number.isFinite(candidateCount) && Number.isFinite(matureCount)) return matureCount >= candidateCount;
  const outcomes = raw?.sample_outcomes;
  if (!Array.isArray(outcomes) || !outcomes.length) return false;
  return outcomes.every((outcome) => {
    if (!outcome || typeof outcome !== "object" || Array.isArray(outcome)) return false;
    const record = outcome as Record<string, JsonValue>;
    const observedHours = typeof record.observation_hours === "number" ? record.observation_hours : Number.NaN;
    if (Number.isFinite(observedHours)) return observedHours >= 20;
    const entryTime = typeof record.entry_time === "string" ? record.entry_time : "";
    const lastObservationTime = typeof record.last_observation_time === "string" ? record.last_observation_time : "";
    return Boolean(entryTime && lastObservationTime && dateMillis(lastObservationTime) - dateMillis(entryTime) >= 20 * 60 * 60 * 1000);
  });
}

function cohortDefinition(row: RowRecord): string {
  const raw = recordField(row, "raw");
  const definition = raw?.cohort_definition;
  return typeof definition === "string" && definition.trim() ? definition.trim() : `${displayField(row, ["cohort_type"])}=${displayField(row, ["cohort_value"])}`;
}

function thesisState(row: RowRecord, requestByEvent: Map<string, RowRecord>): { kind: ThesisFilter; label: string; tone: Tone } {
  if (textField(row, ["thesis_id"])) return { kind: "attached", label: "Attached", tone: "good" };
  const request = requestByEvent.get(textField(row, ["event_id"]));
  if (request) {
    const status = textField(request, ["status"], "requested");
    return { kind: "requested", label: status.toLowerCase() === "open" ? "Requested" : titleLabel(status), tone: toneFromText(status) };
  }
  return { kind: "needs", label: "Needs thesis", tone: "warn" };
}

function focusCandidateRows(rows: RowRecord[], focus: CandidateFocus): RowRecord[] {
  if (focus === "all") return rows;
  const bestByTicker = new Map<string, RowRecord>();
  for (const row of rows) {
    const ticker = textField(row, ["ticker"]);
    const key = ticker || textField(row, ["event_id"]);
    const current = bestByTicker.get(key);
    if (!current || compareCandidates(row, current, "state") < 0) {
      bestByTicker.set(key, row);
    }
  }
  const focused = [...bestByTicker.values()].sort((left, right) => compareCandidates(left, right, "state"));
  if (focus === "top25") return focused.slice(0, 25);
  return focused;
}

function compareCandidates(left: RowRecord, right: RowRecord, sort: CandidateSort): number {
  if (sort === "ticker-asc") return compareText(textField(left, ["ticker"]), textField(right, ["ticker"])) || compareScore(left, right);
  if (sort === "move-asc") return compareNumber(numberField(left, ["required_move_pct"], Number.POSITIVE_INFINITY), numberField(right, ["required_move_pct"], Number.POSITIVE_INFINITY)) || compareScore(left, right);
  if (sort === "premium-asc") return compareNumber(numberField(left, ["premium_mid"], Number.POSITIVE_INFINITY), numberField(right, ["premium_mid"], Number.POSITIVE_INFINITY)) || compareScore(left, right);
  if (sort === "expiry-asc") return compareText(stringFromRecord(recordField(left, "raw"), "expiration"), stringFromRecord(recordField(right, "raw"), "expiration")) || compareScore(left, right);
  if (sort === "state") return stateRank(stateOf(left)) - stateRank(stateOf(right)) || compareScore(left, right);
  return compareScore(left, right);
}

function compareScore(left: RowRecord, right: RowRecord): number {
  return compareNumber(candidateConviction(right), candidateConviction(left));
}

function compareNumber(left: number, right: number): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

function compareText(left: string, right: string): number {
  return left.localeCompare(right);
}

function stateRank(state: string): number {
  if (state === "FIRE") return 0;
  if (state === "SETUP") return 1;
  if (state === "WATCH") return 2;
  return 3;
}

function stateTone(state: string): Tone {
  const normalized = state.toUpperCase();
  if (normalized === "FIRE" || normalized === "HOLD") return "good";
  if (normalized === "SETUP" || normalized === "TRIM") return "warn";
  if (normalized === "INVALIDATED" || normalized === "EXIT") return "bad";
  if (normalized === "WATCH") return "info";
  return "muted";
}

function tierTone(tier: string): Tone {
  if (tier === "Exceptional") return "good";
  if (tier === "Service Bug") return "bad";
  if (tier === "Research") return "info";
  return "muted";
}

function investmentStateLabel(row: RowRecord): string {
  const tier = tierOf(row);
  const primaryState = textField(row, ["primary_state"], "watch").toUpperCase();
  if (isServiceRepair(row)) return "Data Blocked";
  if (tier === "Exceptional") return "Trade Ready";
  if (tier === "Research") return `${titleLabel(primaryState)} Research`;
  if (primaryState === "FIRE") return "Fire Watch";
  if (primaryState === "SETUP") return "Setup Watch";
  return titleLabel(primaryState || tier || "Watch");
}

function investmentStateTone(row: RowRecord): Tone {
  if (isServiceRepair(row)) return "bad";
  const tier = tierOf(row);
  if (tier === "Exceptional") return "good";
  if (tier === "Research") return "info";
  return stateTone(textField(row, ["primary_state"]));
}

function thesisStateTone(state: string): Tone {
  const normalized = state.toLowerCase();
  if (normalized.includes("invalidated")) return "bad";
  if (normalized.includes("validated")) return "good";
  if (normalized.includes("tracking")) return "info";
  if (normalized.includes("weakening")) return "warn";
  if (!normalized) return "muted";
  return "info";
}

function thesisValidationLabel(validation: RowRecord | undefined): string {
  const state = textField(validation, ["state"]);
  if (!state) return "No validation";
  if (state.toLowerCase() === "pending") return "Needs proof";
  return titleLabel(state);
}

function validationStatusLabel(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "hard_risk_triggered") return "Fundamental risk flagged";
  return titleLabel(status);
}

function validationStatusTone(status: string): Tone {
  const normalized = status.toLowerCase();
  if (["supported", "scheduled", "source_confirmed", "clear", "source_backed"].includes(normalized)) return "good";
  if (["partial", "pending", "agent_cited", "source_context_available", "news_only", "agent_only"].includes(normalized)) return "warn";
  if (["breached", "missing", "hard_risk_triggered"].includes(normalized)) return "bad";
  return "muted";
}

function attributionTone(label: string): Tone {
  const normalized = label.toLowerCase();
  if (normalized.includes("good") || normalized.includes("convexity")) return "good";
  if (normalized.includes("crush") || normalized.includes("decay") || normalized.includes("risk")) return "bad";
  if (normalized.includes("bleed") || normalized.includes("spread")) return "warn";
  return "info";
}

function verdictTone(value: string): Tone {
  const normalized = value.toLowerCase();
  if (normalized === "pass" || normalized === "complete") return "good";
  if (normalized === "fail") return "bad";
  if (normalized.includes("collecting") || normalized.includes("pending") || normalized.includes("active")) return "warn";
  return toneFromText(normalized);
}

function toneText(tone: Tone): string {
  if (tone === "good") return "text-green-700 dark:text-green-300";
  if (tone === "warn") return "text-amber-700 dark:text-amber-300";
  if (tone === "bad") return "text-destructive";
  if (tone === "info") return "text-blue-700 dark:text-blue-300";
  return "text-foreground";
}

const reasonLabels: Record<string, string> = {
  "10x_math_inside_cap": "10x target inside cap",
  asymmetry_below_exceptional_bar: "Asymmetry below top bar",
  conviction_below_exceptional_bar: "Conviction below top bar",
  convexity_inside_extreme_bar: "Convexity inside extreme bar",
  delta_in_range: "Delta in range",
  delta_outside_strategy_range: "Delta outside range",
  dte_outside_strategy_range: "DTE outside range",
  entry_quality_below_exceptional_bar: "Entry quality below top bar",
  entry_quality_supported: "Entry quality supported",
  hard_red_team_risk: "Fundamental risk flagged",
  iv_not_overpriced: "IV acceptable",
  iv_percentile_above_fire_threshold: "IV above fire limit",
  iv_percentile_reject: "IV too expensive",
  leap_survivability_not_exceptional: "LEAP survivability weak",
  leap_survivability_supported: "LEAP survivability supported",
  market_regime_hostile_to_long_premium: "Market regime hostile",
  missing_50d_context: "Missing 50D context",
  missing_delta: "Missing delta",
  missing_dte: "Missing DTE",
  missing_iv_percentile: "Missing IV rank",
  missing_open_interest: "Missing open interest",
  missing_rs_vs_qqq: "Missing RS context",
  missing_spread: "Missing spread",
  missing_volume: "Missing volume",
  bank_move_implausible_without_validated_catalyst: "Bank move needs validated catalyst",
  regulated_healthcare_move_implausible_without_validated_catalyst: "Regulated healthcare move needs validated catalyst",
  mega_cap_move_implausible_without_validated_catalyst: "Mega-cap move needs validated catalyst",
  no_printed_volume: "No volume print",
  option_chain_terms_sync_gap: "Option terms sync gap",
  option_contract_quote_sync_gap: "Option quote sync gap",
  option_data_conflict: "Option data conflict",
  option_iv_and_delta_sync_gap: "Option IV/Greek sync gap",
  option_liquidity_sync_gap: "Option liquidity sync gap",
  not_fire_state: "Wait for fire setup",
  open_interest_below_threshold: "Open interest too low",
  open_interest_not_exceptional: "Open interest not exceptional",
  open_interest_supported: "Open interest supported",
  premium_above_buy_under: "Option premium too high",
  premium_inside_buy_under: "Premium inside cap",
  provider_quality_flags_present: "Provider quality flags",
  required_move_too_high: "Required move too high",
  required_move_not_exceptional: "Required move not exceptional",
  rs_vs_qqq_20d_negative: "RS vs QQQ weak",
  rs_vs_qqq_improving: "RS vs QQQ improving",
  market_regime_sync_gap: "Market regime sync gap",
  source_evidence_cluster: "Source evidence cluster",
  source_evidence_sync_gap: "Source evidence sync gap",
  source_backed_thesis: "Source-backed thesis",
  spread_above_fire_threshold: "Spread above fire limit",
  spread_not_exceptional: "Spread not exceptional",
  spread_reject: "Spread too wide",
  spread_usable: "Spread usable",
  theme_ai_applications: "AI applications watch",
  theme_ai_biotech: "AI biotech watch",
  theme_ai_infrastructure: "AI infrastructure watch",
  theme_crypto_infrastructure: "Crypto infrastructure watch",
  theme_robotics_physical_ai: "Robotics / physical AI watch",
  theme_space_tech: "Space tech watch",
  stock_above_50d: "Above 50D",
  stock_below_50d: "Below 50D",
  supportive_market_regime: "Supportive market regime",
  strategy_only_tracks_calls: "Strategy tracks calls only",
  stock_context_sync_gap: "Stock context sync gap",
  thesis_synthesis_sync_gap: "Thesis synthesis sync gap",
  thesis_invalidated: "Thesis invalidated",
  thesis_validated: "Thesis validated",
  volume_below_threshold: "Volume too low",
  volume_seen: "Volume seen",
  wait_for_fire_setup: "Wait for fire setup",
};

function reasonLabel(reason: string): string {
  return reasonLabels[reason] ?? titleLabel(reason);
}

function dataContractStatus(row: RowRecord | undefined): string {
  return textField(row, ["data_contract_status"], "").toLowerCase();
}

function isServiceRepair(row: RowRecord | undefined): boolean {
  return dataContractStatus(row) === "repair_required" || tierOf(row) === "Service Bug";
}

function dataContractFailures(row: RowRecord | undefined): string[] {
  return arrayText(row, "data_contract_failures");
}

function readableReasonSummary(row: RowRecord): string {
  const raw = recordField(row, "raw");
  return [...listFromRecord(raw, "hard_rejects"), ...listFromRecord(raw, "blockers"), ...listFromRecord(raw, "positives")]
    .map(reasonLabel)
    .join(" ");
}

function recordField(row: RowRecord | undefined, key: string): Record<string, JsonValue> | undefined {
  const value = row?.[key];
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, JsonValue>;
  return undefined;
}

function jsonRecord(value: JsonValue | undefined): Record<string, JsonValue> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, JsonValue>;
  return undefined;
}

function jsonArrayField(row: RowRecord | undefined, key: string): JsonValue[] {
  const value = row?.[key];
  if (Array.isArray(value)) return value;
  if (typeof value === "string" && value.trim().startsWith("[")) {
    try {
      const parsed = JSON.parse(value) as JsonValue;
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }
  return [];
}

function arrayText(row: RowRecord | undefined, key: string): string[] {
  return jsonArrayField(row, key).map((item) => typeof item === "string" || typeof item === "number" ? String(item) : "").filter(Boolean);
}

function listFromRecord(record: Record<string, JsonValue> | undefined, key: string): string[] {
  const value = record?.[key];
  if (!Array.isArray(value)) return [];
  return value.map((item) => typeof item === "string" || typeof item === "number" ? String(item) : "").filter(Boolean);
}

function commonBlockers(rows: RowRecord[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const blocker of arrayText(row, "blockers")) {
      counts.set(blocker, (counts.get(blocker) ?? 0) + 1);
    }
  }
  return [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 6);
}

function commonDataContractFailures(rows: RowRecord[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const failure of dataContractFailures(row)) {
      counts.set(failure, (counts.get(failure) ?? 0) + 1);
    }
  }
  return [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 6);
}

function numberFromRecord(record: Record<string, JsonValue> | undefined, key: string): number {
  const value = record?.[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
}

function stringFromRecord(record: Record<string, JsonValue> | undefined, key: string, fallback = ""): string {
  const value = record?.[key];
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

function boolFromRecord(record: Record<string, JsonValue> | undefined, key: string): boolean {
  return record?.[key] === true;
}

function mapBy(items: RowRecord[], key: string): Map<string, RowRecord> {
  const map = new Map<string, RowRecord>();
  for (const item of items) {
    const value = textField(item, [key]);
    if (value) map.set(value, item);
  }
  return map;
}

function latestBy(items: RowRecord[], key: string, dateKey: string): Map<string, RowRecord> {
  const map = new Map<string, RowRecord>();
  for (const item of items) {
    const value = textField(item, [key]);
    if (!value) continue;
    const current = map.get(value);
    if (!current || dateMillis(textField(item, [dateKey])) >= dateMillis(textField(current, [dateKey]))) {
      map.set(value, item);
    }
  }
  return map;
}

function latestValidationBy(items: RowRecord[], key: string): Map<string, RowRecord> {
  const history = validationHistoryBy(items, key);
  const latest = new Map<string, RowRecord>();
  for (const [value, rows] of history.entries()) {
    if (rows[0]) latest.set(value, rows[0]);
  }
  return latest;
}

function validationHistoryBy(items: RowRecord[], key: string): Map<string, RowRecord[]> {
  const history = new Map<string, RowRecord[]>();
  for (const item of items) {
    const value = textField(item, [key]);
    if (!value) continue;
    const rows = history.get(value) ?? [];
    rows.push(item);
    history.set(value, rows);
  }
  for (const rows of history.values()) {
    rows.sort((left, right) => validationMillis(right) - validationMillis(left));
  }
  return history;
}

function oldestDate(items: RowRecord[], key: string): string {
  let oldest = "";
  for (const item of items) {
    const value = textField(item, [key]);
    if (value && (!oldest || dateMillis(value) < dateMillis(oldest))) oldest = value;
  }
  return oldest;
}

