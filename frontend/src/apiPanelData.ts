import type { DashboardPayload, KnownPanelTables, PanelData, RowRecord, ScopeSnapshotStatus, TablePayload } from "./types";

export const EMPTY_TABLE: TablePayload = { rows: [], count: 0 };

export type PanelSnapshotPayload = {
  scope?: string;
  status?: DashboardPayload["status"];
  dashboard?: DashboardPayload | null;
  tables?: Record<string, TablePayload>;
};

const TABLE_KEY_OVERRIDES: Record<string, keyof KnownPanelTables> = {
  ticker_memos: "memos",
};

const RESERVED_PANEL_KEYS = new Set(["dashboard", "settings", "errors"]);

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
  const incomingPhase4 = phase4Identity(snapshot.tables);
  const existingPhase4 = phase4IdentityFromPanel(existing);
  if (incomingPhase4 && existingPhase4 && incomingPhase4 !== existingPhase4) {
    return {
      ...existing,
      errors: { ...existing.errors, portfolio: "Phase 4 snapshot identity diverged; retained the prior immutable view." },
    };
  }
  const next: PanelData = { ...existing, errors: { ...existing.errors }, scopeStatus: { ...existing.scopeStatus } };
  if (snapshot.dashboard) {
    next.dashboard = snapshot.dashboard;
  } else if (snapshot.status) {
    next.dashboard = { ...next.dashboard, status: snapshot.status };
  }
  for (const [apiKey, table] of Object.entries(snapshot.tables ?? {})) {
    const dataKey = tableKeyFor(apiKey);
    if (!RESERVED_PANEL_KEYS.has(dataKey)) {
      const existingTable = next[dataKey] as TablePayload | undefined;
      next[dataKey] = options.append ? appendTable(existingTable ?? EMPTY_TABLE, table ?? EMPTY_TABLE) : table ?? EMPTY_TABLE;
    }
  }
  if (snapshot.scope) {
    const metadata = snapshot.status?.metadata;
    const stale = metadata?.snapshot_state === "stale";
    next.scopeStatus[snapshot.scope] = {
      state: stale ? "stale" : "ready",
      message: snapshot.status?.message,
      error: typeof metadata?.snapshot_error === "string" ? metadata.snapshot_error : undefined,
      lastGoodAt: typeof metadata?.last_good_at === "string" ? metadata.last_good_at : undefined,
    };
  }
  return next;
}

function phase4Identity(tables: Record<string, TablePayload> | undefined): string | null {
  const allocation = tables?.portfolio_allocation?.rows?.[0];
  if (!allocation) return null;
  const allocationId = typeof allocation.allocation_id === "string" ? allocation.allocation_id : null;
  const canonical = allocation.canonical_portfolio && typeof allocation.canonical_portfolio === "object" && !Array.isArray(allocation.canonical_portfolio)
    ? allocation.canonical_portfolio as Record<string, unknown>
    : null;
  if (canonical && canonical.allocation_id !== allocationId) return null;
  const scenario = tables?.portfolio_scenario_artifact?.rows?.[0];
  const execution = tables?.execution_model_snapshot?.rows?.[0];
  const scenarioId = typeof scenario?.scenario_artifact_id === "string" ? scenario.scenario_artifact_id : "";
  const executionId = typeof execution?.execution_model_snapshot_id === "string" ? execution.execution_model_snapshot_id : "";
  const canonicalScenario = canonical?.scenario && typeof canonical.scenario === "object" && !Array.isArray(canonical.scenario)
    ? canonical.scenario as Record<string, unknown> : null;
  const canonicalExecution = canonical?.execution && typeof canonical.execution === "object" && !Array.isArray(canonical.execution)
    ? canonical.execution as Record<string, unknown> : null;
  if (canonicalScenario && (canonicalScenario.allocation_id !== allocationId || (scenarioId && canonicalScenario.scenario_artifact_id !== scenarioId))) return null;
  if (canonicalExecution && (canonicalExecution.allocation_id !== allocationId || (executionId && canonicalExecution.execution_model_snapshot_id !== executionId))) return null;
  return allocationId ? `${allocationId}|${scenarioId}|${executionId}` : null;
}

function phase4IdentityFromPanel(data: PanelData): string | null {
  return phase4Identity({
    portfolio_allocation: data.portfolioAllocation,
    portfolio_scenario_artifact: data.portfolioScenarioArtifact,
    execution_model_snapshot: data.executionModelSnapshot,
  });
}

export function mergePanelData(existing: PanelData, incoming: PanelData, options: { append?: boolean } = {}): PanelData {
  const next: PanelData = {
    ...existing,
    dashboard: { ...existing.dashboard, ...incoming.dashboard },
    settings: { ...existing.settings, ...incoming.settings },
    errors: { ...existing.errors, ...incoming.errors },
    scopeStatus: { ...existing.scopeStatus, ...incoming.scopeStatus },
  };
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
