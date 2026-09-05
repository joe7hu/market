import type { DashboardPayload, KnownPanelTables, PanelData, RowRecord, ScopeSnapshotStatus, TablePayload } from "./types";

export const EMPTY_TABLE: TablePayload = { rows: [], count: 0 };

export type PanelSnapshotPayload = {
  scope?: string;
  status?: DashboardPayload["status"];
  dashboard?: DashboardPayload | null;
  tables?: Record<string, TablePayload>;
  portfolio_integrated?: import("./generated/apiSchema").components["schemas"]["PortfolioIntegratedDTO"] | null;
};

const TABLE_KEY_OVERRIDES: Record<string, keyof KnownPanelTables> = {
  ticker_memos: "memos",
};

const RESERVED_PANEL_KEYS = new Set(["dashboard", "settings", "errors"]);
const PHASE4_WORKSPACES = new Set(["today", "opportunities", "portfolio", "research", "health"]);

function tableKeyFor(apiKey: string): keyof KnownPanelTables | string {
  if (apiKey in TABLE_KEY_OVERRIDES) return TABLE_KEY_OVERRIDES[apiKey];
  return apiKey.replace(/_([a-z0-9])/g, (_, letter: string) => letter.toUpperCase());
}

export function emptyPanelData(): PanelData {
  return {
    dashboard: {},
    settings: {},
    errors: {},
    scopeStatus: {},
  } as PanelData;
}

export function mergeSnapshot(existing: PanelData, snapshot: PanelSnapshotPayload, options: { append?: boolean } = {}): PanelData {
  const incomingTables = phase4Identity(snapshot.tables);
  const incomingIntegrated = phase4IdentityFromIntegrated(snapshot.portfolio_integrated);
  const incomingHasPhase4 = incomingTables.state !== "absent" || incomingIntegrated.state !== "absent";
  if (incomingHasPhase4 && (incomingTables.state === "invalid" || incomingIntegrated.state === "invalid" ||
      (incomingTables.state === "valid" && incomingIntegrated.state === "valid" && incomingTables.value !== incomingIntegrated.value))) {
    return {
      ...existing,
      errors: { ...existing.errors, portfolio: "Invalid or missing Phase 4 snapshot identity." },
    };
  }
  const incomingPhase4 = incomingTables.state === "valid" ? incomingTables : incomingIntegrated;
  const existingPhase4 = phase4IdentityFromPanel(existing);
  if (snapshot.scope && PHASE4_WORKSPACES.has(snapshot.scope) && incomingPhase4.state === "absent") {
    const message = "Phase 4 identity is missing from this workspace response.";
    return {
      ...existing,
      errors: { ...existing.errors, portfolio: message },
      scopeStatus: { ...existing.scopeStatus, [snapshot.scope]: { state: "failed", error: message } },
    };
  }
  if (snapshot.scope && PHASE4_WORKSPACES.has(snapshot.scope) && existingPhase4.state === "valid" && incomingPhase4.state === "absent") {
    return {
      ...existing,
      errors: { ...existing.errors, portfolio: "Phase 4 identity is missing from this workspace response." },
      scopeStatus: { ...existing.scopeStatus, [snapshot.scope]: { state: "failed", error: "Phase 4 identity is missing from this workspace response." } },
    };
  }
  if (incomingPhase4.state === "valid" && existingPhase4.state === "valid" && incomingPhase4.value !== existingPhase4.value
      && !(snapshot.scope && PHASE4_WORKSPACES.has(snapshot.scope))) {
    return {
      ...existing,
      errors: { ...existing.errors, portfolio: "Phase 4 snapshot identity diverged; retained the prior immutable view." },
    };
  }
  const next: PanelData = { ...existing, errors: { ...existing.errors }, scopeStatus: { ...existing.scopeStatus } };
  const validScopedRollover = Boolean(snapshot.scope && PHASE4_WORKSPACES.has(snapshot.scope)
    && existingPhase4.state === "valid" && incomingPhase4.state === "valid"
    && existingPhase4.value !== incomingPhase4.value);
  if (validScopedRollover) {
    const reset = next as Record<string, unknown>;
    delete reset.portfolioScenarioArtifact;
    delete reset.executionModelSnapshot;
    delete reset.portfolioIntegrated;
  }
  if (snapshot.portfolio_integrated) next.portfolioIntegrated = snapshot.portfolio_integrated;
  if (snapshot.dashboard) {
    next.dashboard = snapshot.dashboard;
  } else if (snapshot.status) {
    next.dashboard = { ...next.dashboard, status: snapshot.status };
  }
  const deferred = snapshot.scope === "dashboard" ? deferredDashboardModels(snapshot) : new Set<string>();
  for (const [apiKey, table] of Object.entries(snapshot.tables ?? {})) {
    if (deferred.has(apiKey)) continue;
    const dataKey = tableKeyFor(apiKey);
    if (!RESERVED_PANEL_KEYS.has(dataKey)) {
      const existingTable = next[dataKey] as TablePayload | undefined;
      next[dataKey] = options.append ? appendTable(existingTable ?? EMPTY_TABLE, table ?? EMPTY_TABLE) : table ?? EMPTY_TABLE;
    }
  }
  if (snapshot.scope) {
    const metadata = snapshot.status?.metadata;
    const failure = typeof metadata?.snapshot_error === "string" ? metadata.snapshot_error : undefined;
    const stale = metadata?.snapshot_state === "stale";
    next.scopeStatus[snapshot.scope] = {
      state: failure ? (stale ? "stale" : "failed") : (stale ? "stale" : "ready"),
      message: snapshot.status?.message,
      error: failure,
      lastGoodAt: typeof metadata?.last_good_at === "string" ? metadata.last_good_at : undefined,
    };
  }
  return next;
}

