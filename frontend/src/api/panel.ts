/** Panel, read-model, settings, source, ticker, and refresh requests. */

import type { components } from "../generated/apiSchema";
import { emptyPanelData, mergeSnapshot, type PanelSnapshotPayload } from "../apiPanelData";
import type { DashboardPayload, PanelData, RowRecord, TablePayload, TickerDossier } from "../types";
import { getJson, patchJson, sendJson } from "../apiTransport";

type ApiSchema = components["schemas"];

export type RefreshJob = ApiSchema["RefreshJobResponse"] & {
  id?: string;
  job_name?: string;
  status?: string;
  started_at?: string;
  finished_at?: string | null;
  error?: string | null;
};
export type RefreshLatestStatus = {
  ok?: boolean;
  status?: string;
  dataOk?: boolean;
  dataFinishedAt?: string | null;
  startedAt?: string;
  finishedAt?: string;
  failedStep?: string | null;
  job?: string;
  host?: string;
};
export type RefreshJobsPayload = {
  rows: RefreshJob[];
  count: number;
  allowlist: string[];
  latest_status: RefreshLatestStatus | null;
};
export type SourceCatalogPayload = ApiSchema["SourceCatalogResponse"];
export type SourceCatalogRow = {
  source_id: string;
  source_name: string;
  source_family: string;
  source_kind: string;
  operational_group: string;
  operational_state: "active" | "standby" | "archived" | string;
  enabled: boolean;
  ingestion_mode: string;
  effective_status: string;
  refresh_job: string;
  refresh_jobs: string[];
  run_status: string;
  freshness_status: string;
  latest_capability: string;
  source_url: string;
  health_owner: string;
  freshness_seconds: number | null;
  next_due_at: string | null;
  last_attempt_at: string | null;
  status_at: string | null;
  last_success_at: string | null;
  last_data_at: string | null;
  item_count: number;
  ticker_count: number;
  failure_detail: string;
  remediation: string;
  inherited_check: boolean;
  cadence_label: string;
  capability_health: Array<{ capability: string; status: string; finished_at: string | null; failure_detail: string }>;
};
export type SettingsPayload = ApiSchema["SettingsResponse"];
export type TickerPayload = ApiSchema["TickerDetailResponse"] & { dossier: TickerDossier };
export type PanelScopeOptions = {
  offset?: number;
  limit?: number;
  append?: boolean;
  force?: boolean;
};
export type AgentSettingsInput = ApiSchema["AgentSettingsInput"];
export type ResearchSourcesInput = ApiSchema["ResearchSourcesInput"];

export { emptyPanelData } from "../apiPanelData";

export async function loadPanelData(): Promise<PanelData> {
  return loadPanelScope("feed");
}

export async function loadPanelScope(
  scope: string,
  existing?: PanelData,
  options: PanelScopeOptions = {},
): Promise<PanelData> {
  const params = new URLSearchParams({ scope });
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  const snapshot = await getJson<ApiSchema["PanelSnapshotResponse"]>(`/api/panel-snapshot?${params.toString()}`);
  const data = mergeSnapshot(existing ?? emptyPanelData(), {
    scope: snapshot.scope,
    status: snapshot.status as unknown as DashboardPayload["status"],
    dashboard: snapshot.dashboard as unknown as DashboardPayload | null | undefined,
    tables: snapshot.tables as unknown as Record<string, TablePayload> | undefined,
  }, options);
  if (scope === "settings") data.settings = await loadSettings();
  return data;
}

export async function loadEventScoutSnapshot(signal?: AbortSignal): Promise<PanelSnapshotPayload> {
  const payload = await getJson<ApiSchema["EventScoutEventsResponse"]>("/api/event-scout", signal);
  return {
    scope: "event-scout",
    status: payload.status as unknown as DashboardPayload["status"],
    tables: payload.tables as unknown as Record<string, TablePayload>,
  };
}

export async function loadSuperinvestorPortfolio(
  investorKey: string,
  signal?: AbortSignal,
): Promise<ApiSchema["SuperinvestorDetailResponse"]> {
  return getJson(`/api/superinvestors/${encodeURIComponent(investorKey)}`, signal);
}

