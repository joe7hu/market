// Signal brief panel, opportunity thesis summary, strategy explainer.

import {useMemo } from "react";
import {StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { JsonValue, RowRecord } from "@/types";
import {Tone } from "@/ui/tone";
import {displayField, fullField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import {moneyField, formatRatio, formatNumber, formatDate, formatScore } from "../optionsRadarFormat";
import {recordField, numberFromRecord, stringFromRecord, boolFromRecord } from "../optionsRadarData";
import {stateTone, tierTone, thesisStateTone, thesisValidationLabel, validationStatusLabel, validationStatusTone } from "../optionsRadarTone";
import {FullText, TickerButton } from "../optionsRadarPrimitives";
import {OpenTicker } from "../workspacePage";
import {summarizeReasons, impactSummary, thesisFallbackText, compareGroupedOpportunities, researchRank, executionQualityScore, opportunityActionText, tierOf, isServiceRepair, commonBlockers, commonDataContractFailures, stateOf } from "./helpers";
import {OptionThesisAgentRuntime } from "./types";
import {BriefCallout, InsightLine, MetricBox } from "./shared";

export function SignalBriefPanel({
  rows,
  activeAlertCount,
  fireCount,
  setupCount,
  symbolsConsidered,
  symbolsWithChains,
  contractsEvaluated,
  opportunityTickerCount,
  latestSnapshot,
  snapshotLabel,
  latestCandidateTime,
  marketState,
  onOpenTicker,
  onOpenDecision,
}: {
  rows: RowRecord[];
  activeAlertCount: number;
  fireCount: number;
  setupCount: number;
  symbolsConsidered: number;
  symbolsWithChains: number;
  contractsEvaluated: number;
  opportunityTickerCount: number;
  latestSnapshot: string;
  snapshotLabel: string;
  latestCandidateTime: string;
  marketState: RowRecord | undefined;
  onOpenTicker: OpenTicker;
  onOpenDecision: (decisionId: string) => void;
}) {
  const offHours = Boolean(snapshotLabel) && snapshotLabel !== "regular";
  const snapshotText = latestSnapshot
    ? `Option data ${formatDate(latestSnapshot)}${offHours ? ` (${snapshotLabel})` : ""}`
    : "No option data";
  const ranked = useMemo(() => [...rows].sort(compareGroupedOpportunities), [rows]);
  const strongest = ranked.find((row) => !isServiceRepair(row)) ?? ranked[0];
  const strongestDecisionId = textField(strongest, ["decision_id", "candidate_event_id"]);
  const hasLowerBound = Boolean(strongest) && Number.isFinite(numberField(strongest, ["lower_95_expected_value"], Number.NaN));
  const probabilitySemantics = textField(strongest, ["probability_semantics"]);
  const calibratedProbability = probabilitySemantics.startsWith("calibrated");
  const repairRows = rows.filter(isServiceRepair);
  const exceptionalRows = rows.filter((row) => stateOf(row) === "READY" && row["execution_ready"] === true);
  const blockedReadyRows = rows.filter((row) => stateOf(row) === "READY" && row["execution_ready"] !== true);
  const researchRows = rows.filter((row) => tierOf(row) === "Research");
  const topBlockers = commonBlockers(rows).slice(0, 3);
  const dataFailures = commonDataContractFailures(repairRows).slice(0, 3);
  const emptyBlocker = emptySignalBlocker(rows);
  const decisionTone: Tone = exceptionalRows.length ? "good" : repairRows.length ? "bad" : researchRows.length ? "info" : "muted";
  const decisionLabel = exceptionalRows.length
    ? `${exceptionalRows.length} trade-ready opportunit${exceptionalRows.length === 1 ? "y" : "ies"}`
    : repairRows.length
      ? `${repairRows.length} data contract issue${repairRows.length === 1 ? "" : "s"}`
      : researchRows.length
        ? `${researchRows.length} research opportunit${researchRows.length === 1 ? "y" : "ies"}`
        : rows.length
          ? `${rows.length} ranked signal${rows.length === 1 ? "" : "s"}`
          : "No current signals";
  const fireGap = fireCount > 0 && exceptionalRows.length === 0;
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <MarketStateBar row={marketState} />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={decisionTone}>{decisionLabel}</StatusBadge>
            {fireGap ? <StatusBadge tone="warn">{fireCount.toLocaleString()} ready contract{fireCount === 1 ? "" : "s"} awaiting grouped evidence</StatusBadge> : null}
            {blockedReadyRows.length ? <StatusBadge tone="warn">{blockedReadyRows.length} READY but execution-blocked</StatusBadge> : null}
            <StatusBadge tone={symbolsWithChains >= 20 ? "good" : symbolsWithChains ? "warn" : "muted"}>{`${symbolsConsidered.toLocaleString()} symbols considered · ${symbolsWithChains.toLocaleString()} chains · ${contractsEvaluated.toLocaleString()} contracts evaluated`}</StatusBadge>
            <StatusBadge tone={opportunityTickerCount ? "info" : "muted"}>{opportunityTickerCount ? `${opportunityTickerCount.toLocaleString()} shortlisted` : "NONE MEETS THE STANDARD"}</StatusBadge>
            {activeAlertCount ? <StatusBadge tone="warn">{activeAlertCount.toLocaleString()} active alert{activeAlertCount === 1 ? "" : "s"}</StatusBadge> : null}
            <StatusBadge tone="muted">{latestCandidateTime ? `Candidate run ${formatDate(latestCandidateTime)}` : "No candidate run"}</StatusBadge>
            <StatusBadge tone={offHours ? "warn" : "muted"}>{snapshotText}</StatusBadge>
          </div>
          {strongest ? (
            <div className="mt-4">
              <div className="flex flex-wrap items-center gap-2">
                {strongestDecisionId ? <Button type="button" variant="ghost" size="sm" className="-ml-2 h-7 font-semibold tracking-normal" onClick={() => onOpenDecision(strongestDecisionId)}>{textField(strongest, ["ticker"])}</Button> : <TickerButton ticker={textField(strongest, ["ticker"])} onOpenTicker={onOpenTicker} />}
                <StatusBadge tone={tierTone(tierOf(strongest))}>{titleLabel(textField(strongest, ["structure"], tierOf(strongest)).replaceAll("_", " "))}</StatusBadge>
                <StatusBadge tone={stateTone(stateOf(strongest))}>{titleLabel(stateOf(strongest) || "watch")}</StatusBadge>
                {strongestDecisionId ? <Button type="button" variant="outline" size="sm" onClick={() => onOpenDecision(strongestDecisionId)}>View ticket</Button> : null}
              </div>
              <p className="mt-2 max-w-5xl text-sm leading-6 text-foreground">{opportunityActionText(strongest)}</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-6">
                <MetricBox label="Research rank" value={formatScore(researchRank(strongest))} />
                <MetricBox label="Execution quality" value={formatScore(executionQualityScore(strongest))} />
                <MetricBox
                  label="Lower-Confidence EV / Max Risk"
                  value={formatRatio(numberField(strongest, ["lower_confidence_expectancy_per_max_risk"], Number.NaN))}
                />
                <MetricBox
                  label={calibratedProbability ? "Calibrated profit probability" : "Model scenario (not calibrated)"}
                  value={calibratedProbability ? formatRatio(numberField(strongest, ["probability_profit"], Number.NaN)) : "Uncalibrated"}
                />
                <MetricBox label={hasLowerBound ? "Lower 95% EV" : "Net EV (Provisional)"} value={moneyField(strongest, hasLowerBound ? ["lower_95_expected_value"] : ["expected_value"])} />
                <MetricBox label={textField(strongest, ["structure"]) === "cash_secured_put" ? "Minimum Credit" : "Maximum Entry"} value={moneyField(strongest, ["suggested_limit", "entry_price", "premium_mid"])} />
              </div>
            </div>
          ) : (
            <p className="mt-4 text-sm text-muted-foreground">No current opportunity read model is available for this radar run.</p>
          )}
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
          <BriefCallout
            label="Decision Impact"
            tone={decisionTone}
            value={strongest ? impactSummary(strongest, fireCount, setupCount) : "Wait for the next radar run."}
          />
          <BriefCallout
            label={repairRows.length ? "Data Blocker" : "Main Blocker"}
            tone={repairRows.length ? "bad" : emptyBlocker ? "muted" : topBlockers.length ? "warn" : "good"}
            value={repairRows.length ? summarizeReasons(dataFailures, "Data contract is clean.") : emptyBlocker ?? summarizeReasons(topBlockers, "Strict gates are clean.")}
          />
        </div>
      </div>
    </section>
  );
}

export function emptySignalBlocker(rows: RowRecord[]): string | null {
  return rows.length ? null : "No contract passed the full qualification gates.";
}

export function marketStateFacts(row: RowRecord | undefined) {
  const nested = recordField(row, "market_state");
  const source: Record<string, JsonValue> | undefined = nested && Object.keys(nested).length
    ? nested
    : row as Record<string, JsonValue> | undefined;
  const value = (keys: string[]) => stringFromRecord(source, keys[0]) || textField(row, keys);
  const directEr = numberFromRecord(source, "kaufman_er_20d");
  const er = Number.isFinite(directEr)
    ? directEr
    : numberField(row, ["market_kaufman_er_20d"], Number.NaN);
  const changes = candidateChangeLines(row?.candidate_changes);
  return {
    direction: value(["trend_state", "direction", "market_direction", "market_state"]),
    efficiencyRatio: Number.isFinite(er) ? formatNumber(er, 2) : value(["er"]),
    volatility: value(["volatility_state", "vol_state"]),
    breadth: value(["breadth_state"]),
    asOf: value(["as_of", "market_as_of", "market_state_as_of", "publication_cutoff"]),
    quality: value(["quality_status", "market_quality", "market_state_quality"]),
    changes,
  };
}

function MarketStateBar({ row }: { row: RowRecord | undefined }) {
  const facts = marketStateFacts(row);
  const values = [
    ["Direction", facts.direction], ["ER", facts.efficiencyRatio], ["Vol", facts.volatility],
    ["Breadth", facts.breadth], ["As of", facts.asOf ? formatDate(facts.asOf) : ""], ["Quality", facts.quality],
  ].filter(([, value]) => value);
  if (!values.length && !facts.changes.length) return null;
  return <div className="mb-4 min-w-0 rounded-md border border-border/70 bg-muted/40 p-3">
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Market state</span>
      {values.map(([label, value]) => <StatusBadge key={label} tone={label === "Quality" ? toneFromText(value) : "muted"}>{label} {titleLabel(value)}</StatusBadge>)}
    </div>
    {facts.changes.length ? <p className="mt-2 text-xs leading-5 text-muted-foreground"><span className="font-semibold text-foreground">Candidate changes: </span>{facts.changes.join(" · ")}</p> : null}
  </div>;
}

function valueList(value: JsonValue | undefined): string[] {
  if (Array.isArray(value)) return value.map((item) => typeof item === "string" || typeof item === "number" ? String(item) : "").filter(Boolean);
  if (typeof value === "string") return value.split(/[;|]/).map((item) => item.trim()).filter(Boolean);
  return [];
}

function candidateChangeLines(value: JsonValue | undefined): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return valueList(value).slice(0, 3);
  const changes = value as Record<string, JsonValue>;
  return ["new", "retained", "removed"].flatMap((kind) => valueList(changes[kind]).map((ticker) => `${ticker} ${kind}`)).slice(0, 3);
}