function deferredDashboardModels(snapshot: PanelSnapshotPayload): Set<string> {
  const values = snapshot.status?.metadata?.dashboard_deferred_models;
  return Array.isArray(values)
    ? new Set(values.filter((value): value is string => typeof value === "string"))
    : new Set<string>();
}

type Phase4Identity = { state: "absent" | "valid" | "invalid"; value: string | null };

function phase4Identity(tables: Record<string, TablePayload> | undefined): Phase4Identity {
  const allocation = tables?.portfolio_allocation?.rows?.[0];
  const scenario = tables?.portfolio_scenario_artifact?.rows?.[0];
  const execution = tables?.execution_model_snapshot?.rows?.[0];
  if (!allocation && !scenario && !execution) return { state: "absent", value: null };
  if (!allocation || (scenario && (typeof scenario.scenario_artifact_id !== "string" || !scenario.scenario_artifact_id.trim())) ||
      (execution && (typeof execution.execution_model_snapshot_id !== "string" || !execution.execution_model_snapshot_id.trim()))) {
    return { state: "invalid", value: null };
  }
  const allocationId = typeof allocation.allocation_id === "string" && allocation.allocation_id.trim() ? allocation.allocation_id : null;
  if (!allocationId) return { state: "invalid", value: null };
  const canonical = allocation.canonical_portfolio && typeof allocation.canonical_portfolio === "object" && !Array.isArray(allocation.canonical_portfolio)
    ? allocation.canonical_portfolio as Record<string, unknown>
    : null;
  if (allocation.canonical_portfolio !== undefined && canonical === null) return { state: "invalid", value: null };
  if (canonical && canonical.allocation_id !== allocationId) return { state: "invalid", value: null };
  if (scenario?.allocation_id !== undefined && scenario.allocation_id !== allocationId) return { state: "invalid", value: null };
  if (execution?.allocation_id !== undefined && execution.allocation_id !== allocationId) return { state: "invalid", value: null };
  const scenarioId = typeof scenario?.scenario_artifact_id === "string" ? scenario.scenario_artifact_id :
    (canonical?.scenario_artifact_id as string | undefined) ?? "";
  const executionId = typeof execution?.execution_model_snapshot_id === "string" ? execution.execution_model_snapshot_id :
    (canonical?.execution_model_snapshot_id as string | undefined) ?? "";
  const canonicalScenario = canonical?.scenario && typeof canonical.scenario === "object" && !Array.isArray(canonical.scenario)
    ? canonical.scenario as Record<string, unknown> : null;
  const canonicalExecution = canonical?.execution && typeof canonical.execution === "object" && !Array.isArray(canonical.execution)
    ? canonical.execution as Record<string, unknown> : null;
  if ((canonical?.scenario !== undefined && canonical?.scenario !== null && canonicalScenario === null) ||
      (canonical?.execution !== undefined && canonical?.execution !== null && canonicalExecution === null)) {
    return { state: "invalid", value: null };
  }
  if (canonicalScenario && (typeof canonicalScenario.scenario_artifact_id !== "string" || !canonicalScenario.scenario_artifact_id.trim()) ||
      canonicalExecution && (typeof canonicalExecution.execution_model_snapshot_id !== "string" || !canonicalExecution.execution_model_snapshot_id.trim())) {
    return { state: "invalid", value: null };
  }
  if (canonicalScenario && (canonicalScenario.allocation_id !== allocationId || (scenarioId && canonicalScenario.scenario_artifact_id !== scenarioId))) return { state: "invalid", value: null };
  if (canonicalExecution && (canonicalExecution.allocation_id !== allocationId || (executionId && canonicalExecution.execution_model_snapshot_id !== executionId))) return { state: "invalid", value: null };
  return { state: "valid", value: `${allocationId}|${scenarioId}|${executionId}` };
}

