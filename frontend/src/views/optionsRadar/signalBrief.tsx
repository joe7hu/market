// Signal brief panel, opportunity thesis summary, strategy explainer.

import {useMemo } from "react";
import {StatusBadge } from "@/components/market/workstation";
import {RowRecord } from "@/types";
import {Tone } from "@/ui/tone";
import {displayField, fullField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import {moneyField, formatRatio, formatScore, formatNumber, formatDate } from "../optionsRadarFormat";
import {recordField, numberFromRecord, stringFromRecord, boolFromRecord } from "../optionsRadarData";
import {stateTone, tierTone, thesisStateTone, thesisValidationLabel, validationStatusLabel, validationStatusTone } from "../optionsRadarTone";
import {FullText, TickerButton } from "../optionsRadarPrimitives";
import {OpenTicker } from "../workspacePage";
import {summarizeReasons, impactSummary, thesisFallbackText, compareGroupedOpportunities, opportunityActionText, tierOf, isServiceRepair, commonBlockers, commonDataContractFailures } from "./helpers";
import {OptionThesisAgentRuntime } from "./types";
import {BriefCallout, InsightLine, MetricBox } from "./shared";

export function SignalBriefPanel({
  rows,
  activeAlertCount,
  fireCount,
  setupCount,
  scannedTickerCount,
  opportunityTickerCount,
  latestSnapshot,
  snapshotLabel,
  latestCandidateTime,
  onOpenTicker,
}: {
  rows: RowRecord[];
  activeAlertCount: number;
  fireCount: number;
  setupCount: number;
  scannedTickerCount: number;
  opportunityTickerCount: number;
  latestSnapshot: string;
  snapshotLabel: string;
  latestCandidateTime: string;
  onOpenTicker: OpenTicker;
}) {
  const offHours = Boolean(snapshotLabel) && snapshotLabel !== "regular";
  const snapshotText = latestSnapshot
    ? `Option data ${formatDate(latestSnapshot)}${offHours ? ` (${snapshotLabel})` : ""}`
    : "No option data";
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
            <StatusBadge tone={scannedTickerCount >= 20 ? "good" : scannedTickerCount ? "warn" : "muted"}>{`${scannedTickerCount.toLocaleString()} scanned / ${opportunityTickerCount.toLocaleString()} with setups`}</StatusBadge>
            {activeAlertCount ? <StatusBadge tone="warn">{activeAlertCount.toLocaleString()} active alert{activeAlertCount === 1 ? "" : "s"}</StatusBadge> : null}
            <StatusBadge tone="muted">{latestCandidateTime ? `Candidate run ${formatDate(latestCandidateTime)}` : "No candidate run"}</StatusBadge>
            <StatusBadge tone={offHours ? "warn" : "muted"}>{snapshotText}</StatusBadge>
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
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
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
        </div>
      </div>
    </section>
  );
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

