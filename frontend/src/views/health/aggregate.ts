import { type RefreshJob } from "@/api";
import type { PanelData, RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import { displayField, numberField, titleLabel, toneFromText } from "@/views/rowFormat";
import {
  jobDef,
  sourceFamilyDef,
  toneRank,
  worstTone,
  type FamilyHealth,
  type SourceFamilyId,
} from "@/views/health/dataFlow";
import {
  CATEGORY_BY_TYPE,
  CONTENT_TYPES,
  DIR_FAMILY_CATEGORY,
  INGESTION_RUNS_CATEGORY,
  OTHER_CATEGORY,
  type AgentPipeline,
  type AgentRuntime,
  type Category,
  type CategoryDef,
  type DirEntry,
  type ErrorAgg,
  type JobState,
  type ProviderStat,
} from "@/views/health/types";
import {
  baseProvider,
  booleanFromJson,
  booleanValue,
  dateMs,
  freshnessTone,
  jsonRecord,
  normKey,
  numberFromJson,
  stringFromJson,
  truncate,
} from "@/views/health/format";

export function latestByJob(jobRows: RefreshJob[]): Record<string, JobState> {
  const out: Record<string, JobState> = {};
  for (const row of jobRows) {
    const name = row.job_name;
    if (!name) continue;
    const existing = out[name];
    if (!existing || dateMs(row.started_at ?? "") > dateMs(existing.startedAt ?? "")) {
      out[name] = { status: row.status ?? "", startedAt: row.started_at, finishedAt: row.finished_at, error: row.error };
    }
  }
  return out;
}

export function buildCategories(data: PanelData): Category[] {
  // Accumulate per category -> per provider.
  const cats = new Map<string, { def: CategoryDef; providers: Map<string, ProviderStat>; origins: Set<string> }>();

  const record = (
    def: CategoryDef,
    provider: string,
    tone: Tone,
    status: string,
    latestAt: string,
    detail: string,
    origin: string,
    counts: { items?: number; signals?: number; tickers?: number } = {},
  ) => {
    let cat = cats.get(def.id);
    if (!cat) {
      cat = { def, providers: new Map(), origins: new Set() };
      cats.set(def.id, cat);
    }
    cat.origins.add(origin);
    const key = baseProvider(provider);
    const stat =
      cat.providers.get(key) ??
      ({ provider: key, tone: "good", status, checks: 0, fresh: 0, stale: 0, failed: 0, latestAt: "", detail: "", items: 0, signals: 0, tickers: 0 } satisfies ProviderStat);
    stat.checks += 1;
    if (tone === "bad") stat.failed += 1;
    else if (tone === "warn") stat.stale += 1;
    else if (tone === "good" || tone === "info") stat.fresh += 1;
    if (toneRank(tone) < toneRank(stat.tone)) {
      stat.tone = tone;
      stat.status = status;
      if (detail) stat.detail = detail;
    } else if (!stat.detail && detail) {
      stat.detail = detail;
    }
    if (dateMs(latestAt) > dateMs(stat.latestAt)) stat.latestAt = latestAt;
    stat.items = Math.max(stat.items, counts.items ?? 0);
    stat.signals = Math.max(stat.signals, counts.signals ?? 0);
    stat.tickers = Math.max(stat.tickers, counts.tickers ?? 0);
    cat.providers.set(key, stat);
  };

  // Index the followed-source directory up front so freshness rows for the same
  // source land in one content category and pick up contribution stats.
  const dirIndex = new Map<string, DirEntry>();
  for (const row of rows(data.sources)) {
    const name = displayField(row, ["source_name", "source"], "");
    if (!name) continue;
    const family = displayField(row, ["source_family", "content_type"], "");
    const enabled = booleanValue(row.enabled) || booleanValue(row.is_followed);
    dirIndex.set(normKey(name), {
      name,
      def: DIR_FAMILY_CATEGORY[family] ?? INGESTION_RUNS_CATEGORY,
      items: numberField(row, ["items_count", "item_count"], 0),
      signals: numberField(row, ["signals_count", "signal_count"], 0),
      tickers: numberField(row, ["tickers_count", "ticker_count"], 0),
      status: displayField(row, ["latest_run_status", "freshness", "health", "status"], enabled ? "not_loaded" : "disabled"),
      latestAt: displayField(row, ["latest_run_at", "latest_at", "checked_at"], ""),
      detail: displayField(row, ["latest_failure_detail", "notes"], ""),
    });
  }
  const matchedDir = new Set<string>();

  // Source freshness — the bulk of the pipeline health.
  for (const row of rows(data.sourceFreshness)) {
    const type = displayField(row, ["source_type"], "");
    const provider = displayField(row, ["provider", "source_key", "source"], "unknown");
    // Only content-source freshness rows fold into the directory category.
    const dir = CONTENT_TYPES.has(type) ? dirIndex.get(normKey(baseProvider(provider))) : undefined;
    if (dir) matchedDir.add(normKey(baseProvider(provider)));
    const status = displayField(row, ["freshness_status", "status"], "not_loaded");
    record(
      dir ? dir.def : CATEGORY_BY_TYPE[type] ?? OTHER_CATEGORY,
      provider,
      freshnessTone(displayField(row, ["freshness_status"], ""), displayField(row, ["status"], "")),
      status,
      displayField(row, ["last_observed_at", "checked_at"], ""),
      displayField(row, ["detail"], ""),
      "freshness",
      dir ? { items: dir.items, signals: dir.signals, tickers: dir.tickers } : {},
    );
  }

  // Source health — upstream provider reachability. Verified-docs markers go to
  // Documentation; everything else is provider reachability. Per-symbol probes
  // (e.g. "yfinance:ZENA") collapse to their base provider inside record().
  for (const row of rows(data.sourceHealth)) {
    const provider = displayField(row, ["source"], "unknown");
    const status = displayField(row, ["status"], "not_loaded");
    const def = status === "verified_docs" ? CATEGORY_BY_TYPE.documentation : CATEGORY_BY_TYPE.provider_health;
    record(
      def,
      provider,
      toneFromText(status),
      status,
      displayField(row, ["checked_at"], ""),
      displayField(row, ["detail"], ""),
      "health",
    );
  }

  // Followed sources with no matching freshness row yet — add them once, into the
  // same shared category, so the directory never duplicates a freshness entry.
  for (const [key, entry] of dirIndex) {
    if (matchedDir.has(key)) continue;
    record(
      entry.def,
      entry.name,
      toneFromText(entry.status),
      entry.status,
      entry.latestAt,
      entry.detail,
      "directory",
      { items: entry.items, signals: entry.signals, tickers: entry.tickers },
    );
  }

  // Broker connectivity.
  for (const row of rows(data.brokerStatus)) {
    const provider = displayField(row, ["provider", "source"], "broker");
    const status = displayField(row, ["status", "health"], "not_loaded");
    record(
      { id: "broker", label: "Broker", family: "broker" },
      provider,
      toneFromText(status),
      status,
      displayField(row, ["checked_at", "last_data_at"], ""),
      displayField(row, ["detail"], ""),
      "broker",
    );
  }

  // Finalize: roll provider stats up to category summaries.
  const categories: Category[] = [];
  for (const { def, providers, origins } of cats.values()) {
    const list = [...providers.values()].sort(
      (a, b) => toneRank(a.tone) - toneRank(b.tone) || dateMs(b.latestAt) - dateMs(a.latestAt) || a.provider.localeCompare(b.provider),
    );
    const category: Category = {
      id: def.id,
      label: def.label,
      family: def.family,
      tone: worstTone(list.map((stat) => stat.tone)),
      total: list.length,
      fresh: list.filter((stat) => stat.tone === "good" || stat.tone === "info").length,
      stale: list.filter((stat) => stat.tone === "warn").length,
      failed: list.filter((stat) => stat.tone === "bad").length,
      checks: list.reduce((sum, stat) => sum + stat.checks, 0),
      items: list.reduce((sum, stat) => sum + stat.items, 0),
      signals: list.reduce((sum, stat) => sum + stat.signals, 0),
      tickers: list.reduce((sum, stat) => sum + stat.tickers, 0),
      latestAt: list.reduce((latest, stat) => (dateMs(stat.latestAt) > dateMs(latest) ? stat.latestAt : latest), ""),
      origins: [...origins].sort(),
      providers: list,
    };
    categories.push(category);
  }

  return categories.sort(
    (a, b) => toneRank(a.tone) - toneRank(b.tone) || b.failed - a.failed || b.total - a.total || a.label.localeCompare(b.label),
  );
}

export function buildFamilyHealth(categories: Category[]): FamilyHealth[] {
  const byFamily = new Map<SourceFamilyId, { tones: Tone[]; total: number; healthy: number }>();
  for (const category of categories) {
    const entry = byFamily.get(category.family) ?? { tones: [], total: 0, healthy: 0 };
    entry.tones.push(category.tone);
    entry.total += category.total;
    entry.healthy += category.fresh;
    byFamily.set(category.family, entry);
  }
  return [...byFamily.entries()].map(([id, entry]) => ({
    id,
    label: sourceFamilyDef(id).label,
    tone: worstTone(entry.tones),
    total: entry.total,
    healthy: entry.healthy,
  }));
}

export function buildAgentPipelines(data: PanelData): AgentPipeline[] {
  const metadata = jsonRecord(data.dashboard.status?.metadata);
  const agents = jsonRecord(metadata.agents);

  const thesisRequests = rows(data.agentThesisRequest);
  const postmortemRequests = rows(data.agentPostmortemRequest);
  const thesisResults = rows(data.agentThesis);
  const postmortemResults = rows(data.agentPostmortem);

  // Prefer the unified single-pass `option_agent` block; fall back to the legacy
  // split blocks for back-compat with older runtimes.
  const unified = jsonRecord(agents.option_agent);
  const runtime = agentRuntime(
    Object.keys(unified).length ? unified : jsonRecord(agents.option_thesis),
    8,
  );

  const thesis = pipelineCounts(thesisRequests, thesisResults);
  const postmortem = pipelineCounts(postmortemRequests, postmortemResults);
  const failed = thesis.failed + postmortem.failed;
  const open = thesis.open + postmortem.open;
  const latestAt = dateMs(thesis.latestAt) > dateMs(postmortem.latestAt) ? thesis.latestAt : postmortem.latestAt;
  const tone: Tone = failed ? "bad" : open && !runtime.active ? "warn" : runtime.active ? "good" : "muted";

  return [
    {
      id: "option_agent",
      label: "Option Agent",
      caption:
        "Single consolidated pass producing option theses (synthesis, catalysts, invalidation, red-team) and postmortems (missed winners/losers) for top-ranked candidates.",
      tone,
      active: runtime.active,
      open,
      fulfilled: thesis.fulfilled + postmortem.fulfilled,
      failed,
      superseded: thesis.superseded + postmortem.superseded,
      limit: runtime.limit,
      timeoutSeconds: runtime.timeoutSeconds,
      latestAt,
      mode: runtime.mode,
      subCounts: [
        { label: "Thesis", open: thesis.open, fulfilled: thesis.fulfilled, failed: thesis.failed, limit: runtime.thesisLimit ?? runtime.limit },
        { label: "Postmortem", open: postmortem.open, fulfilled: postmortem.fulfilled, failed: postmortem.failed, limit: runtime.postmortemLimit ?? runtime.limit },
      ],
    },
  ];
}

function pipelineCounts(
  requestRows: RowRecord[],
  resultRows: RowRecord[],
): { open: number; fulfilled: number; failed: number; superseded: number; latestAt: string } {
  const statusCounts = countByStatus(requestRows);
  const latestRequest = latestRowDate(requestRows, ["created_at", "updated_at"]);
  const latestResult = latestRowDate(resultRows, ["created_at", "updated_at"]);
  return {
    open: statusCounts.open ?? 0,
    fulfilled: statusCounts.fulfilled ?? resultRows.length,
    failed: (statusCounts.agent_failed ?? 0) + (statusCounts.failed ?? 0),
    superseded: statusCounts.superseded ?? 0,
    latestAt: dateMs(latestResult) > dateMs(latestRequest) ? latestResult : latestRequest,
  };
}

export function agentSchedulerSeconds(data: PanelData): number {
  const metadata = jsonRecord(data.dashboard.status?.metadata);
  const scheduler = jsonRecord(metadata.scheduler);
  return numberFromJson(scheduler.agent_refresh_seconds, 0);
}

export function agentRuntime(raw: Record<string, unknown>, fallbackLimit: number): AgentRuntime {
  return {
    active: booleanFromJson(raw.active ?? raw.enabled, false) && booleanFromJson(raw.configured, Boolean(raw.command)),
    configured: booleanFromJson(raw.configured, Boolean(raw.command)),
    limit: numberFromJson(raw.limit, fallbackLimit),
    timeoutSeconds: numberFromJson(raw.timeout_seconds, 120),
    requestCap: raw.request_cap === undefined ? undefined : numberFromJson(raw.request_cap, fallbackLimit),
    cadence: stringFromJson(raw.cadence, "daily_premarket"),
    mode: raw.mode === undefined ? undefined : stringFromJson(raw.mode, ""),
    thesisLimit: raw.thesis_limit === undefined ? undefined : numberFromJson(raw.thesis_limit, fallbackLimit),
    postmortemLimit: raw.postmortem_limit === undefined ? undefined : numberFromJson(raw.postmortem_limit, fallbackLimit),
  };
}

export function countByStatus(requestRows: RowRecord[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const row of requestRows) {
    const status = displayField(row, ["status"], "unknown").toLowerCase();
    counts[status] = (counts[status] ?? 0) + 1;
  }
  return counts;
}

export function latestRowDate(requestRows: RowRecord[], fields: string[]): string {
  return requestRows.reduce((latest, row) => {
    const value = displayField(row, fields, "");
    return dateMs(value) > dateMs(latest) ? value : latest;
  }, "");
}

export function latestAgentStep(status: unknown): { attempted: number; accepted: number; failed: number; status: string } {
  const full = jsonRecord(status);
  const steps = Array.isArray(full.steps) ? full.steps : [];
  const optionStep = steps.map(jsonRecord).find((step) => stringFromJson(step.name, "") === "option_agents");
  const result = jsonRecord(optionStep?.result);
  const thesis = jsonRecord(result.agent_thesis_runner);
  const postmortem = jsonRecord(result.agent_postmortem_runner);
  return {
    attempted: numberFromJson(thesis.attempted, 0) + numberFromJson(postmortem.attempted, 0),
    accepted: numberFromJson(thesis.accepted, 0) + numberFromJson(postmortem.accepted, 0),
    failed: numberFromJson(thesis.failed, 0) + numberFromJson(postmortem.failed, 0),
    status: optionStep ? (optionStep.ok === false ? "failed" : "succeeded") : "unknown",
  };
}

export function latestRunnerCount(jobRows: RefreshJob[], jobName: string, field: "attempted" | "accepted" | "failed"): number {
  const row = jobRows.find((item) => item.job_name === jobName);
  if (!row) return 0;
  const summary = jsonRecord(row.summary);
  const nestedAgents = jsonRecord(summary.agents);
  const source = Object.keys(nestedAgents).length ? nestedAgents : summary;
  const thesis = jsonRecord(source.agent_thesis_runner);
  const postmortem = jsonRecord(source.agent_postmortem_runner);
  return numberFromJson(thesis[field], 0) + numberFromJson(postmortem[field], 0);
}

export function collectTopErrors(categories: Category[], data: PanelData, jobRows: RefreshJob[]): ErrorAgg[] {
  const byMessage = new Map<string, ErrorAgg>();

  const add = (message: string, tone: Tone, at: string, source: string) => {
    const clean = message.trim();
    if (!clean) return;
    const dedupeKey = clean.slice(0, 160).toLowerCase();
    const existing = byMessage.get(dedupeKey);
    if (existing) {
      existing.count += 1;
      if (toneRank(tone) < toneRank(existing.tone)) existing.tone = tone;
      if (dateMs(at) > dateMs(existing.latestAt)) existing.latestAt = at;
      if (!existing.sources.includes(source)) existing.sources.push(source);
    } else {
      byMessage.set(dedupeKey, { message: clean.slice(0, 200), tone, count: 1, latestAt: at, sources: [source] });
    }
  };

  for (const category of categories) {
    for (const provider of category.providers) {
      if (provider.tone !== "bad" && provider.tone !== "warn") continue;
      const message = provider.detail ? truncate(provider.detail) : `${category.label}: ${provider.status}`;
      add(message, provider.tone, provider.latestAt, provider.provider);
    }
  }
  for (const row of rows(data.providerRuns)) {
    const status = displayField(row, ["status"], "");
    const tone = toneFromText(status);
    if (tone === "bad" || tone === "warn") {
      const detail = displayField(row, ["detail", "error", "message"], titleLabel(status));
      add(truncate(detail), tone, displayField(row, ["finished_at", "started_at"], ""), `${displayField(row, ["provider"], "provider")}:${displayField(row, ["capability"], "")}`);
    }
  }
  for (const row of jobRows) {
    if ((row.status ?? "") === "failed" && row.error) {
      add(truncate(row.error), "bad", row.finished_at ?? row.started_at ?? "", jobDef(row.job_name ?? "").label);
    }
  }

  return [...byMessage.values()]
    .sort((a, b) => toneRank(a.tone) - toneRank(b.tone) || b.count - a.count || dateMs(b.latestAt) - dateMs(a.latestAt))
    .slice(0, 12);
}