function phase4IdentityFromIntegrated(integrated: PanelSnapshotPayload["portfolio_integrated"]): Phase4Identity {
  if (!integrated) return { state: "absent", value: null };
  if (typeof integrated.allocation_id !== "string" || !integrated.allocation_id.trim()) return { state: "invalid", value: null };
  if (integrated.scenario && integrated.scenario.allocation_id !== integrated.allocation_id) return { state: "invalid", value: null };
  if (integrated.execution && integrated.execution.allocation_id !== integrated.allocation_id) return { state: "invalid", value: null };
  if (integrated.scenario_artifact_id !== null && (typeof integrated.scenario_artifact_id !== "string" || !integrated.scenario_artifact_id.trim()) ||
      integrated.execution_model_snapshot_id !== null && (typeof integrated.execution_model_snapshot_id !== "string" || !integrated.execution_model_snapshot_id.trim())) {
    return { state: "invalid", value: null };
  }
  const scenarioId = typeof integrated.scenario_artifact_id === "string" ? integrated.scenario_artifact_id : "";
  const executionId = typeof integrated.execution_model_snapshot_id === "string" ? integrated.execution_model_snapshot_id : "";
  if (integrated.scenario && integrated.scenario.scenario_artifact_id !== scenarioId) return { state: "invalid", value: null };
  if (integrated.execution && integrated.execution.execution_model_snapshot_id !== executionId) return { state: "invalid", value: null };
  return { state: "valid", value: `${integrated.allocation_id}|${scenarioId}|${executionId}` };
}

function phase4IdentityFromPanel(data: PanelData): Phase4Identity {
  const tables = phase4Identity({
    portfolio_allocation: data.portfolioAllocation,
    portfolio_scenario_artifact: data.portfolioScenarioArtifact,
    execution_model_snapshot: data.executionModelSnapshot,
  });
  const integrated = phase4IdentityFromIntegrated(data.portfolioIntegrated);
  if (tables.state === "invalid" || integrated.state === "invalid") return { state: "invalid", value: null };
  if (tables.state === "valid" && integrated.state === "valid" && tables.value !== integrated.value) return { state: "invalid", value: null };
  return tables.state === "valid" ? tables : integrated;
}

export function mergePanelData(existing: PanelData, incoming: PanelData, options: { append?: boolean } = {}): PanelData {
  const existingPhase4 = phase4IdentityFromPanel(existing);
  const incomingPhase4 = phase4IdentityFromPanel(incoming);
  const phase4Scope = Object.keys(incoming.scopeStatus ?? {}).find((scope) => PHASE4_WORKSPACES.has(scope));
  if (incomingPhase4.state === "invalid" || (phase4Scope && incomingPhase4.state === "absent")) {
    const message = "Invalid or missing Phase 4 snapshot identity.";
    return {
      ...existing,
      errors: { ...existing.errors, portfolio: message },
      scopeStatus: { ...existing.scopeStatus, ...(phase4Scope ? { [phase4Scope]: { state: "failed", error: message } } : {}) },
    };
  }
  if (existingPhase4.state === "valid" && incomingPhase4.state === "valid" && existingPhase4.value !== incomingPhase4.value && !phase4Scope) {
    return { ...existing, errors: { ...existing.errors, portfolio: "Phase 4 snapshot identity diverged; retained the prior immutable view." } };
  }
  const validScopedRollover = Boolean(phase4Scope && existingPhase4.state === "valid" && incomingPhase4.state === "valid"
    && existingPhase4.value !== incomingPhase4.value);
  const next: PanelData = {
    ...existing,
    dashboard: { ...existing.dashboard, ...incoming.dashboard },
    settings: { ...existing.settings, ...incoming.settings },
    errors: { ...existing.errors, ...incoming.errors },
    scopeStatus: { ...existing.scopeStatus, ...incoming.scopeStatus },
  };
  if (validScopedRollover) {
    for (const key of ["portfolioAllocation", "portfolioAllocationItems", "portfolioScenarioArtifact", "executionModelSnapshot", "paperExecutionObservations", "bookAttribution", "portfolioIntegrated"]) {
      delete (next as Record<string, unknown>)[key];
    }
  }
  for (const [key, value] of Object.entries(incoming)) {
    if (RESERVED_PANEL_KEYS.has(key) || key === "scopeStatus" || value === undefined) continue;
    const existingTable = next[key] as TablePayload | undefined;
    next[key] = options.append && isTablePayload(existingTable) && isTablePayload(value)
      ? appendTable(existingTable, value)
      : value;
  }
  return next;
}

export function withScopeStatus(data: PanelData, scope: string, status: ScopeSnapshotStatus): PanelData {
  return { ...data, scopeStatus: { ...data.scopeStatus, [scope]: status } };
}

function appendTable(existing: TablePayload, incoming: TablePayload): TablePayload {
  const existingRows = existing.rows ?? [];
  const incomingRows = incoming.rows ?? [];
  return {
    ...incoming,
    rows: appendUniqueRows(existingRows, incomingRows),
    count: incoming.count ?? existing.count,
  };
}

function isTablePayload(value: unknown): value is TablePayload {
  return typeof value === "object" && value !== null && "rows" in value;
}

function appendUniqueRows(existingRows: RowRecord[], incomingRows: RowRecord[]): RowRecord[] {
  const output = existingRows.slice();
  const seen = new Set(output.map(rowKey));
  for (const row of incomingRows) {
    const key = rowKey(row);
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(row);
  }
  return output;
}

function rowKey(row: RowRecord): string {
  const symbol = String(row.symbol ?? row.ticker ?? "");
  const qualifier = String(row.method ?? row.source ?? row.source_key ?? row.id ?? row.date ?? row.as_of ?? "");
  return symbol || qualifier ? `${symbol}:${qualifier}` : JSON.stringify(row);
}
