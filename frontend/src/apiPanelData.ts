import type { DashboardPayload, KnownPanelTables, PanelData, RowRecord, TablePayload } from "./types";

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
  } as PanelData;
}

export function mergeSnapshot(existing: PanelData, snapshot: PanelSnapshotPayload, options: { append?: boolean } = {}): PanelData {
  const next: PanelData = { ...existing, errors: { ...existing.errors } };
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
  return next;
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