export function OpportunityThesisSummary({
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

export function StrategyExplainer({ strategy }: { strategy: RowRecord | undefined }) {
  const params = recordField(strategy, "parameters");
  const strategyName = displayField(strategy, ["strategy_name"], "Professional options radar");
  const version = displayField(strategy, ["strategy_version"], "No strategy loaded");
  const status = textField(strategy, ["status"], "shadow");
  const rules = [
    ["Contract", `${titleLabel(stringFromRecord(params, "option_type", "call"))} options`],
    ["Delta", `${formatNumber(numberFromRecord(params, "delta_min"), 2)}-${formatNumber(numberFromRecord(params, "delta_max"), 2)}`],
    ["DTE", `${formatNumber(numberFromRecord(params, "dte_min"), 0)}-${formatNumber(numberFromRecord(params, "dte_max"), 0)} days`],
    ["Spread", `Eligible <= ${formatRatio(numberFromRecord(params, "max_spread_pct"))}`],
    ["Liquidity", `OI >= ${formatNumber(numberFromRecord(params, "min_open_interest"), 0)}; volume >= ${formatNumber(numberFromRecord(params, "min_volume"), 0)}`],
    ["IV", `Fire <= ${formatNumber(numberFromRecord(params, "max_iv_percentile"), 0)} pctile; reject > ${formatNumber(numberFromRecord(params, "reject_iv_percentile"), 0)}`],
    ["Trend", `${boolFromRecord(params, "require_price_above_ma50") ? "Above 50D" : "50D optional"}; ${boolFromRecord(params, "require_rs_improving") ? "RS vs QQQ improving" : "RS optional"}`],
    ["Structures", "Long calls, long puts, and cash-secured puts"],
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
            A shadow-only decision system for executable directional options and fully collateralized short puts. Research rank, assignment basis, tail loss, execution quality, and portfolio constraints remain separate; READY stays locked until forward calibration matures.
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
