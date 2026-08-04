// Forward-only option recovery evidence, outcomes, and advisory provenance.

import {DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import {RowRecord } from "@/types";
import {formatRatio } from "../optionsRadarFormat";
import {jsonArrayField, jsonRecord } from "../optionsRadarData";
import {displayField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import {Cell, Head, MetricPill, SectionTitle, TickerButton, Truncated } from "../optionsRadarPrimitives";
import {OpenTicker } from "../workspacePage";
import {MetricBox } from "./shared";

type RecoveryProgramPanelProps = {
  funnel: RowRecord | undefined;
  events: RowRecord[];
  opportunities: RowRecord[];
  familyPerformance: RowRecord[];
  agentProvenance: RowRecord[];
  health: RowRecord | undefined;
  onOpenTicker: OpenTicker;
};

const OUTCOME_ORDER = ["captured", "missed", "unfilled", "unmeasurable"];

function outcomeTone(outcome: string): "good" | "bad" | "warn" | "muted" {
  if (outcome === "captured") return "good";
  if (outcome === "missed") return "bad";
  if (outcome === "unfilled") return "warn";
  return "muted";
}

function eventTone(status: string): "good" | "warn" | "muted" {
  if (status === "active") return "good";
  if (status === "deferred_capacity") return "warn";
  return "muted";
}

function stageCount(funnel: RowRecord | undefined, name: string): number {
  for (const item of jsonArrayField(funnel, "stages")) {
    const stage = jsonRecord(item);
    if (textField(stage, ["stage"]) === name) return numberField(stage, ["count"], 0);
  }
  return 0;
}

export function RecoveryProgramPanel({
  funnel,
  events,
  opportunities,
  familyPerformance,
  agentProvenance,
  health,
  onOpenTicker,
}: RecoveryProgramPanelProps) {
  const activeEvents = events.filter((row) => ["active", "deferred_capacity"].includes(textField(row, ["status"])));
  const outcomeCounts = new Map(OUTCOME_ORDER.map((outcome) => [outcome, 0]));
  for (const row of opportunities) {
    const outcome = textField(row, ["outcome_classification"], "observing");
    if (outcomeCounts.has(outcome)) outcomeCounts.set(outcome, (outcomeCounts.get(outcome) ?? 0) + 1);
  }
  const observed = stageCount(funnel, "observed");
  const program = jsonRecord(health?.program);
  const currentCohort = jsonRecord(program?.current_cohort);
  const paperStaging = jsonRecord(program?.paper_staging);
  const programState = textField(program as RowRecord | undefined, ["program_state"], "collecting");
  const qualifiedDates = numberField(program as RowRecord | undefined, ["qualified_dates"], 0);
  const requiredDates = numberField(program as RowRecord | undefined, ["required_qualified_dates"], 5);
  const cohortLabel = textField(currentCohort as RowRecord | undefined, ["objective_version"], "Current cohort");
  const paperEligibility = textField(paperStaging as RowRecord | undefined, ["eligible"], "No");

  return (
    <section className="space-y-4 rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h2 className="text-base font-semibold">Forward Opportunity Recovery</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Executable-side, forward-only evidence for sell-off continuation and rebound contracts. Historical midpoint or peak examples do not enter this cohort.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={programState === "paper_enabled" ? "good" : programState === "collecting" ? "warn" : "info"}>{titleLabel(programState)}</StatusBadge>
          <StatusBadge tone="info">{qualifiedDates}/{requiredDates} qualified dates</StatusBadge>
          <StatusBadge tone={paperEligibility === "Yes" ? "good" : "warn"}>Paper eligibility: {paperEligibility}</StatusBadge>
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-6">
        {(["observed", "measurable", "signaled", "ticketed", "filled", "exited"] as const).map((stage) => (
          <MetricBox key={stage} label={titleLabel(stage)} value={stageCount(funnel, stage).toLocaleString()} />
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        Cohort: {cohortLabel}. Paper staging remains local and is separately kill-switched; no broker order authority exists.
      </p>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(360px,0.8fr)]">
        <DataTableFrame title={<SectionTitle title="Active sell-off / rebound events" count={activeEvents.length} />}>
          {activeEvents.length ? (
            <table className="w-full min-w-[980px] text-sm">
              <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  <Head>Symbol</Head>
                  <Head>Status</Head>
                  <Head>Trigger / reference</Head>
                  <Head>Priority / capacity</Head>
                  <Head className="text-right">Captures</Head>
                  <Head className="text-right">Signals</Head>
                  <Head className="text-right">Paper</Head>
                </tr>
              </thead>
              <tbody>
                {activeEvents.slice(0, 12).map((row) => {
                  const symbol = textField(row, ["symbol"]);
                  const status = textField(row, ["status"]);
                  return (
                    <tr key={textField(row, ["event_id"])} className="border-b border-border align-top last:border-0 hover:bg-accent/40">
                      <Cell>{symbol ? <TickerButton ticker={symbol} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                      <Cell><StatusBadge tone={eventTone(status)}>{titleLabel(status)}</StatusBadge></Cell>
                      <Cell>
                        <div>{titleLabel(textField(row, ["trigger_reason", "event_type"]))}</div>
                        <div className="text-xs text-muted-foreground">
                          {displayField(row, ["reference_source_id"], "unconfirmed")} · {displayField(row, ["reference_trading_date"], "no reference date")} · {formatQuoteAge(numberField(row, ["quote_age_minutes"], Number.NaN))}
                        </div>
                      </Cell>
                      <Cell>
                        <div className="tabular-nums">{numberField(row, ["severity_score"], 0).toFixed(1)}/100</div>
                        <div className="text-xs text-muted-foreground">{displayField(row, ["capacity_defer_reason"], "admitted")}</div>
                      </Cell>
                      <Cell className="text-right tabular-nums">{numberField(row, ["complete_captures"], 0).toLocaleString()}</Cell>
                      <Cell className="text-right tabular-nums">{numberField(row, ["active_signals"], 0).toLocaleString()}</Cell>
                      <Cell className="text-right tabular-nums">{numberField(row, ["open_paper_orders"], 0).toLocaleString()}</Cell>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="p-4 text-sm text-muted-foreground">No active recovery events. The detector continues to watch the effective watchlist and radar universe.</div>
          )}
        </DataTableFrame>

        <div className="rounded-md border border-border bg-background p-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="font-semibold">Full-denominator outcomes</h3>
            <span className="text-xs text-muted-foreground">{observed.toLocaleString()} observed</span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            {OUTCOME_ORDER.map((outcome) => (
              <div key={outcome} className="rounded-md bg-muted px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <StatusBadge tone={outcomeTone(outcome)}>{titleLabel(outcome)}</StatusBadge>
                  <span className="font-semibold tabular-nums">{(outcomeCounts.get(outcome) ?? 0).toLocaleString()}</span>
                </div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">Unmeasurable contracts are retained and reported, never treated as losses or missed winners.</p>
        </div>
      </div>

      <DataTableFrame title={<SectionTitle title="Family performance" count={familyPerformance.length} />}>
        {familyPerformance.length ? (
          <table className="w-full min-w-[1040px] text-sm">
            <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <Head>Family</Head>
                <Head className="text-right">Events</Head>
                <Head className="text-right">Shadow</Head>
                <Head className="text-right">Fills</Head>
                <Head className="text-right">3× recall</Head>
                <Head className="text-right">4× recall</Head>
                <Head className="text-right">Net expectancy</Head>
                <Head className="text-right">95% lower bound</Head>
              </tr>
            </thead>
            <tbody>
              {familyPerformance.map((row) => (
                <tr key={textField(row, ["family"])} className="border-b border-border align-top last:border-0 hover:bg-accent/40">
                  <Cell><Truncated>{titleLabel(textField(row, ["family"]))}</Truncated></Cell>
                  <Cell className="text-right tabular-nums">{numberField(row, ["independent_events"], 0).toLocaleString()}</Cell>
                  <Cell className="text-right tabular-nums">{numberField(row, ["shadow_signals"], 0).toLocaleString()}</Cell>
                  <Cell className="text-right tabular-nums">{numberField(row, ["paper_fills"], 0).toLocaleString()}</Cell>
                  <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["event_3x_recall"], Number.NaN))}</Cell>
                  <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["event_4x_recall"], Number.NaN))}</Cell>
                  <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["net_expectancy"], Number.NaN))}</Cell>
                  <Cell className="text-right tabular-nums">{formatRatio(numberField(row, ["lower_95_expectancy"], Number.NaN))}</Cell>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <EmptyState title="No family evidence yet" detail="Performance appears after the first forward event-strip observations are evaluated." />}
      </DataTableFrame>

      <DataTableFrame title={<SectionTitle title="Advisory thesis and countercase provenance" count={agentProvenance.length} />}>
        {agentProvenance.length ? (
          <table className="w-full min-w-[900px] text-sm">
            <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
              <tr><Head>Event</Head><Head>Role</Head><Head>Status</Head><Head>Evidence</Head><Head>Proposal</Head></tr>
            </thead>
            <tbody>
              {agentProvenance.slice(0, 24).map((row) => {
                const status = textField(row, ["status"]);
                return (
                  <tr key={textField(row, ["task_id", "id"])} className="border-b border-border align-top last:border-0 hover:bg-accent/40">
                    <Cell><Truncated>{displayField(row, ["event_id"], "-")}</Truncated></Cell>
                    <Cell>{titleLabel(textField(row, ["role"]))}</Cell>
                    <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                    <Cell className="tabular-nums">{numberField(row, ["evidence_count", "validated_evidence_count"], 0).toLocaleString()}</Cell>
                    <Cell><Truncated>{displayField(row, ["proposal_status", "mutation_status"], "Advisory only")}</Truncated></Cell>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <div className="p-4 text-sm text-muted-foreground">No advisory batch has run for an active event yet.</div>}
      </DataTableFrame>
    </section>
  );
}

function formatQuoteAge(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(1)}m quote age` : "no fresh quote";
}