export async function loadTicker(symbol: string): Promise<TickerPayload> {
  return getJson<TickerPayload>(`/api/tickers/${encodeURIComponent(symbol)}`);
}

export async function loadSourceCatalog(): Promise<SourceCatalogPayload> {
  return getJson<SourceCatalogPayload>("/api/source-catalog");
}

export async function loadSettings(): Promise<SettingsPayload> {
  return getJson<SettingsPayload>("/api/settings");
}

export async function startRefreshJob(jobName: string): Promise<RefreshJob> {
  const payload = await sendJson<ApiSchema["RefreshJobResponse"]>(`/api/refresh-jobs/${encodeURIComponent(jobName)}/background`, "POST");
  return normalizeRefreshJob(payload);
}

export async function loadRefreshJobs(): Promise<RefreshJobsPayload> {
  const payload = await getJson<ApiSchema["RefreshJobsResponse"]>("/api/refresh-jobs");
  return normalizeRefreshJobs(payload);
}

export async function updateAgentSettings(payload: AgentSettingsInput): Promise<SettingsPayload> {
  return patchJson<SettingsPayload>("/api/settings/agents", payload);
}

export async function updateResearchSources(payload: ResearchSourcesInput): Promise<SettingsPayload> {
  return patchJson<SettingsPayload>("/api/settings/research-sources", payload);
}

export async function acknowledgeRadarAlert(alertId: string): Promise<ApiSchema["RadarAlertAcknowledgementResponse"]> {
  return sendJson<ApiSchema["RadarAlertAcknowledgementResponse"]>(
    `/api/radar-alerts/${encodeURIComponent(alertId)}/ack`,
    "POST",
  );
}

export async function loadQuotes(symbols: string[], signal?: AbortSignal): Promise<ApiSchema["QuotesResponse"]> {
  const query = symbols.map((symbol) => symbol.trim()).filter(Boolean).join(",");
  return getJson<ApiSchema["QuotesResponse"]>(`/api/quotes?symbols=${encodeURIComponent(query)}`, signal);
}

export type TickerDecisionSnapshot = ApiSchema["TickerDecisionSnapshotResponse"];
export type SuperinvestorDetail = ApiSchema["SuperinvestorDetailResponse"];
export type SourceDetail = ApiSchema["SourceDetailResponse"];
export type SourceAudit = ApiSchema["SourceAuditResponse"];
export type PanelRow = RowRecord;

function normalizeRefreshJob(payload: ApiSchema["RefreshJobResponse"]): RefreshJob {
  return {
    ...payload,
    id: payload.id ?? undefined,
    job_name: payload.job_name ?? undefined,
    status: payload.status ?? undefined,
    started_at: payload.started_at ?? undefined,
  };
}

function normalizeRefreshJobs(payload: ApiSchema["RefreshJobsResponse"]): RefreshJobsPayload {
  const rawStatus = payload.latest_status as (ApiSchema["RefreshLatestStatusResponse"] & Record<string, unknown>) | null | undefined;
  return {
    rows: (payload.rows ?? []).map(normalizeRefreshJob),
    count: payload.count,
    allowlist: payload.allowlist ?? [],
    latest_status: rawStatus ? {
      ok: rawStatus.ok ?? booleanValue(rawStatus.ok),
      status: rawStatus.status ?? stringValue(rawStatus.status),
      dataOk: booleanValue(rawStatus.data_ok ?? rawStatus.dataOk),
      dataFinishedAt: stringValue(rawStatus.data_finished_at ?? rawStatus.dataFinishedAt) || null,
      startedAt: stringValue(rawStatus.started_at ?? rawStatus.startedAt) || undefined,
      finishedAt: stringValue(rawStatus.finished_at ?? rawStatus.finishedAt) || undefined,
      failedStep: stringValue(rawStatus.failed_step ?? rawStatus.failedStep) || null,
      job: stringValue(rawStatus.job) || undefined,
      host: stringValue(rawStatus.host) || undefined,
    } : null,
  };
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
