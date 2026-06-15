// Pure derivation/selector helpers for the options radar view. No JSX, no
// component deps — row selectors, comparators, tier/quality/thesis-state logic,
// gate/proposal summaries, and agent-runtime derivation shared by every radar
// section.

import type { JsonValue, PanelData, RowRecord, TablePayload } from "@/types";
import type { Tone } from "@/ui/tone";
import { cn } from "@/lib/utils";

import { displayField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import { dateMillis, formatNumber } from "../optionsRadarFormat";
import { arrayText, boolFromRecord, jsonRecord, listFromRecord, numberFromRecord, recordField, stringFromRecord } from "../optionsRadarData";
import { reasonLabel, stateRank, stateTone } from "../optionsRadarTone";
import type { CandidateFocus, CandidateSort, OptionThesisAgentRuntime, QualityFilter, ThesisFilter } from "./types";

export function tabButtonClass(active: boolean): string {
  return cn(
    "rounded-sm px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
    active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
  );
}

export function summarizeReasons(reasons: Array<[string, number]>, empty: string): string {
  if (!reasons.length) return empty;
  return reasons.map(([reason, count]) => `${reasonLabel(reason)} (${count})`).join("; ");
}

export function impactSummary(row: RowRecord, fireCount = 0, setupCount = 0): string {
  if (isServiceRepair(row)) return "Do not interpret as a trade setup until the data contract is fixed.";
  if (tierOf(row) === "Exceptional") return "Candidate is allowed into trade review because data, asymmetry, entry, and evidence gates are aligned.";
  if (tierOf(row) === "Research") {
    const contractMix = fireCount ? `${fireCount.toLocaleString()} FIRE contract${fireCount === 1 ? "" : "s"}` : `${setupCount.toLocaleString()} setup contract${setupCount === 1 ? "" : "s"}`;
    return `${contractMix} exist, but the grouped ticker is still Research because strict evidence, thesis, regime, or blocker gates are not all clean.`;
  }
  return "Watch only; signal is not yet strong enough for research priority.";
}

export function thesisFallbackText(row: RowRecord, request: RowRecord | undefined, agentRuntime: OptionThesisAgentRuntime): string {
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

export function compareGroupedOpportunities(left: RowRecord, right: RowRecord): number {
  const leftState = textField(left, ["primary_state"]).toUpperCase();
  const rightState = textField(right, ["primary_state"]).toUpperCase();
  return (
    stateRank(leftState) - stateRank(rightState) ||
    compareNumber(numberField(right, ["conviction_score"], Number.NEGATIVE_INFINITY), numberField(left, ["conviction_score"], Number.NEGATIVE_INFINITY)) ||
    compareText(textField(left, ["ticker"]), textField(right, ["ticker"]))
  );
}

export function opportunityActionText(row: RowRecord): string {
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

export function candidateActionText(row: RowRecord, validation: RowRecord | undefined): string {
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

export function postmortemImpact(row: RowRecord): string {
  const sourceType = textField(row, ["source_type"]);
  if (sourceType.includes("winner")) return "Explain what made the move work before changing strategy gates.";
  if (sourceType.includes("loser")) return "Explain whether the setup failed, aged out, or exposed a bad gate.";
  return "Summarize only if it changes ranking, risk, or strategy rules.";
}

export function cohortKey(row: RowRecord): string {
  return textField(row, ["cohort_id"], `${textField(row, ["cohort_type"])}-${textField(row, ["cohort_value"])}`);
}

export function cohortObservationStats(row: RowRecord): { sampleCount: number; maxObservationDays: number; matureCount: number } {
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

export function formatObservedWindow(days: number): string {
  if (!Number.isFinite(days)) return "-";
  if (days <= 0) return "<1d";
  if (days < 1) return `${Math.max(1, Math.round(days * 24))}h`;
  return `${days.toFixed(days >= 10 ? 0 : 1)}d`;
}

export function proposalGateSummary(row: RowRecord, backtest: RowRecord | undefined, forward: RowRecord | undefined): { label: string; detail: string; tone: Tone } {
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

export function proposalChangeItems(row: RowRecord): string[] {
  const changes = recordField(row, "proposed_parameter_changes");
  if (!changes) return [];
  return Object.entries(changes)
    .filter(([key, value]) => !["candidate_note", "filter_reason", "setup_type"].includes(key) && valueIsPresent(value))
    .map(([key, value]) => `${titleLabel(key)}: ${formatConfigValue(value)}`);
}

export function proposalChangeNote(row: RowRecord): string {
  const changes = recordField(row, "proposed_parameter_changes");
  return stringFromRecord(changes, "candidate_note") || stringFromRecord(changes, "filter_reason") || stringFromRecord(changes, "setup_type");
}

export function compactStrategyVersion(value: string): string {
  if (!value) return "Proposed strategy change";
  return titleLabel(value.replace(/^leap_10x_reversal_v1_?/, "").replace(/_agent_proposed_v\d+$/, "") || value);
}

export function backtestDetail(row: RowRecord | undefined): string {
  if (!row) return "Waiting for deterministic replay.";
  const baseline = numberField(row, ["baseline_candidate_count"], Number.NaN);
  const proposed = numberField(row, ["proposed_candidate_count"], Number.NaN);
  const verdict = textField(row, ["verdict"]).toLowerCase();
  const countDetail = Number.isFinite(baseline) && Number.isFinite(proposed) ? `${formatNumber(baseline, 0)} -> ${formatNumber(proposed, 0)} candidates` : "Candidate impact unavailable";
  if (verdict === "fail") return `${countDetail}; no proven improvement.`;
  if (verdict === "pass") return `${countDetail}; passed replay.`;
  return countDetail;
}

export function forwardDetail(row: RowRecord | undefined): string {
  if (!row) return "Waiting for shadow comparison.";
  const days = numberField(row, ["days_observed"], Number.NaN);
  const raw = recordField(row, "raw");
  const minimumDays = numberFromRecord(raw, "min_forward_test_days");
  if (Number.isFinite(days) && Number.isFinite(minimumDays)) return `${formatNumber(days, 0)}/${formatNumber(minimumDays, 0)} days observed`;
  if (Number.isFinite(days)) return `${formatNumber(days, 0)} days observed`;
  return "Observation window pending.";
}

export function valueIsPresent(value: JsonValue | undefined): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

export function formatConfigValue(value: JsonValue): string {
  if (typeof value === "boolean") return value ? "required" : "off";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  if (typeof value === "string") return titleLabel(value);
  if (Array.isArray(value)) return `${value.length} items`;
  if (value && typeof value === "object") return "configured";
  return "-";
}

export function thesisId(row: RowRecord | undefined): string {
  return textField(row, ["thesis_id"], `${textField(row, ["ticker"])}-${textField(row, ["created_at"])}`);
}

export function validationForThesis(thesis: RowRecord | undefined, byThesis: Map<string, RowRecord>, byTicker: Map<string, RowRecord>): RowRecord | undefined {
  const id = thesisId(thesis);
  return byThesis.get(id) ?? byTicker.get(textField(thesis, ["ticker"]));
}

export function validationHistoryForThesis(thesis: RowRecord | undefined, byThesis: Map<string, RowRecord[]>, legacyByTicker: Map<string, RowRecord>): RowRecord[] {
  const id = thesisId(thesis);
  const rows = byThesis.get(id);
  if (rows?.length) return rows;
  const legacy = legacyByTicker.get(textField(thesis, ["ticker"]));
  return legacy ? [legacy] : [];
}

export function rows(table: TablePayload): RowRecord[] {
  return table.rows ?? [];
}

export const OPPORTUNITY_STATES = new Set(["FIRE", "SETUP", "WATCH", "HOLD", "TRIM"]);

export function isOpportunityCandidate(row: RowRecord): boolean {
  return OPPORTUNITY_STATES.has(stateOf(row));
}

export function uniqueText(items: RowRecord[], key: string): string[] {
  return Array.from(new Set(items.map((row) => textField(row, [key])).filter(Boolean)));
}

export function uniqueValues(items: string[]): string[] {
  return Array.from(new Set(items.filter(Boolean))).sort((left, right) => left.localeCompare(right));
}

export function candidateOpportunityFields(opportunity: RowRecord | undefined): RowRecord {
  if (!opportunity) return {};
  return {
    opportunity_tier: opportunity.tier,
    opportunity_conviction_score: opportunity.conviction_score,
    opportunity_data_contract_status: opportunity.data_contract_status,
    opportunity_id: opportunity.opportunity_id,
  };
}

export function candidateConviction(row: RowRecord): number {
  return numberField(row, ["opportunity_conviction_score", "conviction_score", "score"], Number.NEGATIVE_INFINITY);
}

// Human-readable contract label (e.g. "$845 Call") for the radar table, replacing
// the opaque contract_id UUID. Strike/option_type live in candidate_event.raw.
export function contractLabel(row: RowRecord): string {
  const raw = recordField(row, "raw");
  const strike = numberFromRecord(raw, "strike");
  const optionType = stringFromRecord(raw, "option_type", "call");
  const strikeText = Number.isFinite(strike) ? `$${formatNumber(strike, Number.isInteger(strike) ? 0 : 2)}` : "";
  const label = [strikeText, titleLabel(optionType)].filter(Boolean).join(" ");
  return label || textField(row, ["contract_id"], "Unknown contract");
}

export function candidateFamily(row: RowRecord): string {
  const raw = recordField(row, "raw");
  const rawFamily = stringFromRecord(raw, "strategy_family");
  if (rawFamily) return rawFamily;
  const version = textField(row, ["strategy_version"], "leap_10x_reversal_v1");
  return version.replace(/_v\d+$/, "");
}

export function countWhere(items: RowRecord[], predicate: (row: RowRecord) => boolean): number {
  return items.reduce((count, row) => count + (predicate(row) ? 1 : 0), 0);
}

export function optionThesisAgentState(data: PanelData): OptionThesisAgentRuntime {
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

export function stateOf(row: RowRecord | undefined): string {
  return textField(row, ["state"]).toUpperCase();
}

export function tierOf(row: RowRecord | undefined): string {
  return textField(row, ["tier"], "Watch");
}

export function qualityOf(row: RowRecord | undefined): QualityFilter {
  const quality = textField(row, ["quality_status"], "ok").toLowerCase();
  if (quality === "bad" || quality === "caution" || quality === "ok") return quality;
  return "ok";
}

export function outcomeMaturity(mark: RowRecord): { label: string; tone: Tone } {
  if (Number.isFinite(numberField(mark, ["return_20d"], Number.NaN))) return { label: "20D observed", tone: "good" };
  if (Number.isFinite(numberField(mark, ["return_5d"], Number.NaN))) return { label: "5D observed", tone: "good" };
  if (Number.isFinite(numberField(mark, ["return_1d"], Number.NaN))) return { label: "1D observed", tone: "info" };
  return { label: "Waiting <1D", tone: "warn" };
}

export function cohortHasMatureEvidence(row: RowRecord): boolean {
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

export function cohortDefinition(row: RowRecord): string {
  const raw = recordField(row, "raw");
  const definition = raw?.cohort_definition;
  return typeof definition === "string" && definition.trim() ? definition.trim() : `${displayField(row, ["cohort_type"])}=${displayField(row, ["cohort_value"])}`;
}

export function thesisState(row: RowRecord, requestByEvent: Map<string, RowRecord>): { kind: ThesisFilter; label: string; tone: Tone } {
  if (textField(row, ["thesis_id"])) return { kind: "attached", label: "Attached", tone: "good" };
  const request = requestByEvent.get(textField(row, ["event_id"]));
  if (request) {
    const status = textField(request, ["status"], "requested");
    return { kind: "requested", label: status.toLowerCase() === "open" ? "Requested" : titleLabel(status), tone: toneFromText(status) };
  }
  return { kind: "needs", label: "Needs thesis", tone: "warn" };
}

export function focusCandidateRows(rows: RowRecord[], focus: CandidateFocus): RowRecord[] {
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

export function compareCandidates(left: RowRecord, right: RowRecord, sort: CandidateSort): number {
  if (sort === "ticker-asc") return compareText(textField(left, ["ticker"]), textField(right, ["ticker"])) || compareScore(left, right);
  if (sort === "move-asc") return compareNumber(numberField(left, ["required_move_pct"], Number.POSITIVE_INFINITY), numberField(right, ["required_move_pct"], Number.POSITIVE_INFINITY)) || compareScore(left, right);
  if (sort === "premium-asc") return compareNumber(numberField(left, ["premium_mid"], Number.POSITIVE_INFINITY), numberField(right, ["premium_mid"], Number.POSITIVE_INFINITY)) || compareScore(left, right);
  if (sort === "expiry-asc") return compareText(stringFromRecord(recordField(left, "raw"), "expiration"), stringFromRecord(recordField(right, "raw"), "expiration")) || compareScore(left, right);
  if (sort === "state") return stateRank(stateOf(left)) - stateRank(stateOf(right)) || compareScore(left, right);
  return compareScore(left, right);
}

export function compareScore(left: RowRecord, right: RowRecord): number {
  return compareNumber(candidateConviction(right), candidateConviction(left));
}

export function compareNumber(left: number, right: number): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

export function compareText(left: string, right: string): number {
  return left.localeCompare(right);
}

export function investmentStateLabel(row: RowRecord): string {
  const tier = tierOf(row);
  const primaryState = textField(row, ["primary_state"], "watch").toUpperCase();
  if (isServiceRepair(row)) return "Data Blocked";
  if (tier === "Exceptional") return "Trade Ready";
  if (tier === "Research") return `${titleLabel(primaryState)} Research`;
  if (primaryState === "FIRE") return "Fire Watch";
  if (primaryState === "SETUP") return "Setup Watch";
  return titleLabel(primaryState || tier || "Watch");
}

export function investmentStateTone(row: RowRecord): Tone {
  if (isServiceRepair(row)) return "bad";
  const tier = tierOf(row);
  if (tier === "Exceptional") return "good";
  if (tier === "Research") return "info";
  return stateTone(textField(row, ["primary_state"]));
}

export function dataContractStatus(row: RowRecord | undefined): string {
  return textField(row, ["data_contract_status"], "").toLowerCase();
}

export function isServiceRepair(row: RowRecord | undefined): boolean {
  return dataContractStatus(row) === "repair_required" || tierOf(row) === "Service Bug";
}

export function dataContractFailures(row: RowRecord | undefined): string[] {
  return arrayText(row, "data_contract_failures");
}

export function readableReasonSummary(row: RowRecord): string {
  const raw = recordField(row, "raw");
  return [...listFromRecord(raw, "hard_rejects"), ...listFromRecord(raw, "blockers"), ...listFromRecord(raw, "positives")]
    .map(reasonLabel)
    .join(" ");
}

export function commonBlockers(rows: RowRecord[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const blocker of arrayText(row, "blockers")) {
      counts.set(blocker, (counts.get(blocker) ?? 0) + 1);
    }
  }
  return [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 6);
}

export function commonDataContractFailures(rows: RowRecord[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const failure of dataContractFailures(row)) {
      counts.set(failure, (counts.get(failure) ?? 0) + 1);
    }
  }
  return [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 6);
}


export function oldestDate(items: RowRecord[], key: string): string {
  let oldest = "";
  for (const item of items) {
    const value = textField(item, [key]);
    if (value && (!oldest || dateMillis(value) < dateMillis(oldest))) oldest = value;
  }
  return oldest;
}

