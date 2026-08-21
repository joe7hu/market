import { describe, expect, it } from "vitest";

import type { SourceCatalogRow } from "@/api/panel";
import type { PanelData } from "@/types";
import {
  collectSourceErrors,
  filterSourceHealth,
  groupSourceHealth,
  parseSourceCatalog,
  sourceFamilyHealth,
  summarizeSourceHealth,
} from "@/views/health/catalog";

function source(overrides: Partial<SourceCatalogRow>): SourceCatalogRow {
  return {
    source_id: "source",
    source_name: "Source",
    source_family: "research",
    source_kind: "news",
    operational_group: "research",
    enabled: true,
    ingestion_mode: "direct",
    refresh_job: "update_research_sources",
    refresh_jobs: ["update_research_sources"],
    cadence_label: "1 hr",
    run_status: "succeeded",
    freshness_status: "fresh",
    effective_status: "healthy",
    latest_capability: "news",
    capability_health: [{ capability: "news", status: "succeeded", finished_at: "2026-07-12T20:00:00Z", failure_detail: "" }],
    last_attempt_at: "2026-07-12T20:00:00Z",
    status_at: "2026-07-12T20:00:00Z",
    last_success_at: "2026-07-12T20:00:00Z",
    last_data_at: "2026-07-12T19:55:00Z",
    item_count: 12,
    ticker_count: 4,
    failure_detail: "",
    remediation: "",
    inherited_check: false,
    source_url: "",
    ...overrides,
  };
}

describe("source health catalog", () => {
  it("keeps disabled sources out of operational counts and filters", () => {
    const rows = [
      source({ source_id: "healthy" }),
      source({ source_id: "failed", source_name: "Reuters", run_status: "failed", effective_status: "failed" }),
      source({ source_id: "disabled", enabled: false, effective_status: "disabled", freshness_status: "disabled" }),
    ];

    expect(summarizeSourceHealth(rows)).toMatchObject({ total: 3, enabled: 2, healthy: 1, attention: 1, failed: 1, disabled: 1 });
    expect(filterSourceHealth(rows, "", "all").map((row) => row.source_id)).toEqual(["healthy", "failed"]);
    expect(filterSourceHealth(rows, "reuters", "attention").map((row) => row.source_id)).toEqual(["failed"]);
  });

  it("sorts attention sources first and exposes only exact group jobs", () => {
    const groups = groupSourceHealth([
      source({ source_id: "healthy" }),
      source({ source_id: "failed", effective_status: "failed" }),
      source({ source_id: "no-job", refresh_job: "", refresh_jobs: [], effective_status: "missing" }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].rows.map((row) => row.effective_status)).toEqual(["failed", "missing", "healthy"]);
    expect(groups[0].jobs).toEqual(["update_research_sources"]);
  });

  it("keeps warning tone when another source in the group is running", () => {
    const groups = groupSourceHealth([
      source({ source_id: "running", effective_status: "running" }),
      source({ source_id: "missing", effective_status: "missing" }),
    ]);

    expect(groups[0].tone).toBe("warn");
  });

  it("carries exact refresh jobs into data-flow family health", () => {
    const families = sourceFamilyHealth([
      source({ source_id: "research", refresh_job: "update_research_sources", refresh_jobs: ["update_research_sources"] }),
      source({ source_id: "arco", refresh_job: "update_arco_data", refresh_jobs: ["update_arco_data"] }),
    ]);
    expect(families[0].jobs).toEqual(["update_arco_data", "update_research_sources"]);
  });

  it("deduplicates remediation messages across affected sources", () => {
    const message = "Reconnect the configured OpenCLI browser profile, then rerun this source.";
    const errors = collectSourceErrors([
      source({ source_id: "reuters", source_name: "Reuters", effective_status: "failed", remediation: message }),
      source({ source_id: "x", source_name: "Curated X", effective_status: "failed", remediation: message }),
    ], [
      { job_name: "full_market_refresh", status: "failed", started_at: "2026-07-12T18:00:00Z", error: "old failure" },
      { job_name: "full_market_refresh", status: "succeeded", started_at: "2026-07-12T20:00:00Z" },
    ]);

    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatchObject({ message, count: 2, sources: ["Reuters", "Curated X"] });
    expect(errors.some((error) => error.message === "old failure")).toBe(false);
  });

  it("parses the PostgreSQL row shape without the removed fetcher field", () => {
    const data = {
      dashboard: {},
      settings: {},
      errors: {},
      sourceCatalog: { rows: [source({ source_id: "parsed" })], count: 1 },
    } as unknown as PanelData;

    const parsed = parseSourceCatalog(data);
    expect(parsed[0].source_id).toBe("parsed");
    expect(parsed[0].effective_status).toBe("healthy");
    expect(parsed[0]).not.toHaveProperty("live_fetcher");
  });
});
