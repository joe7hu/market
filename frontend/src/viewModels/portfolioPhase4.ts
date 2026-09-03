import type { PanelData } from "@/types";

type JsonObject = Record<string, unknown>;

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

function actionFromRow(row: JsonObject, allocationId: string): Phase4Action | null {
  const canonical = row;
  const itemId = text(canonical.allocation_item_id);
  const ticker = text(canonical.ticker);
  const trace = object(canonical.sizing_trace);
  if (!itemId || !ticker || !trace) return null;
  return {
    allocationId: text(canonical.allocation_id) ?? allocationId,
    allocationItemId: itemId,
    ticker,
    disposition: text(canonical.disposition) ?? text(row.disposition) ?? "rejected",
    forecastId: text(canonical.strategy_forecast_id) ?? text(row.strategy_forecast_id),
    actionId: text(canonical.action_id) ?? text(row.action_id),
    rankId: text(canonical.rank_id),
    expression: object(canonical.expression),
    invalidation: object(canonical.invalidation),
    missingData: strings(canonical.missing_data),
    blockers: strings(canonical.blockers),
    targetWeight: number(canonical.target_weight),
    currentWeight: number(canonical.current_weight),
    marginalBookUtility: number(canonical.marginal_book_utility),
    currentMrc: number(trace.current_marginal_risk_contribution),
    proposedMrc: number(trace.proposed_marginal_risk_contribution),
    fundingSource: text(canonical.funding_source),
    sizingTrace: trace,
  };
}

export function buildPortfolioPhase4Decision(data: PanelData): Phase4Decision | null {
  const allocationRow = object(rows(data.portfolioAllocation)[0]);
  if (!allocationRow) return null;
  const allocationId = text(allocationRow.allocation_id);
  if (!allocationId) return null;
  const canonical = object(allocationRow.canonical_portfolio);
  if (!canonical || canonical.allocation_id !== allocationId || !Array.isArray(canonical.actions)) return null;
  const canonicalActions = canonical.actions;
  const actions = canonicalActions.map((row) => actionFromRow(object(row) ?? {}, allocationId)).filter((row): row is Phase4Action => row !== null);
  if (actions.length !== canonicalActions.length || actions.some((action) => action.allocationId !== allocationId)) return null;
  const scenarioRow = object(rows(data.portfolioScenarioArtifact)[0]);
  if (scenarioRow && text(scenarioRow.allocation_id) !== allocationId) return null;
  const scenario = scenarioRow && text(scenarioRow.scenario_artifact_id) ? {
    artifactId: text(scenarioRow.scenario_artifact_id) as string,
    scenarios: Array.isArray(scenarioRow.scenarios) ? scenarioRow.scenarios.map((row) => object(row)).filter((row): row is JsonObject => row !== null) : [],
    tailDependence: object(scenarioRow.tail_dependence) ?? {},
    simultaneousUnwind: object(scenarioRow.simultaneous_unwind) ?? {},
  } : null;
  const executionRow = object(rows(data.executionModelSnapshot)[0]);
  if (executionRow && text(executionRow.allocation_id) !== allocationId) return null;
  const execution = executionRow && text(executionRow.execution_model_snapshot_id) ? {
    snapshotId: text(executionRow.execution_model_snapshot_id) as string,
    calibrationStatus: text(executionRow.calibration_status) ?? "unavailable",
    sampleCount: number(executionRow.sample_count),
  } : null;
  return {
    allocationId,
    inputCutoff: text(allocationRow.input_cutoff),
    status: text(allocationRow.status) ?? "unavailable",
    actions,
    scenario,
    execution,
    attributionCount: rows(data.bookAttribution).length,
  };
}
