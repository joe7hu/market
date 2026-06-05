import { Activity, BrainCircuit, CheckCircle2, GitBranchPlus, Loader2, Target, TrendingUp } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { promoteStrategyMutation } from "@/api";
import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import type { PanelData, RowRecord, TablePayload } from "@/types";
import type { Tone } from "@/ui/tone";
import { displayField, formatMoney, fullField, numberField, textField, titleLabel, toneFromText } from "./rowFormat";
import { WorkspacePage, type MetricSpec, type OpenTicker } from "./workspacePage";

type OptionsRadarPageProps = {
  data: PanelData;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void> | void;
};

export function OptionsRadarPage({ data, onOpenTicker, onRefresh }: OptionsRadarPageProps) {
  const [promotingProposal, setPromotingProposal] = useState<string | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const candidates = rows(data.candidateEvent);
  const candidateMarks = rows(data.candidateEventMark);
  const candidateAttributions = rows(data.candidateEventAttribution);
  const shadowTrades = rows(data.shadowTrade);
  const shadowMarks = rows(data.shadowTradeMark);
  const stateTransitions = rows(data.radarStateTransition);
  const attributions = rows(data.optionAttribution);
  const missedWinners = rows(data.missedWinnerEvent);
  const proposals = rows(data.strategyMutationProposal);
  const backtests = rows(data.strategyBacktestResult);
  const forwardTests = rows(data.strategyForwardTestResult);
  const cohorts = rows(data.strategyCohortResult);
  const thesisRequests = rows(data.agentThesisRequest);
  const thesisValidations = rows(data.agentThesisValidation);
  const postmortemRequests = rows(data.agentPostmortemRequest);
  const postmortems = rows(data.agentPostmortem);
  const agentTheses = rows(data.agentThesis);
  const optionSnapshots = rows(data.optionSnapshot);
  const optionFeatures = rows(data.optionFeatures);
  const stockFeatures = rows(data.stockFeatures);
  const strategyVersions = rows(data.optionStrategyVersions);

  const eventById = useMemo(() => mapBy(candidates, "event_id"), [candidates]);
  const latestAttributionByEvent = useMemo(() => latestBy(attributions, "event_id", "snapshot_time"), [attributions]);
  const latestBacktestByProposal = useMemo(() => latestBy(backtests, "proposal_id", "evaluated_at"), [backtests]);
  const latestForwardByProposal = useMemo(() => latestBy(forwardTests, "proposal_id", "evaluated_at"), [forwardTests]);

  const fireCount = countWhere(candidates, (row) => stateOf(row) === "FIRE");
  const setupCount = countWhere(candidates, (row) => stateOf(row) === "SETUP");
  const watchCount = countWhere(candidates, (row) => stateOf(row) === "WATCH");
  const candidateHit2x = countWhere(candidateMarks, (row) => hasValue(row, "time_to_2x"));
  const candidateHit5x = countWhere(candidateMarks, (row) => hasValue(row, "time_to_5x"));
  const openShadowCount = countWhere(shadowTrades, (row) => !["closed", "exited"].includes(textField(row, ["status"]).toLowerCase()));
  const hit2x = countWhere(shadowTrades, (row) => hasValue(row, "time_to_2x"));
  const hit5x = countWhere(shadowTrades, (row) => hasValue(row, "time_to_5x"));
  const exitStateCount = countWhere(stateTransitions, (row) => ["EXIT", "INVALIDATED"].includes(stateOf(row)));
  const activeStateCount = countWhere(stateTransitions, (row) => ["FIRE", "HOLD", "TRIM"].includes(stateOf(row)));
  const missed10x = countWhere(missedWinners, (row) => textField(row, ["winner_threshold"]).toLowerCase() === "10x");
  const openPostmortems = countWhere(postmortemRequests, (row) => textField(row, ["status"], "open").toLowerCase() === "open");
  const humanPending = countWhere(proposals, (row) => !["approved", "rejected"].includes(textField(row, ["human_approval_status"], "pending").toLowerCase()));
  const forwardCollecting = countWhere(forwardTests, (row) => textField(row, ["verdict", "status"]).toLowerCase() === "collecting_data");

  const metrics: MetricSpec[] = [
    ["Fire", fireCount.toLocaleString(), `${setupCount.toLocaleString()} setup, ${watchCount.toLocaleString()} watch`, fireCount ? "good" : setupCount ? "warn" : "muted"],
    ["Candidate Marks", candidateMarks.length.toLocaleString(), `${candidateHit2x.toLocaleString()} hit 2x, ${candidateHit5x.toLocaleString()} hit 5x, ${candidateAttributions.length.toLocaleString()} attributions`, candidateHit5x ? "good" : candidateHit2x ? "info" : candidateMarks.length ? "muted" : "muted"],
    ["Shadow", openShadowCount.toLocaleString(), `${hit2x.toLocaleString()} hit 2x, ${hit5x.toLocaleString()} hit 5x, ${shadowMarks.length.toLocaleString()} marks`, openShadowCount ? "info" : "muted"],
    ["States", stateTransitions.length.toLocaleString(), `${activeStateCount.toLocaleString()} active, ${exitStateCount.toLocaleString()} exit or invalidated`, exitStateCount ? "warn" : activeStateCount ? "good" : "muted"],
    ["Missed Winners", missedWinners.length.toLocaleString(), `${missed10x.toLocaleString()} reached 10x`, missedWinners.length ? "warn" : "muted"],
    ["Postmortems", postmortems.length.toLocaleString(), `${openPostmortems.toLocaleString()} open requests`, openPostmortems ? "warn" : postmortems.length ? "info" : "muted"],
    ["Strategy Gates", humanPending.toLocaleString(), `${forwardCollecting.toLocaleString()} forward tests collecting, ${cohorts.length.toLocaleString()} cohorts`, humanPending ? "warn" : "info"],
  ];

  const latestSnapshot = latestDate(optionSnapshots, "snapshot_time");
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
      metrics={metrics}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={latestSnapshot ? "good" : "muted"}>{latestSnapshot ? `Snapshot ${formatDate(latestSnapshot)}` : "No snapshots"}</StatusBadge>
          <StatusBadge tone="info">{displayField(latestStrategy, ["strategy_version", "strategy_name"], "No strategy")}</StatusBadge>
        </div>
      }
    >
      <Tabs defaultValue="radar" className="min-w-0">
        <TabsList className="h-auto max-w-full flex-wrap justify-start">
          <TabsTrigger value="radar">Radar</TabsTrigger>
          <TabsTrigger value="learning">Learning</TabsTrigger>
          <TabsTrigger value="theses">Thesis Queue</TabsTrigger>
          <TabsTrigger value="data">Data</TabsTrigger>
        </TabsList>

        <TabsContent value="radar" className="space-y-4">
          <CandidateEventsTable rows={candidates} onOpenTicker={onOpenTicker} />
          <CandidateEventMarksTable rows={candidateMarks} onOpenTicker={onOpenTicker} />
          <CandidateEventAttributionsTable rows={candidateAttributions} onOpenTicker={onOpenTicker} />
          <RadarStateTransitionsTable rows={stateTransitions} onOpenTicker={onOpenTicker} />
          <ShadowTradesTable rows={shadowTrades} eventById={eventById} latestAttributionByEvent={latestAttributionByEvent} onOpenTicker={onOpenTicker} />
          <ShadowTradeMarksTable rows={shadowMarks} onOpenTicker={onOpenTicker} />
        </TabsContent>

        <TabsContent value="learning" className="space-y-4">
          <MissedWinnersTable rows={missedWinners} onOpenTicker={onOpenTicker} />
          <PostmortemRequestsTable rows={postmortemRequests} onOpenTicker={onOpenTicker} />
          <PostmortemsTable rows={postmortems} onOpenTicker={onOpenTicker} />
          <CohortResultsTable rows={cohorts} />
          <StrategyProposalsTable
            rows={proposals}
            backtestByProposal={latestBacktestByProposal}
            forwardByProposal={latestForwardByProposal}
            promotingProposal={promotingProposal}
            promotionError={promotionError}
            onPromote={handlePromoteProposal}
          />
        </TabsContent>

        <TabsContent value="theses" className="space-y-4">
          <ThesisRequestsTable rows={thesisRequests} eventById={eventById} onOpenTicker={onOpenTicker} />
          <ThesisValidationsTable rows={thesisValidations} onOpenTicker={onOpenTicker} />
          <AgentThesisTable rows={agentTheses} onOpenTicker={onOpenTicker} />
        </TabsContent>

        <TabsContent value="data" className="space-y-4">
          <DataSamples optionSnapshots={optionSnapshots} optionFeatures={optionFeatures} stockFeatures={stockFeatures} strategyVersions={strategyVersions} onOpenTicker={onOpenTicker} />
        </TabsContent>
      </Tabs>
    </WorkspacePage>
  );
}

function CandidateEventsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No candidate events" detail="No options radar candidates are stored yet." icon={Target} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Candidate Events" count={rows.length} />}>
      <table className="w-full min-w-[1180px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>State</Head>
            <Head>Contract</Head>
            <Head>Strategy</Head>
            <Head className="text-right">Premium</Head>
            <Head className="text-right">Buy Under</Head>
            <Head className="text-right">10x Price</Head>
            <Head className="text-right">Move</Head>
            <Head className="text-right">Score</Head>
            <Head>Thesis</Head>
            <Head>Trigger</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const state = stateOf(row);
            return (
              <tr key={textField(row, ["event_id"], `${ticker}-${textField(row, ["contract_id"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge></Cell>
                <Cell className="max-w-[260px]"><Truncated>{displayField(row, ["contract_id"])}</Truncated></Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["strategy_version"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["premium_mid"])}</Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["buy_under"])}</Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["required_10x_price"])}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["required_move_pct"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["score"], Number.NaN))}</Cell>
                <Cell>{textField(row, ["thesis_id"]) ? <StatusBadge tone="good">Attached</StatusBadge> : <StatusBadge tone="muted">Open</StatusBadge>}</Cell>
                <Cell className="max-w-[340px]"><Truncated>{displayField(row, ["trigger_reason"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
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

function CohortResultsTable({ rows }: { rows: RowRecord[] }) {
  if (!rows.length) {
    return <EmptyState title="No cohort results" detail="No deterministic setup cohorts have enough shadow outcomes yet." icon={Activity} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Cohort Learning" count={rows.length} />}>
      <table className="w-full min-w-[1180px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Cohort</Head>
            <Head className="text-right">Candidates</Head>
            <Head className="text-right">2x</Head>
            <Head className="text-right">5x</Head>
            <Head className="text-right">10x</Head>
            <Head className="text-right">Median Max</Head>
            <Head className="text-right">Median DD</Head>
            <Head className="text-right">Early</Head>
            <Head className="text-right">Bleed</Head>
            <Head className="text-right">Convexity</Head>
            <Head className="text-right">QQQ 200D</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const key = textField(row, ["cohort_id"], `${textField(row, ["cohort_type"])}-${textField(row, ["cohort_value"])}`);
            return (
              <tr key={key} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell className="max-w-[260px]">
                  <div className="min-w-0">
                    <div className="truncate font-medium">{titleLabel(displayField(row, ["cohort_value"]))}</div>
                    <div className="truncate text-xs text-muted-foreground">{displayField(row, ["cohort_type"])}</div>
                  </div>
                </Cell>
                <Cell className="text-right tabular-nums">{formatNumber(numberField(row, ["candidate_count"], Number.NaN), 0)}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["hit_rate_2x"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["hit_rate_5x"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["hit_rate_10x"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatMultiple(numberField(row, ["median_max_return"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatSignedRatio(numberField(row, ["median_max_drawdown"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["early_entry_rate"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["theta_iv_bleed_rate"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["good_convexity_rate"], Number.NaN))}</Cell>
                <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["qqq_above_200d_rate"], Number.NaN))}</Cell>
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
                <Cell><StatusBadge tone={human === "approved" ? "good" : human === "rejected" ? "bad" : "warn"}>{titleLabel(human)}</StatusBadge></Cell>
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

function ThesisRequestsTable({ rows, eventById, onOpenTicker }: { rows: RowRecord[]; eventById: Map<string, RowRecord>; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No thesis requests" detail="No agent thesis handoffs are open." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Agent Thesis Queue" count={rows.length} />}>
      <table className="w-full min-w-[1040px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head className="text-right">Priority</Head>
            <Head>Candidate State</Head>
            <Head>Strategy</Head>
            <Head>Created</Head>
            <Head>Prompt</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const event = eventById.get(textField(row, ["event_id"]));
            const status = textField(row, ["status"], "open");
            return (
              <tr key={textField(row, ["request_id"], `${ticker}-${textField(row, ["created_at"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["priority_score"], Number.NaN))}</Cell>
                <Cell><StatusBadge tone={stateTone(stateOf(event))}>{titleLabel(stateOf(event) || "pending")}</StatusBadge></Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["strategy_version"])}</Truncated></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                <Cell className="max-w-[360px]"><Truncated>{displayField(row, ["prompt"])}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
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
      <table className="w-full min-w-[1100px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head className="text-right">Bull Target</Head>
            <Head>Bull Date</Head>
            <Head className="text-right">Base Target</Head>
            <Head className="text-right">Confidence</Head>
            <Head>Core Thesis</Head>
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
                <Cell className="max-w-[380px]"><Truncated>{displayField(row, ["core_thesis"])}</Truncated></Cell>
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

function countWhere(items: RowRecord[], predicate: (row: RowRecord) => boolean): number {
  return items.reduce((count, row) => count + (predicate(row) ? 1 : 0), 0);
}

function stateOf(row: RowRecord | undefined): string {
  return textField(row, ["state"]).toUpperCase();
}

function stateTone(state: string): Tone {
  const normalized = state.toUpperCase();
  if (normalized === "FIRE" || normalized === "HOLD") return "good";
  if (normalized === "SETUP" || normalized === "TRIM") return "warn";
  if (normalized === "INVALIDATED" || normalized === "EXIT") return "bad";
  if (normalized === "WATCH") return "info";
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

function contractTicker(contractId: string): string {
  const match = contractId.match(/^[A-Z]+:([A-Z.]+)/);
  return match?.[1] ?? "";
}
