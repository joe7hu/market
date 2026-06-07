import { Activity, AlertTriangle, ArrowDownUp, BrainCircuit, CheckCircle2, ChevronLeft, ChevronRight, GitBranchPlus, Loader2, Search, Target, TrendingUp } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { promoteStrategyMutation } from "@/api";
import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import type { JsonValue, PanelData, RowRecord, TablePayload } from "@/types";
import type { Tone } from "@/ui/tone";
import { displayField, formatMoney, fullField, listField, numberField, textField, titleLabel, toneFromText } from "./rowFormat";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};

export function OptionsRadarPage({ data, onOpenTicker, onRefresh }: OptionsRadarPageProps) {
  const [promotingProposal, setPromotingProposal] = useState<string | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const candidates = rows(data.candidateEvent);
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
  const optionSnapshots = rows(data.optionSnapshot);
  const optionFeatures = rows(data.optionFeatures);
  const stockFeatures = rows(data.stockFeatures);
  const strategyVersions = rows(data.optionStrategyVersions);
  const radarSummary = rows(data.optionRadarSummary)[0];
  const openThesisRequests = useMemo(() => thesisRequests.filter((row) => textField(row, ["status"], "open").toLowerCase() === "open"), [thesisRequests]);
  const actionableThesisRequests = useMemo(() => thesisRequests.filter((row) => {
    const status = textField(row, ["status"], "open").toLowerCase();
    return status === "open" || status.includes("failed");
  }), [thesisRequests]);
  const latestCandidateTime = textField(radarSummary, ["latest_candidate_time"]);

  const opportunityCandidates = useMemo(
    () => candidates.filter((row) => isOpportunityCandidate(row) && (!latestCandidateTime || textField(row, ["snapshot_time"]) === latestCandidateTime)),
    [candidates, latestCandidateTime],
  );
  const opportunityTickers = useMemo(() => uniqueText(opportunityCandidates, "ticker"), [opportunityCandidates]);
  const scannedTickers = useMemo(() => uniqueText(optionSnapshots, "ticker"), [optionSnapshots]);

  const latestBacktestByProposal = useMemo(() => latestBy(backtests, "proposal_id", "evaluated_at"), [backtests]);
  const latestForwardByProposal = useMemo(() => latestBy(forwardTests, "proposal_id", "evaluated_at"), [forwardTests]);
  const eventById = useMemo(() => mapBy(candidates, "event_id"), [candidates]);
  const latestCandidateMarkByEvent = useMemo(() => latestBy(candidateMarks, "event_id", "mark_time"), [candidateMarks]);
  const latestCandidateAttributionByEvent = useMemo(() => latestBy(candidateAttributions, "event_id", "snapshot_time"), [candidateAttributions]);
  const thesisRequestByEvent = useMemo(() => mapBy(openThesisRequests, "event_id"), [openThesisRequests]);

  const opportunityCount = numberField(radarSummary, ["opportunity_rows_current"], opportunityCandidates.length);
  const opportunityTickerCount = numberField(radarSummary, ["opportunity_tickers_current"], opportunityTickers.length);
  const scannedTickerCount = numberField(radarSummary, ["scanned_tickers_current"], scannedTickers.length);
  const fireCount = numberField(radarSummary, ["fire_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "FIRE"));
  const setupCount = numberField(radarSummary, ["setup_rows_current"], countWhere(opportunityCandidates, (row) => stateOf(row) === "SETUP"));
  const exceptionalCount = numberField(radarSummary, ["exceptional_opportunities_current"], countWhere(opportunityRows, (row) => tierOf(row) === "Exceptional"));
  const researchCount = numberField(radarSummary, ["research_opportunities_current"], countWhere(opportunityRows, (row) => tierOf(row) === "Research"));

  const latestSnapshot = textField(radarSummary, ["latest_snapshot_time"], latestDate(optionSnapshots, "snapshot_time"));
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

  return (
    <WorkspacePage
      eyebrow="Options Radar"
      title="10x Options Radar"
      subtitle="Daily candidate state, shadow outcomes, thesis validation, and strategy gate results."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={latestSnapshot ? "good" : "muted"}>{latestSnapshot ? `Snapshot ${formatDate(latestSnapshot)}` : "No snapshots"}</StatusBadge>
          <StatusBadge tone="info">{displayField(latestStrategy, ["strategy_version", "strategy_name"], "No strategy")}</StatusBadge>
        </div>
      }
    >
      <RadarSummaryStrip
        opportunityCount={opportunityCount}
        opportunityTickerCount={opportunityTickerCount}
        scannedTickerCount={scannedTickerCount}
        fireCount={fireCount}
        setupCount={setupCount}
        exceptionalCount={exceptionalCount}
        researchCount={researchCount}
      />
      <StrategyExplainer strategy={latestStrategy} />
      <Tabs defaultValue="radar" className="min-w-0">
        <TabsList className="h-auto max-w-full flex-wrap justify-start">
          <TabsTrigger value="radar">Opportunities</TabsTrigger>
          <TabsTrigger value="learning">Learning</TabsTrigger>
          <TabsTrigger value="theses">Theses</TabsTrigger>
        </TabsList>

        <TabsContent value="radar" className="space-y-4">
          <ExtremeOpportunityDesk
            rows={opportunityRows}
            candidates={opportunityCandidates}
            latestMarkByEvent={latestCandidateMarkByEvent}
            latestAttributionByEvent={latestCandidateAttributionByEvent}
            onOpenTicker={onOpenTicker}
          />
        </TabsContent>

        <TabsContent value="learning" className="space-y-4">
          <LearningProgressPanel
            opportunities={opportunityCandidates}
            latestMarkByEvent={latestCandidateMarkByEvent}
            latestAttributionByEvent={latestCandidateAttributionByEvent}
            cohorts={cohortResults}
            proposals={proposals}
            missedWinners={missedWinners}
            postmortemRequests={postmortemRequests}
            postmortems={postmortems}
          />
          <CandidateEventsTable
            rows={opportunityCandidates}
            thesisRequestByEvent={thesisRequestByEvent}
            latestMarkByEvent={latestCandidateMarkByEvent}
            latestAttributionByEvent={latestCandidateAttributionByEvent}
            onOpenTicker={onOpenTicker}
          />
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
        </TabsContent>

        <TabsContent value="theses" className="space-y-4">
          <ThesisPipelinePanel requests={thesisRequests} theses={agentTheses} validations={thesisValidations} />
          <ThesisRequestsTable rows={actionableThesisRequests} eventById={eventById} onOpenTicker={onOpenTicker} title="Actionable Thesis Queue" />
          <AgentThesisBrowser theses={agentTheses} validations={thesisValidations} onOpenTicker={onOpenTicker} />
        </TabsContent>
      </Tabs>
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
}: {
  opportunityCount: number;
  opportunityTickerCount: number;
  scannedTickerCount: number;
  fireCount: number;
  setupCount: number;
  exceptionalCount: number;
  researchCount: number;
}) {
  const items: Array<[string, string, Tone]> = [
    ["Exceptional", exceptionalCount.toLocaleString(), exceptionalCount ? "good" : "muted"],
    ["Research", researchCount.toLocaleString(), researchCount ? "info" : "muted"],
    ["Opportunities", `${opportunityCount.toLocaleString()} rows / ${opportunityTickerCount.toLocaleString()} tickers`, opportunityCount ? "good" : "muted"],
    ["Fire", fireCount.toLocaleString(), fireCount ? "good" : "muted"],
    ["Setup", setupCount.toLocaleString(), setupCount ? "warn" : "muted"],
    ["Scanned", scannedTickerCount.toLocaleString(), scannedTickerCount >= 20 ? "good" : scannedTickerCount ? "warn" : "muted"],
  ];
  return (
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
      {items.map(([label, value, tone]) => (
        <div key={label} className="rounded-md border border-border bg-card px-3 py-2">
          <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
          <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
        </div>
      ))}
    </div>
  );
}

function ExtremeOpportunityDesk({
  rows,
  candidates,
  latestMarkByEvent,
  latestAttributionByEvent,
  onOpenTicker,
}: {
  rows: RowRecord[];
  candidates: RowRecord[];
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  onOpenTicker: OpenTicker;
}) {
  const exceptionalRows = useMemo(() => rows.filter((row) => tierOf(row) === "Exceptional"), [rows]);
  const researchRows = useMemo(() => rows.filter((row) => tierOf(row) === "Research"), [rows]);
  const visibleRows = exceptionalRows.length ? exceptionalRows : researchRows.slice(0, 8);
  const blockerSummary = useMemo(() => commonBlockers(rows), [rows]);

  if (!rows.length) {
    return (
      <EmptyState
        title="No opportunity read model"
        detail={candidates.length ? "Candidate events exist, but the extreme opportunity layer has not been refreshed yet." : "No current options radar candidates are stored yet."}
        icon={Target}
      />
    );
  }

  return (
    <DataTableFrame
      title={<SectionTitle title={exceptionalRows.length ? "Exceptional Setups" : "No Exceptional Setup"} count={visibleRows.length} />}
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{exceptionalRows.length.toLocaleString()} exceptional</span>
          <span>{researchRows.length.toLocaleString()} research</span>
          <span>{rows.length.toLocaleString()} grouped tickers</span>
        </div>
      }
    >
      {!exceptionalRows.length ? (
        <div className="border-b border-border px-3 py-3">
          <div className="flex flex-wrap gap-1.5">
            {blockerSummary.length ? blockerSummary.map(([reason, count]) => (
              <span key={reason} className="rounded-md border border-border bg-muted px-2 py-1 text-xs font-medium text-muted-foreground">
                {reasonLabel(reason)} {count}
              </span>
            )) : <StatusBadge tone="muted">No blocker summary</StatusBadge>}
          </div>
        </div>
      ) : null}
      <div className="space-y-3 p-3 lg:hidden">
        {visibleRows.map((row) => (
          <OpportunityMobileCard
            key={textField(row, ["opportunity_id"], `${textField(row, ["ticker"])}-${textField(row, ["primary_contract_id"])}`)}
            row={row}
            latestMarkByEvent={latestMarkByEvent}
            latestAttributionByEvent={latestAttributionByEvent}
            onOpenTicker={onOpenTicker}
          />
        ))}
      </div>
      <table className="hidden w-full min-w-[1320px] text-sm lg:table">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Tier</Head>
            <Head>Primary Contract</Head>
            <Head className="text-right">Buy Under</Head>
            <Head className="text-right">10x Move</Head>
            <Head>Scores</Head>
            <Head>Blocking Checks</Head>
            <Head>Shadow P/L</Head>
            <Head>Next Action</Head>
            <Head>Kill Switch</Head>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => {
            const eventId = textField(row, ["primary_event_id"]);
            const mark = latestMarkByEvent.get(eventId);
            const attribution = latestAttributionByEvent.get(eventId);
            const tier = tierOf(row);
            const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
            const qualityFlags = arrayText(row, "quality_flags");
            return (
              <tr
                key={textField(row, ["opportunity_id"], `${textField(row, ["ticker"])}-${textField(row, ["primary_contract_id"])}`)}
                className={cn(
                  "border-b border-border align-top transition-colors hover:bg-accent/40",
                  tier === "Exceptional" && "bg-green-500/5",
                  qualityStatus === "bad" && "bg-destructive/5",
                  qualityStatus === "caution" && "bg-amber-500/5",
                )}
              >
                <Cell>
                  <TickerButton ticker={textField(row, ["ticker"])} onOpenTicker={onOpenTicker} />
                </Cell>
                <Cell>
                  <div className="flex items-center gap-2">
                    <StatusBadge tone={tierTone(tier)}>{tier}</StatusBadge>
                    <QualityIndicator status={qualityStatus} flags={qualityFlags} />
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">{displayField(row, ["position_sizing_band"], "no position")}</div>
                </Cell>
                <Cell className="max-w-[260px]">
                  <Truncated>{displayField(row, ["primary_contract_id"])}</Truncated>
                  <AlternativeContracts row={row} />
                </Cell>
                <Cell className="text-right tabular-nums">
                  <div>{moneyField(row, ["buy_under"])}</div>
                  <div className="text-xs text-muted-foreground">{displayField(row, ["entry_zone"], "wait")}</div>
                </Cell>
                <Cell className="text-right tabular-nums">
                  <div>{formatRatio(numberField(row, ["required_move_pct"], Number.NaN))}</div>
                  <div className="text-xs text-muted-foreground">{moneyField(row, ["required_10x_price"])}</div>
                </Cell>
                <Cell><OpportunityScoreStack row={row} /></Cell>
                <Cell className="max-w-[260px]"><OpportunityReasonChips row={row} /></Cell>
                <Cell className="max-w-[230px]"><OpportunityOutcome mark={mark} attribution={attribution} /></Cell>
                <Cell className="max-w-[320px]"><Truncated>{opportunityActionText(row)}</Truncated></Cell>
                <Cell className="max-w-[340px] text-muted-foreground"><Truncated>{displayField(row, ["kill_switch"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function OpportunityMobileCard({
  row,
  latestMarkByEvent,
  latestAttributionByEvent,
  onOpenTicker,
}: {
  row: RowRecord;
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  onOpenTicker: OpenTicker;
}) {
  const eventId = textField(row, ["primary_event_id"]);
  const mark = latestMarkByEvent.get(eventId);
  const attribution = latestAttributionByEvent.get(eventId);
  const tier = tierOf(row);
  const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
  const qualityFlags = arrayText(row, "quality_flags");
  return (
    <article className={cn("rounded-md border border-border bg-card p-3", tier === "Exceptional" && "border-green-500/40 bg-green-500/5")}>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <TickerButton ticker={textField(row, ["ticker"])} onOpenTicker={onOpenTicker} />
          <div className="mt-1 truncate text-xs text-muted-foreground">{displayField(row, ["primary_contract_id"])}</div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <StatusBadge tone={tierTone(tier)}>{tier}</StatusBadge>
          <QualityIndicator status={qualityStatus} flags={qualityFlags} />
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <InlineMetric label="Buy Under" value={moneyField(row, ["buy_under"])} />
        <InlineMetric label="10x Move" value={formatRatio(numberField(row, ["required_move_pct"], Number.NaN))} />
        <InlineMetric label="Score" value={formatScore(numberField(row, ["conviction_score"], Number.NaN))} />
      </div>
      <div className="mt-3 space-y-2">
        <MobileSection label="Next Action">{opportunityActionText(row)}</MobileSection>
        <MobileSection label="Blocking Checks"><OpportunityReasonChips row={row} /></MobileSection>
        <MobileSection label="Shadow P/L"><OpportunityOutcome mark={mark} attribution={attribution} /></MobileSection>
        <MobileSection label="Kill Switch">{displayField(row, ["kill_switch"])}</MobileSection>
      </div>
    </article>
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

function OpportunityScoreStack({ row }: { row: RowRecord }) {
  return (
    <div className="grid min-w-52 grid-cols-2 gap-1.5">
      <MetricPill label="Conviction" value={formatScore(numberField(row, ["conviction_score"], Number.NaN))} />
      <MetricPill label="Asymmetry" value={formatScore(numberField(row, ["asymmetry_score"], Number.NaN))} />
      <MetricPill label="Entry" value={formatScore(numberField(row, ["entry_quality_score"], Number.NaN))} />
      <MetricPill label="Evidence" value={formatScore(numberField(row, ["evidence_score"], Number.NaN))} />
    </div>
  );
}

function OpportunityReasonChips({ row }: { row: RowRecord }) {
  const blockers = arrayText(row, "blockers");
  const reasons = blockers.length ? blockers.slice(0, 4) : arrayText(row, "top_reasons").slice(0, 4);
  const extra = (blockers.length ? blockers.length : arrayText(row, "top_reasons").length) - reasons.length;
  return (
    <div className="flex flex-wrap gap-1.5">
      {reasons.length ? reasons.map((reason) => (
        <ReasonChip key={reason} reason={reason} tone={blockers.length ? "warn" : "good"} />
      )) : <StatusBadge tone="good">Strict gates passed</StatusBadge>}
      {extra > 0 ? <span className="rounded-md border border-border px-2 py-0.5 text-xs text-muted-foreground">+{extra}</span> : null}
    </div>
  );
}

function opportunityActionText(row: RowRecord): string {
  const tier = tierOf(row);
  const blockers = arrayText(row, "blockers");
  const reasons = arrayText(row, "top_reasons");
  if (tier === "Exceptional" && !blockers.length) {
    const why = reasons.slice(0, 3).map(reasonLabel).join(", ");
    return why ? `Review now: ${why}.` : "Review now: strict gates passed.";
  }
  if (blockers.length) {
    const why = blockers.slice(0, 3).map(reasonLabel).join(", ");
    return `Wait: ${why}.`;
  }
  return displayField(row, ["why_now"], "Review setup details.");
}

function AlternativeContracts({ row }: { row: RowRecord }) {
  const alternatives = jsonArrayField(row, "alternative_contracts");
  if (!alternatives.length) return <div className="mt-1 text-xs text-muted-foreground">No alternatives</div>;
  return <div className="mt-1 text-xs text-muted-foreground">{alternatives.length} alternative{alternatives.length === 1 ? "" : "s"}</div>;
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
            A shadow-only LEAP call screen looking for contracts where a large underlying move could make intrinsic value roughly 10x the option mid. Candidate rank is deterministic: FIRE beats SETUP beats WATCH, then score favors lower required stock move, stronger liquidity, better convexity, trend, and relative strength. Agents only receive the current top-ranked queue, and promotion requires deterministic backtest, forward shadow test, and human approval.
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

type CandidateSort = "score-desc" | "ticker-asc" | "move-asc" | "premium-asc" | "expiry-asc" | "state";
type CandidateStateFilter = "all" | "FIRE" | "SETUP" | "WATCH";
type CandidateFocus = "top25" | "top-per-ticker" | "all";
type ThesisFilter = "all" | "needs" | "requested" | "attached";
type QualityFilter = "all" | "ok" | "caution" | "bad";

const CANDIDATE_PAGE_SIZE = 50;

function CandidateEventsTable({
  rows,
  thesisRequestByEvent,
  latestMarkByEvent,
  latestAttributionByEvent,
  onOpenTicker,
}: {
  rows: RowRecord[];
  thesisRequestByEvent: Map<string, RowRecord>;
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  onOpenTicker: OpenTicker;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<CandidateStateFilter>("all");
  const [thesisFilter, setThesisFilter] = useState<ThesisFilter>("all");
  const [qualityFilter, setQualityFilter] = useState<QualityFilter>("all");
  const [focus, setFocus] = useState<CandidateFocus>("top25");
  const [sort, setSort] = useState<CandidateSort>("state");
  const [page, setPage] = useState(0);

  const filteredRows = useMemo(() => {
    const normalizedQuery = query.trim().toUpperCase();
    return rows
      .filter((row) => {
        if (stateFilter !== "all" && stateOf(row) !== stateFilter) return false;
        if (qualityFilter !== "all" && qualityOf(row) !== qualityFilter) return false;
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
  }, [query, qualityFilter, rows, stateFilter, thesisFilter, thesisRequestByEvent]);

  const focusedRows = useMemo(
    () => focusCandidateRows(filteredRows, focus).sort((left, right) => compareCandidates(left, right, sort)),
    [filteredRows, focus, sort],
  );

  useEffect(() => {
    setPage(0);
  }, [focus, query, qualityFilter, sort, stateFilter, thesisFilter]);

  const pageCount = Math.max(1, Math.ceil(focusedRows.length / CANDIDATE_PAGE_SIZE));
  const boundedPage = Math.min(page, pageCount - 1);
  const visibleRows = focusedRows.slice(boundedPage * CANDIDATE_PAGE_SIZE, (boundedPage + 1) * CANDIDATE_PAGE_SIZE);
  const tickerCount = uniqueText(focusedRows, "ticker").length;

  if (!rows.length) {
    return <EmptyState title="No candidate events" detail="No options radar candidates are stored yet." icon={Target} />;
  }

  return (
    <DataTableFrame
      title={<SectionTitle title="Ranked Candidate Events" count={focusedRows.length} />}
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{tickerCount.toLocaleString()} tickers</span>
          <span>{filteredRows.length.toLocaleString()} matched</span>
          <span>{rows.length.toLocaleString()} loaded</span>
        </div>
      }
    >
      <div className="border-b border-border p-3">
        <div className="grid gap-2 lg:grid-cols-[minmax(220px,1fr)_155px_150px_160px_160px_190px_auto]">
          <div className="relative min-w-0">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker, contract, or reason" aria-label="Filter candidate events" />
          </div>
          <Select value={focus} onValueChange={(value) => setFocus(value as CandidateFocus)}>
            <SelectTrigger aria-label="Candidate focus"><SelectValue placeholder="Focus" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="top25">Top 25</SelectItem>
              <SelectItem value="top-per-ticker">Top per ticker</SelectItem>
              <SelectItem value="all">All contracts</SelectItem>
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
              <SelectItem value="needs">Needs thesis</SelectItem>
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
          <Select value={sort} onValueChange={(value) => setSort(value as CandidateSort)}>
            <SelectTrigger aria-label="Sort candidates"><SelectValue placeholder="Sort" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="score-desc">Score high to low</SelectItem>
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
            setFocus("top25");
            setSort("state");
          }}>
            <ArrowDownUp className="size-4" />
            <span>Reset</span>
          </Button>
        </div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          Default view shows the strongest ranked contracts, not every contract row. Use all contracts only when auditing the full scan.
        </p>
      </div>
      <table className="w-full min-w-[1240px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>State</Head>
            <Head>Contract</Head>
            <Head className="text-right"><HelpLabel label="Option Mid" detail="Current option midpoint from the stored chain snapshot. Est fill adds the strategy slippage assumption used by shadow entries." /></Head>
            <Head className="text-right"><HelpLabel label="Underlying For 10x" detail="Underlying stock price where intrinsic value is about ten times the option mid. Fill target uses the estimated fill premium when it differs. Cap room is how far the estimated fill is below the strategy premium ceiling." /></Head>
            <Head className="text-right">Stock Move</Head>
            <Head className="text-right">Score</Head>
            <Head>Thesis</Head>
            <Head>Outcome</Head>
            <Head>Why / Blockers</Head>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => {
            const ticker = textField(row, ["ticker"]);
            const state = stateOf(row);
            const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
            const qualityFlags = listField(row, ["quality_flags"]);
            const thesis = thesisState(row, thesisRequestByEvent);
            const eventId = textField(row, ["event_id"]);
            const mark = latestMarkByEvent.get(eventId);
            const attribution = latestAttributionByEvent.get(eventId);
            return (
              <tr
                key={textField(row, ["event_id"], `${ticker}-${textField(row, ["contract_id"])}`)}
                className={cn(
                  "border-b border-border align-top transition-colors hover:bg-accent/40",
                  qualityStatus === "bad" && "bg-destructive/5",
                  qualityStatus === "caution" && "bg-amber-500/5",
                )}
              >
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell>
                  <div className="flex items-center gap-2">
                    <StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge>
                    <QualityIndicator status={qualityStatus} flags={qualityFlags} />
                  </div>
                </Cell>
                <Cell className="max-w-[260px]"><Truncated>{displayField(row, ["contract_id"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">
                  <div>{moneyField(row, ["premium_mid"])}</div>
                  <div className="text-xs text-muted-foreground">fill {moneyField(row, ["premium_fill_assumption"])}</div>
                </Cell>
                <Cell className="text-right tabular-nums">
                  <div>{moneyField(row, ["required_10x_price"])}</div>
                  <FillTarget row={row} />
                  <PremiumCapHint row={row} />
                </Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["required_move_pct"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["score"], Number.NaN))}</Cell>
                <Cell><StatusBadge tone={thesis.tone}>{thesis.label}</StatusBadge></Cell>
                <Cell className="max-w-[230px]"><OpportunityOutcome mark={mark} attribution={attribution} /></Cell>
                <Cell className="max-w-[430px]"><ReasonChips row={row} /></Cell>
              </tr>
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
    <span className={cn("rounded-md border px-2 py-0.5 text-xs font-medium", tone === "good" ? "border-green-500/30 bg-green-50/30 text-foreground" : "border-amber-500/30 bg-amber-50/40 text-foreground")} title={reason}>
      {reasonLabel(reason)}
    </span>
  );
}

function CandidateEventMarksTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No candidate marks" detail="No point-in-time validation marks are stored for candidate events." icon={Activity} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Candidate Outcome Marks" count={rows.length} />}>
      <table className="w-full min-w-[1240px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Candidate</Head>
            <Head>Alert</Head>
            <Head>Mark</Head>
            <Head className="text-right">Current</Head>
            <Head className="text-right">1D</Head>
            <Head className="text-right">5D</Head>
            <Head className="text-right">20D</Head>
            <Head className="text-right">60D</Head>
            <Head className="text-right">Max</Head>
            <Head className="text-right">Drawdown</Head>
            <Head>Hit Times</Head>
            <Head className="text-right">IV</Head>
            <Head className="text-right">Spread</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"], contractTicker(textField(row, ["contract_id"])));
            const state = textField(row, ["candidate_state"]);
            return (
              <tr key={textField(row, ["mark_id"], `${ticker}-${textField(row, ["mark_time"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["alert_time"]))}</Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["mark_time"]))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["current_return"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_1d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_5d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_20d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_60d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatMultiple(numberField(row, ["max_return_since_alert"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["max_drawdown_since_alert"], Number.NaN))}</Cell>
                <Cell><HitTimes row={row} /></Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["iv"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["spread_pct"], Number.NaN))}</Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function CandidateEventAttributionsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No candidate attribution" detail="No candidate events have multiple point-in-time marks yet." icon={Activity} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Candidate Event Attribution" count={rows.length} />}>
      <table className="w-full min-w-[1260px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Candidate</Head>
            <Head>Window</Head>
            <Head>Label</Head>
            <Head className="text-right">Option</Head>
            <Head className="text-right">Underlying</Head>
            <Head className="text-right">IV</Head>
            <Head className="text-right">Theta</Head>
            <Head className="text-right">Spread</Head>
            <Head className="text-right">Unexplained</Head>
            <Head>Contract</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"], contractTicker(textField(row, ["contract_id"])));
            const state = textField(row, ["candidate_state"]);
            return (
              <tr key={textField(row, ["attribution_id"], `${ticker}-${textField(row, ["snapshot_time"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">
                  {formatDate(textField(row, ["prior_snapshot_time"]))} {"->"} {formatDate(textField(row, ["snapshot_time"]))}
                </Cell>
                <Cell><AttributionBadge row={row} /></Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["option_return"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["underlying_return"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["iv_change"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["theta_effect"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["spread_change"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["unexplained_effect"], Number.NaN))}</Cell>
                <Cell className="max-w-[260px]"><Truncated>{displayField(row, ["contract_id"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function RadarStateTransitionsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No radar state transitions" detail="No deterministic candidate, hold, trim, exit, or invalidation transitions are stored." icon={Activity} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Radar State Transitions" count={rows.length} />}>
      <table className="w-full min-w-[1180px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>State</Head>
            <Head>Previous</Head>
            <Head>Candidate</Head>
            <Head>Snapshot</Head>
            <Head>Contract</Head>
            <Head>Evidence</Head>
            <Head>Trigger</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"], contractTicker(textField(row, ["contract_id"])));
            const state = stateOf(row);
            const previous = textField(row, ["previous_state"]);
            return (
              <tr key={textField(row, ["transition_id"], `${ticker}-${textField(row, ["snapshot_time"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge></Cell>
                <Cell>{previous ? <StatusBadge tone={stateTone(previous)}>{titleLabel(previous)}</StatusBadge> : <span className="text-muted-foreground">Initial</span>}</Cell>
                <Cell><StatusBadge tone={stateTone(textField(row, ["candidate_state"]))}>{titleLabel(displayField(row, ["candidate_state"], "pending"))}</StatusBadge></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["snapshot_time"]))}</Cell>
                <Cell className="max-w-[250px]"><Truncated>{displayField(row, ["contract_id"])}</Truncated></Cell>
                <Cell className="max-w-[220px]"><Truncated>{transitionEvidence(row)}</Truncated></Cell>
                <Cell className="max-w-[360px]"><Truncated>{displayField(row, ["trigger_reason"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function ShadowTradesTable({
  rows,
  eventById,
  latestAttributionByEvent,
  onOpenTicker,
}: {
  rows: RowRecord[];
  eventById: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  onOpenTicker: OpenTicker;
}) {
  if (!rows.length) {
    return <EmptyState title="No shadow trades" detail="No shadow entries have been created from candidate events." icon={Activity} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Shadow Trades and Attribution" count={rows.length} />}>
      <table className="w-full min-w-[1080px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head className="text-right">Entry</Head>
            <Head className="text-right">Max Return</Head>
            <Head className="text-right">Max Drawdown</Head>
            <Head>Hit Times</Head>
            <Head>Latest Attribution</Head>
            <Head>Exit</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const eventId = textField(row, ["event_id"]);
            const event = eventById.get(eventId);
            const attribution = latestAttributionByEvent.get(eventId);
            const ticker = textField(event, ["ticker"], contractTicker(textField(attribution, ["contract_id"], textField(row, ["contract_id"]))));
            const status = textField(row, ["status"], "open");
            return (
              <tr key={textField(row, ["trade_id"], eventId)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["entry_price_assumption"])}</Cell>
                <Cell className="text-right tabular-nums">{formatMultiple(numberField(row, ["max_return_seen"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["max_drawdown_seen"], Number.NaN))}</Cell>
                <Cell><HitTimes row={row} /></Cell>
                <Cell className="max-w-[300px]">
                  {attribution ? <AttributionBadge row={attribution} /> : <span className="text-muted-foreground">No mark</span>}
                </Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["exit_reason"], displayField(row, ["exit_time"]))}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function ShadowTradeMarksTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No shadow marks" detail="No point-in-time validation marks are stored for shadow trades." icon={Activity} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Daily Shadow Validation Marks" count={rows.length} />}>
      <table className="w-full min-w-[1240px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Mark</Head>
            <Head className="text-right">Current</Head>
            <Head className="text-right">1D</Head>
            <Head className="text-right">5D</Head>
            <Head className="text-right">20D</Head>
            <Head className="text-right">60D</Head>
            <Head className="text-right">Max</Head>
            <Head className="text-right">Drawdown</Head>
            <Head className="text-right">IV</Head>
            <Head className="text-right">Spread</Head>
            <Head className="text-right">Worthless Proxy</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"], contractTicker(textField(row, ["contract_id"])));
            return (
              <tr key={textField(row, ["mark_id"], `${ticker}-${textField(row, ["mark_time"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["mark_time"]))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["current_return"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_1d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_5d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_20d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["return_60d"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatMultiple(numberField(row, ["max_return_since_alert"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["max_drawdown_since_alert"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["iv"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["spread_pct"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["expired_worthless_probability_change"], Number.NaN))}</Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
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

  return (
    <DataTableFrame title={<SectionTitle title="Cohort Learning" count={rows.length} />}>
      <table className="w-full min-w-[1020px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Cohort</Head>
            <Head className="text-right">Candidates</Head>
            <Head>Readiness</Head>
            <Head className="text-right">2x</Head>
            <Head className="text-right">5x</Head>
            <Head className="text-right">10x</Head>
            <Head className="text-right">Median Max</Head>
            <Head className="text-right">Median DD</Head>
            <Head>What It Means</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const key = textField(row, ["cohort_id"], `${textField(row, ["cohort_type"])}-${textField(row, ["cohort_value"])}`);
            const mature = cohortHasMatureEvidence(row);
            return (
              <tr key={key} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell className="max-w-[260px]">
                  <div className="min-w-0">
                    <div className="truncate font-medium">{titleLabel(displayField(row, ["cohort_value"]))}</div>
                    <div className="truncate text-xs text-muted-foreground">{displayField(row, ["cohort_type"])}</div>
                  </div>
                </Cell>
                <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["candidate_count"], Number.NaN), 0)}</Cell>
                <Cell><StatusBadge tone={mature ? "good" : "warn"}>{mature ? "Readable" : "Collecting"}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{mature ? formatRatio(numberField(row, ["hit_rate_2x"], Number.NaN)) : "-"}</Cell>
                <Cell className="text-right tabular-nums">{mature ? formatRatio(numberField(row, ["hit_rate_5x"], Number.NaN)) : "-"}</Cell>
                <Cell className="text-right tabular-nums">{mature ? formatRatio(numberField(row, ["hit_rate_10x"], Number.NaN)) : "-"}</Cell>
                <Cell className="text-right tabular-nums">{mature ? formatMultiple(numberField(row, ["median_max_return"], Number.NaN)) : "-"}</Cell>
                <Cell className="text-right tabular-nums">{mature ? formatSignedRatio(numberField(row, ["median_max_drawdown"], Number.NaN)) : "-"}</Cell>
                <Cell className="max-w-[360px]"><Truncated>{mature ? cohortDefinition(row) : "Outcome window is still pending; do not treat zero hit rates as failure."}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function PostmortemRequestsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No postmortem requests" detail="No important outcomes are queued for agent postmortem." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Agent Postmortem Queue" count={rows.length} />}>
      <table className="w-full min-w-[1040px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head>Outcome</Head>
            <Head className="text-right">Priority</Head>
            <Head>Strategy</Head>
            <Head>Created</Head>
            <Head>Prompt</Head>
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
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["strategy_version"])}</Truncated></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                <Cell className="max-w-[380px]"><Truncated>{displayField(row, ["prompt"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function PostmortemsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No postmortems" detail="No structured agent postmortems are stored." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Structured Postmortems" count={rows.length} />}>
      <table className="w-full min-w-[1180px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Outcome</Head>
            <Head>Failure</Head>
            <Head className="text-right">Confidence</Head>
            <Head>Rule Change</Head>
            <Head>Expected Effect</Head>
            <Head>Risk</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            return (
              <tr key={textField(row, ["postmortem_id"], `${ticker}-${textField(row, ["source_id"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["outcome_type"])}</Truncated></Cell>
                <Cell className="max-w-[240px]"><Truncated>{displayField(row, ["failure_type"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["confidence"], Number.NaN))}</Cell>
                <Cell className="max-w-[320px]"><Truncated>{displayField(row, ["proposed_rule_change"])}</Truncated></Cell>
                <Cell className="max-w-[300px]"><Truncated>{displayField(row, ["expected_effect"])}</Truncated></Cell>
                <Cell className="max-w-[300px]"><Truncated>{displayField(row, ["risk"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
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
    <DataTableFrame
      title={
        <div className="flex flex-wrap items-center gap-2">
          <SectionTitle title="Strategy Mutation Gates" count={rows.length} />
          {promotionError ? <StatusBadge tone="bad">{promotionError}</StatusBadge> : null}
        </div>
      }
    >
      <table className="w-full min-w-[1260px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Proposal</Head>
            <Head>Status</Head>
            <Head>Backtest</Head>
            <Head>Forward</Head>
            <Head>Human</Head>
            <Head>Change</Head>
            <Head>Rationale</Head>
            <Head>Risk</Head>
            <Head className="text-right">Action</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const proposalId = textField(row, ["proposal_id"]);
            const backtest = backtestByProposal.get(proposalId);
            const forward = forwardByProposal.get(proposalId);
            const status = textField(row, ["status"], "pending");
            const human = textField(row, ["human_approval_status"], "pending");
            const backtestVerdict = textField(backtest, ["verdict"]).toLowerCase();
            const forwardVerdict = textField(forward, ["verdict", "status"]).toLowerCase();
            const isPromoting = promotingProposal === proposalId;
            const approvedBy = textField(row, ["approved_by"]);
            const approvedAt = textField(row, ["approved_at"]);
            const canPromote =
              Boolean(proposalId) &&
              status === "ready_for_human_review" &&
              human !== "approved" &&
              backtestVerdict === "pass" &&
              forwardVerdict === "pass";
            return (
              <tr key={proposalId || textField(row, ["proposed_strategy_version"])} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell className="max-w-[240px]"><Truncated>{displayField(row, ["proposed_strategy_version"])}</Truncated></Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell><VerdictBadge row={backtest} keys={["verdict"]} /></Cell>
                <Cell><VerdictBadge row={forward} keys={["verdict", "status"]} /></Cell>
                <Cell>
                  <div className="min-w-0">
                    <StatusBadge tone={human === "approved" ? "good" : human === "rejected" ? "bad" : "warn"}>{titleLabel(human)}</StatusBadge>
                    {approvedBy || approvedAt ? (
                      <div className="mt-1 max-w-[180px] truncate text-xs text-muted-foreground">
                        {approvedBy ? `by ${approvedBy}` : "approved"}{approvedAt ? ` ${formatDate(approvedAt)}` : ""}
                      </div>
                    ) : null}
                  </div>
                </Cell>
                <Cell className="max-w-[260px]"><Truncated>{fullField(row, ["proposed_parameter_changes"])}</Truncated></Cell>
                <Cell className="max-w-[360px]"><Truncated>{displayField(row, ["rationale"])}</Truncated></Cell>
                <Cell className="max-w-[300px]"><Truncated>{displayField(row, ["risk"])}</Truncated></Cell>
                <Cell className="text-right">
                  {human === "approved" ? (
                    <StatusBadge tone="good">Promoted</StatusBadge>
                  ) : (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-9 min-h-9"
                      disabled={!canPromote || isPromoting}
                      title={canPromote ? "Promote strategy" : "Promotion requires passing gates"}
                      onClick={() => void onPromote(proposalId)}
                    >
                      {isPromoting ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
                      <span>Promote</span>
                    </Button>
                  )}
                </Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function ThesisPipelinePanel({ requests, theses, validations }: { requests: RowRecord[]; theses: RowRecord[]; validations: RowRecord[] }) {
  const open = countWhere(requests, (row) => textField(row, ["status"], "open").toLowerCase() === "open");
  const failed = countWhere(requests, (row) => textField(row, ["status"]).toLowerCase().includes("failed"));
  const superseded = countWhere(requests, (row) => textField(row, ["status"]).toLowerCase() === "superseded");
  const fulfilled = countWhere(requests, (row) => textField(row, ["status"]).toLowerCase() === "fulfilled");
  const validated = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "validated");
  const items: Array<[string, string, string, Tone]> = [
    ["Active queue", open.toLocaleString(), "top-ranked candidates waiting for agent thesis", open ? "warn" : "muted"],
    ["Completed theses", theses.length.toLocaleString(), "structured hypotheses returned by agents", theses.length ? "good" : "muted"],
    ["Validated", validated.toLocaleString(), "theses passing deterministic checks", validated ? "good" : validations.length ? "warn" : "muted"],
    ["Fulfilled requests", fulfilled.toLocaleString(), "requests matched to completed theses", fulfilled ? "good" : "muted"],
    ["Superseded", superseded.toLocaleString(), "old requests retired from agent usage", superseded ? "muted" : "good"],
    ["Failed", failed.toLocaleString(), "agent calls needing attention", failed ? "bad" : "good"],
  ];
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Thesis Pipeline</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Agent theses are generated only for the current top-ranked queue. A request is not a thesis; decisions should wait for a completed thesis and deterministic validation.
          </p>
        </div>
        <StatusBadge tone={theses.length ? "good" : open ? "warn" : "muted"}>{theses.length ? "Theses available" : open ? "Awaiting agents" : "No active queue"}</StatusBadge>
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
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

function ThesisRequestsTable({ rows, eventById, onOpenTicker, title = "Agent Thesis Queue" }: { rows: RowRecord[]; eventById: Map<string, RowRecord>; onOpenTicker: OpenTicker; title?: string }) {
  if (!rows.length) {
    return <EmptyState title="No active thesis requests" detail="No current top-ranked candidates are waiting for agent thesis generation." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title={title} count={rows.length} />}>
      <table className="w-full min-w-[1120px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head className="text-right">Priority</Head>
            <Head>Candidate State</Head>
            <Head>Created</Head>
            <Head>Candidate</Head>
            <Head>Next Step</Head>
            <Head>Request Detail</Head>
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
                <Cell className="max-w-[280px]"><Truncated>{status.toLowerCase() === "open" ? "Generate structured thesis and validation inputs" : titleLabel(status)}</Truncated></Cell>
                <Cell className="max-w-[360px]"><Truncated>{displayField(row, ["prompt"])}</Truncated></Cell>
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
      if (stateFilter === "validated" && validationState !== "validated") return false;
      if (stateFilter === "pending" && validationState !== "pending") return false;
      if (stateFilter === "invalidated" && !validationState.includes("invalidated")) return false;
      if (stateFilter === "hard-risk" && !redTeam.includes("hard_risk")) return false;
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
              <h2 className="text-base font-semibold">Completed Thesis Browser</h2>
              <StatusBadge tone={thesisRows.length ? "good" : "muted"}>{thesisRows.length.toLocaleString()} theses</StatusBadge>
            </div>
            <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
              Browse one completed hypothesis at a time with its deterministic validation, proof requirements, catalysts, invalidation, and bear case in the same view.
            </p>
          </div>
          <div className="grid grid-cols-3 gap-2 text-right sm:min-w-[360px]">
            <BrowserStat label="Validated" value={validated} tone={validated ? "good" : "muted"} />
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
              <SelectItem value="pending">Pending</SelectItem>
              <SelectItem value="validated">Validated</SelectItem>
              <SelectItem value="invalidated">Invalidated</SelectItem>
              <SelectItem value="hard-risk">Hard risk</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="grid min-h-[640px] xl:grid-cols-[380px_minmax(0,1fr)]">
        <div className="border-b border-border xl:border-b-0 xl:border-r">
          <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-muted-foreground">
            <span>{filtered.length.toLocaleString()} shown</span>
            <span>{stateFilter === "all" ? "Latest first" : titleLabel(stateFilter)}</span>
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
                            <StatusBadge tone={thesisStateTone(textField(validation, ["state"], "pending"))}>{titleLabel(textField(validation, ["state"], "pending"))}</StatusBadge>
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
  const state = textField(validation, ["state"], "pending");
  const redTeam = textField(validation, ["red_team_status"], "not_checked");
  return (
    <div className="min-w-0">
      <div className="border-b border-border p-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : <span className="text-lg font-semibold">Unknown ticker</span>}
              <StatusBadge tone={thesisStateTone(state)}>{titleLabel(state)}</StatusBadge>
              <StatusBadge tone={validationStatusTone(redTeam)}>{titleLabel(redTeam)}</StatusBadge>
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
                    <StatusBadge tone={thesisStateTone(textField(row, ["state"], "pending"))}>{titleLabel(textField(row, ["state"], "pending"))}</StatusBadge>
                    <StatusBadge tone={validationStatusTone(textField(row, ["red_team_status"]))}>{titleLabel(displayField(row, ["red_team_status"], "unknown"))}</StatusBadge>
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

function ThesisValidationsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No thesis validations" detail="No deterministic thesis checks are stored." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Thesis Validation" count={rows.length} />}>
      <table className="w-full min-w-[1220px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Date</Head>
            <Head>Strategy</Head>
            <Head>State</Head>
            <Head>Candidate</Head>
            <Head>Option</Head>
            <Head>Stock Progress</Head>
            <Head>IV</Head>
            <Head>Proofs</Head>
            <Head>Catalysts</Head>
            <Head>Invalidation</Head>
            <Head>Red Team</Head>
            <Head>Evidence</Head>
            <Head>Reason</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const state = textField(row, ["state"], "pending");
            return (
              <tr key={textField(row, ["validation_id"], `${ticker}-${textField(row, ["validated_at"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["validation_date", "validated_at"]))}</Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["strategy_version"])}</Truncated></Cell>
                <Cell><StatusBadge tone={thesisStateTone(state)}>{titleLabel(state)}</StatusBadge></Cell>
                <Cell><StatusBadge tone={stateTone(textField(row, ["candidate_state"]))}>{titleLabel(displayField(row, ["candidate_state"], "pending"))}</StatusBadge></Cell>
                <Cell>{displayField(row, ["option_still_valid"])}</Cell>
                <Cell className="max-w-[240px]"><Truncated>{displayField(row, ["stock_progress"])}</Truncated></Cell>
                <Cell className="max-w-[200px]"><Truncated>{displayField(row, ["iv_status"])}</Truncated></Cell>
                <Cell><StatusBadge tone={validationStatusTone(textField(row, ["proof_status"]))}>{titleLabel(displayField(row, ["proof_status"], "unknown"))}</StatusBadge></Cell>
                <Cell><StatusBadge tone={validationStatusTone(textField(row, ["catalyst_status"]))}>{titleLabel(displayField(row, ["catalyst_status"], "unknown"))}</StatusBadge></Cell>
                <Cell><StatusBadge tone={validationStatusTone(textField(row, ["invalidation_status"]))}>{titleLabel(displayField(row, ["invalidation_status"], "unknown"))}</StatusBadge></Cell>
                <Cell><StatusBadge tone={validationStatusTone(textField(row, ["red_team_status"]))}>{titleLabel(displayField(row, ["red_team_status"], "unknown"))}</StatusBadge></Cell>
                <Cell><StatusBadge tone={validationStatusTone(textField(row, ["evidence_status"]))}>{titleLabel(displayField(row, ["evidence_status"], "unknown"))}</StatusBadge></Cell>
                <Cell className="max-w-[420px]"><Truncated>{displayField(row, ["reason"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function AgentThesisTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No agent theses" detail="No structured agent hypotheses are stored." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Structured Hypotheses" count={rows.length} />}>
      <table className="w-full min-w-[1320px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head className="text-right">Bull Target</Head>
            <Head>Bull Date</Head>
            <Head className="text-right">Base Target</Head>
            <Head className="text-right">Confidence</Head>
            <Head>Catalysts</Head>
            <Head>Core Thesis</Head>
            <Head>Bear Case</Head>
            <Head>Invalidation</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            return (
              <tr key={textField(row, ["thesis_id"], `${ticker}-${textField(row, ["created_at"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["bull_target_price"])}</Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatShortDate(textField(row, ["bull_target_date"]))}</Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["base_target_price"])}</Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["confidence"], Number.NaN))}</Cell>
                <Cell className="max-w-[260px]"><Truncated>{displayField(row, ["catalyst_summary"], fullField(row, ["catalysts"]))}</Truncated></Cell>
                <Cell className="max-w-[380px]"><Truncated>{displayField(row, ["core_thesis"])}</Truncated></Cell>
                <Cell className="max-w-[320px]"><Truncated>{displayField(row, ["bear_case"])}</Truncated></Cell>
                <Cell className="max-w-[320px]"><Truncated>{fullField(row, ["invalidation_conditions"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function DataSamples({
  optionSnapshots,
  optionFeatures,
  stockFeatures,
  strategyVersions,
  onOpenTicker,
}: {
  optionSnapshots: RowRecord[];
  optionFeatures: RowRecord[];
  stockFeatures: RowRecord[];
  strategyVersions: RowRecord[];
  onOpenTicker: OpenTicker;
}) {
  if (!optionSnapshots.length && !optionFeatures.length && !stockFeatures.length && !strategyVersions.length) {
    return <EmptyState title="No radar data" detail="No option snapshots, features, stock features, or strategy versions are stored." icon={Target} />;
  }

  return (
    <>
      <DataTableFrame title={<SectionTitle title="Option Snapshot Sample" count={optionSnapshots.length} />}>
        <table className="w-full min-w-[1100px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <Head>Ticker</Head>
              <Head>Expiration</Head>
              <Head className="text-right">Strike</Head>
              <Head>Type</Head>
              <Head className="text-right">Mid</Head>
              <Head className="text-right">Spread</Head>
              <Head className="text-right">OI</Head>
              <Head className="text-right">Volume</Head>
              <Head className="text-right">Delta</Head>
              <Head className="text-right">DTE</Head>
              <Head>Snapshot</Head>
            </tr>
          </thead>
          <tbody>
            {optionSnapshots.slice(0, 80).map((row) => {
              const ticker = textField(row, ["ticker"]);
              return (
                <tr key={`${textField(row, ["contract_id"])}-${textField(row, ["snapshot_time"])}`} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                  <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                  <Cell className="whitespace-nowrap">{formatShortDate(textField(row, ["expiration"]))}</Cell>
                  <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["strike"], Number.NaN), 2)}</Cell>
                  <Cell>{titleLabel(displayField(row, ["option_type"]))}</Cell>
                  <Cell className="text-right tabular-nums">{moneyField(row, ["mid"])}</Cell>
                  <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["spread_pct"], Number.NaN))}</Cell>
                  <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["open_interest"], Number.NaN), 0)}</Cell>
                  <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["volume"], Number.NaN), 0)}</Cell>
                  <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["delta"], Number.NaN), 2)}</Cell>
                  <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["dte"], Number.NaN), 0)}</Cell>
                  <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["snapshot_time"]))}</Cell>
                </tr>
              );
            })}
          </tbody>
        </table>
      </DataTableFrame>

      <DataTableFrame title={<SectionTitle title="Feature Sample" count={optionFeatures.length + stockFeatures.length} />}>
        <div className="grid min-w-[1040px] gap-4 p-4 lg:grid-cols-2">
          <FeatureList title="Option Features" rows={optionFeatures} kind="option" />
          <FeatureList title="Stock Features" rows={stockFeatures} kind="stock" />
        </div>
      </DataTableFrame>

      <DataTableFrame title={<SectionTitle title="Strategy Versions" count={strategyVersions.length} />}>
        <table className="w-full min-w-[900px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <Head>Version</Head>
              <Head>Name</Head>
              <Head>Status</Head>
              <Head>Created</Head>
              <Head>Parameters</Head>
            </tr>
          </thead>
          <tbody>
            {strategyVersions.slice(0, 40).map((row) => {
              const status = textField(row, ["status"], "active");
              return (
                <tr key={textField(row, ["strategy_version"], textField(row, ["strategy_name"]))} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                  <Cell className="max-w-[260px]"><Truncated>{displayField(row, ["strategy_version"])}</Truncated></Cell>
                  <Cell className="max-w-[240px]"><Truncated>{displayField(row, ["strategy_name"])}</Truncated></Cell>
                  <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                  <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                  <Cell className="max-w-[520px]"><Truncated>{fullField(row, ["parameters"])}</Truncated></Cell>
                </tr>
              );
            })}
          </tbody>
        </table>
      </DataTableFrame>
    </>
  );
}

function FeatureList({ title, rows, kind }: { title: string; rows: RowRecord[]; kind: "option" | "stock" }) {
  if (!rows.length) {
    return (
      <div className="rounded-md border border-border p-4">
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="mt-2 text-sm text-muted-foreground">No rows stored.</p>
      </div>
    );
  }

  return (
    <div className="min-w-0 rounded-md border border-border">
      <div className="border-b border-border px-3 py-2 text-sm font-semibold">{title}</div>
      <div className="divide-y divide-border">
        {rows.slice(0, 8).map((row) => {
          const key = `${textField(row, ["contract_id", "ticker"])}-${textField(row, ["snapshot_time"])}`;
          return (
            <div key={key} className="grid grid-cols-[minmax(120px,1fr)_auto_auto] gap-3 px-3 py-2 text-sm">
              <div className="min-w-0">
                <div className="truncate font-medium">{displayField(row, [kind === "option" ? "contract_id" : "ticker"])}</div>
                <div className="truncate text-xs text-muted-foreground">{formatDate(textField(row, ["snapshot_time"]))}</div>
              </div>
              {kind === "option" ? (
                <>
                  <MetricPill label="Move" value={formatRatio(numberField(row, ["required_move_10x_pct"], Number.NaN))} />
                  <MetricPill label="Liq" value={formatScore(numberField(row, ["liquidity_score"], Number.NaN))} />
                </>
              ) : (
                <>
                  <MetricPill label="ATR" value={formatRatio(numberField(row, ["atr_pct"], Number.NaN))} />
                  <MetricPill label="RS20" value={formatSignedRatio(numberField(row, ["rs_vs_qqq_20d"], Number.NaN))} />
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function HitTimes({ row }: { row: RowRecord }) {
  const labels = [
    ["2x", numberField(row, ["time_to_2x"], Number.NaN)],
    ["5x", numberField(row, ["time_to_5x"], Number.NaN)],
    ["10x", numberField(row, ["time_to_10x"], Number.NaN)],
  ] as const;
  return (
    <div className="flex flex-wrap gap-1.5">
      {labels.map(([label, value]) => (
        <span key={label} className={cn("rounded-md border px-2 py-0.5 text-xs", Number.isFinite(value) ? "border-green-500/30 bg-green-50/30 text-foreground" : "border-border text-muted-foreground")}>
          {label} {Number.isFinite(value) ? `${value}d` : "-"}
        </span>
      ))}
    </div>
  );
}

function AttributionBadge({ row }: { row: RowRecord }) {
  const label = textField(row, ["label"], "unattributed");
  return (
    <div className="min-w-0">
      <StatusBadge tone={attributionTone(label)}>{titleLabel(label)}</StatusBadge>
      <div className="mt-1 truncate text-xs text-muted-foreground">
        Opt {formatSignedRatio(numberField(row, ["option_return"], Number.NaN))} / Under {formatSignedRatio(numberField(row, ["underlying_return"], Number.NaN))}
      </div>
    </div>
  );
}

function transitionEvidence(row: RowRecord): string {
  const refs = row.evidence_refs;
  if (!Array.isArray(refs)) return fullField(row, ["evidence_refs"]);
  const labels = refs
    .map((ref) => {
      if (!ref || typeof ref !== "object" || Array.isArray(ref)) return "";
      const item = ref as Record<string, unknown>;
      return typeof item.type === "string" ? titleLabel(item.type) : "";
    })
    .filter(Boolean);
  return labels.length ? labels.join(", ") : fullField(row, ["evidence_refs"]);
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

function countWhere(items: RowRecord[], predicate: (row: RowRecord) => boolean): number {
  return items.reduce((count, row) => count + (predicate(row) ? 1 : 0), 0);
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
  return compareNumber(numberField(right, ["score"], Number.NEGATIVE_INFINITY), numberField(left, ["score"], Number.NEGATIVE_INFINITY));
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
  if (tier === "Research") return "info";
  return "muted";
}

function thesisStateTone(state: string): Tone {
  const normalized = state.toLowerCase();
  if (normalized.includes("invalidated")) return "bad";
  if (normalized.includes("validated")) return "good";
  if (normalized.includes("weakening")) return "warn";
  return "info";
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
  fix_option_data_disagreement: "Fix option data mismatch",
  hard_red_team_risk: "Hard red-team risk",
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
  needs_clean_data_quality: "Data check required",
  needs_printed_volume: "Needs printed volume",
  needs_source_evidence: "Needs source evidence",
  needs_source_backed_thesis: "Needs source-backed thesis",
  needs_validated_thesis: "Needs validated thesis",
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
  source_evidence_cluster: "Source evidence cluster",
  source_backed_thesis: "Source-backed thesis",
  spread_above_fire_threshold: "Spread above fire limit",
  spread_not_exceptional: "Spread not exceptional",
  spread_reject: "Spread too wide",
  spread_usable: "Spread usable",
  stock_above_50d: "Above 50D",
  stock_below_50d: "Below 50D",
  supportive_market_regime: "Supportive market regime",
  strategy_only_tracks_calls: "Strategy tracks calls only",
  thesis_invalidated: "Thesis invalidated",
  thesis_validated: "Thesis validated",
  volume_below_threshold: "Volume too low",
  volume_seen: "Volume seen",
  wait_for_fire_setup: "Wait for fire setup",
};

function reasonLabel(reason: string): string {
  return reasonLabels[reason] ?? titleLabel(reason);
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

function mergeRowMaps(fallback: Map<string, RowRecord>, preferred: Map<string, RowRecord>): Map<string, RowRecord> {
  const merged = new Map(fallback);
  for (const [key, value] of preferred.entries()) {
    merged.set(key, value);
  }
  return merged;
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

function latestDate(items: RowRecord[], key: string): string {
  let latest = "";
  for (const item of items) {
    const value = textField(item, [key]);
    if (value && (!latest || dateMillis(value) > dateMillis(latest))) latest = value;
  }
  return latest;
}

function hasValue(row: RowRecord, key: string): boolean {
  const value = row[key];
  return value !== undefined && value !== null && value !== "";
}

function moneyField(row: RowRecord | undefined, keys: string[]): string {
  return formatMoney(numberField(row, keys, Number.NaN));
}

function formatRatio(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return `${(value * 100).toFixed(Math.abs(value) >= 10 ? 0 : 1)}%`;
}

function formatSignedRatio(value: number): string {
  if (!Number.isFinite(value)) return "-";
  const pct = value * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(Math.abs(pct) >= 100 ? 0 : 1)}%`;
}

function formatMultiple(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return `${(value + 1).toFixed(value + 1 >= 10 ? 1 : 2)}x`;
}

function formatScore(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toFixed(Math.abs(value) >= 10 ? 0 : 1);
}

function formatNumber(value: number, digits: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function formatDate(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatShortDate(value: string): string {
  if (!value) return "-";
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00` : value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function dateMillis(value: string): number {
  if (!value) return 0;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function validationMillis(row: RowRecord): number {
  return dateMillis(textField(row, ["validated_at"])) || dateMillis(textField(row, ["validation_date"]));
}

function contractTicker(contractId: string): string {
  const match = contractId.match(/^[A-Z]+:([A-Z.]+)/);
  return match?.[1] ?? "";
}
