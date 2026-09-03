import type { PanelData } from "@/types";

export type JsonObject = Record<string, unknown>;

export type Phase4Action = {
  allocationId: string;
  allocationItemId: string;
  ticker: string;
  disposition: string;
  forecastId: string | null;
  actionId: string | null;
  rankId: string | null;
  expression: JsonObject | null;
  invalidation: JsonObject | null;
  why_trade: string | null;
  why_now?: string[];
  missingData: string[];
  blockers: string[];
  targetWeight: number | null;
  currentWeight: number | null;
  marginalBookUtility: number | null;
  currentMrc: number | null;
  proposedMrc: number | null;
  fundingSource: string | null;
  sizingTrace: JsonObject;
};

export type PortfolioActionDTO = {
  allocation_id: string;
  allocation_item_id: string;
  ticker: string;
  disposition: string;
  strategy_forecast_id: string | null;
  action_id: string | null;
  rank_id: string | null;
  expression: JsonObject | null;
  invalidation: JsonObject | null;
  why_trade: string | null;
  why_now?: string[];
  missing_data: string[];
  blockers: string[];
  target_weight: number | null;
  current_weight: number | null;
  marginal_book_utility: number | null;
  current_mrc: number | null;
  proposed_mrc: number | null;
  funding_source: string | null;
  sizing_trace: JsonObject;
};

export type PortfolioIntegratedDTO = {
  allocation_id: string;
  input_cutoff: string;
  status: string;
  actions: PortfolioActionDTO[];
  scenario_artifact_id: string | null;
  execution_model_snapshot_id: string | null;
  scenario: {
    scenario_artifact_id: string;
    allocation_id: string;
    scenarios: JsonObject[];
    tail_dependence: JsonObject;
    simultaneous_unwind: JsonObject;
  } | null;
  execution: {
    execution_model_snapshot_id: string;
    allocation_id: string;
    calibration_status: string;
    sample_count: number;
  } | null;
  attribution_count: number;
  postmortem?: JsonObject[];
};

export type Phase4Scenario = {
  artifactId: string;
  scenarios: JsonObject[];
  tailDependence: JsonObject;
  simultaneousUnwind: JsonObject;
};

export type Phase4Decision = {
  allocationId: string;
  inputCutoff: string | null;
  status: string;
  actions: Phase4Action[];
  scenario: Phase4Scenario | null;
  execution: { snapshotId: string; calibrationStatus: string; sampleCount: number | null } | null;
  attributionCount: number;
  postmortem: JsonObject[];
};

function object(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : null;
}

function rows(value: unknown): unknown[] {
  const table = object(value);
  return Array.isArray(table?.rows) ? table.rows : [];
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function actionFromRow(canonical: JsonObject): Phase4Action | null {
  const itemId = text(canonical.allocation_item_id);
  const ticker = text(canonical.ticker);
  const allocationId = text(canonical.allocation_id);
  const trace = object(canonical.sizing_trace);
  if (!itemId || !ticker || !allocationId || !trace) return null;
  return {
    allocationId,
    allocationItemId: itemId,
    ticker,
    disposition: text(canonical.disposition) ?? "rejected",
    forecastId: text(canonical.strategy_forecast_id),
    actionId: text(canonical.action_id),
    rankId: text(canonical.rank_id),
    expression: object(canonical.expression),
    invalidation: object(canonical.invalidation),
    why_trade: text(canonical.why_trade),
    why_now: strings(canonical.why_now),
    missingData: strings(canonical.missing_data),
    blockers: strings(canonical.blockers),
    targetWeight: number(canonical.target_weight),
    currentWeight: number(canonical.current_weight),
    marginalBookUtility: number(canonical.marginal_book_utility),
    currentMrc: number(canonical.current_mrc),
    proposedMrc: number(canonical.proposed_mrc),
    fundingSource: text(canonical.funding_source),
    sizingTrace: trace,
  };
}

function isPortfolioActionDTO(value: unknown): value is PortfolioActionDTO {
  const row = object(value);
  return Boolean(
    row && text(row.allocation_id) && text(row.allocation_item_id) && text(row.ticker)
    && text(row.disposition) && object(row.sizing_trace)
    && Array.isArray(row.missing_data) && Array.isArray(row.blockers)
    && (row.why_now === undefined || Array.isArray(row.why_now))
    && (row.why_trade === undefined || row.why_trade === null || typeof row.why_trade === "string")
    && (row.target_weight === null || number(row.target_weight) !== null)
    && (row.current_weight === null || number(row.current_weight) !== null)
    && (row.marginal_book_utility === null || number(row.marginal_book_utility) !== null)
    && (row.current_mrc === null || number(row.current_mrc) !== null)
    && (row.proposed_mrc === null || number(row.proposed_mrc) !== null)
  );
}

export function buildPortfolioPhase4Decision(data: PanelData): Phase4Decision | null {
  const allocationRow = object(rows(data.portfolioAllocation)[0]);
  if (!allocationRow) return null;
  const allocationId = text(allocationRow.allocation_id);
  if (!allocationId) return null;
  const canonical = object(allocationRow.canonical_portfolio) as PortfolioIntegratedDTO | null;
  if (!canonical || canonical.allocation_id !== allocationId || !Array.isArray(canonical.actions) || !canonical.input_cutoff || !canonical.status) return null;
  const canonicalActions = canonical.actions.filter(isPortfolioActionDTO);
  if (canonicalActions.length !== canonical.actions.length) return null;
  const actions = canonicalActions.map((row) => actionFromRow(row)).filter((row): row is Phase4Action => row !== null);
  if (actions.length !== canonicalActions.length || actions.some((action) => action.allocationId !== allocationId)) return null;
  if (canonical.scenario && canonical.scenario.allocation_id !== allocationId) return null;
  const scenario = canonical.scenario && text(canonical.scenario.scenario_artifact_id) ? {
    artifactId: canonical.scenario.scenario_artifact_id,
    scenarios: canonical.scenario.scenarios,
    tailDependence: canonical.scenario.tail_dependence,
    simultaneousUnwind: canonical.scenario.simultaneous_unwind,
  } : null;
  if (canonical.execution && canonical.execution.allocation_id !== allocationId) return null;
  const execution = canonical.execution && text(canonical.execution.execution_model_snapshot_id) ? {
    snapshotId: canonical.execution.execution_model_snapshot_id,
    calibrationStatus: text(canonical.execution.calibration_status) ?? "unavailable",
    sampleCount: number(canonical.execution.sample_count),
  } : null;
  return {
    allocationId,
    inputCutoff: canonical.input_cutoff,
    status: canonical.status,
    actions,
    scenario,
    execution,
    attributionCount: canonical.attribution_count,
    postmortem: canonical.postmortem ?? [],
  };
}
