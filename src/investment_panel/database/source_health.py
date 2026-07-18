"""Canonical PostgreSQL source-health projection.

The Health page and source catalog endpoint consume this query. Compatibility
health/freshness routes retain their established wire shapes. Run outcome and
freshness deliberately remain separate: a source can still have usable recent
data while its latest attempt failed, and disabled sources are never incidents.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from investment_panel.core.job_policy import source_primary_refresh_job_sql, source_refresh_jobs_sql


SOURCE_HEALTH_QUERY = f"""
WITH eligible_run AS (
    SELECT run.*
    FROM ingest.run run
    JOIN ingest.source source ON source.id = run.source_id
    WHERE source.family NOT IN ('research', 'social', 'broker')
       OR (source.family = 'research' AND source.capabilities ? run.capability)
       OR (source.id = 'birdclaw_primary_tweets'
           AND (run.capability = 'content' OR source.capabilities ? run.capability))
       OR (source.id = 'arco' AND run.capability = 'content')
       OR (source.id = 'ibkr' AND run.capability IN ('option_quotes', 'broker_sync'))
       OR (source.id = 'moomoo' AND run.capability = 'broker_sync')
       OR (source.id = 'robinhood' AND run.capability = 'option_quotes')
), latest_run AS (
    SELECT DISTINCT ON (run.source_id)
           run.source_id, run.capability, run.status, run.started_at,
           run.finished_at, run.item_count, run.instrument_count,
           run.failure_detail, run.summary
    FROM eligible_run run
    ORDER BY run.source_id, run.started_at DESC
), latest_success AS (
    SELECT DISTINCT ON (run.source_id)
           run.source_id, run.finished_at, run.item_count, run.instrument_count
    FROM eligible_run run
    WHERE run.status = 'succeeded'
    ORDER BY run.source_id, run.started_at DESC
), latest_data_run AS (
    SELECT DISTINCT ON (run.source_id)
           run.source_id, run.finished_at, run.item_count, run.instrument_count
    FROM eligible_run run
    WHERE run.status IN ('succeeded', 'partial')
      AND (COALESCE(run.item_count, 0) > 0 OR COALESCE(run.instrument_count, 0) > 0)
    ORDER BY run.source_id, run.started_at DESC
), capability_latest AS (
    SELECT DISTINCT ON (run.source_id, run.capability)
           run.source_id, run.capability, run.status, run.started_at,
           run.finished_at, run.failure_detail
    FROM eligible_run run
    ORDER BY run.source_id, run.capability, run.started_at DESC
), worst_capability AS (
    SELECT DISTINCT ON (source_id)
           source_id, capability, status, started_at, finished_at, failure_detail
    FROM capability_latest
    ORDER BY source_id,
      CASE
        WHEN lower(status) = 'failed' THEN 0
        WHEN lower(status) IN ('partial', 'rate_limited', 'skipped') THEN 1
        WHEN lower(status) = 'running' THEN 2
        WHEN lower(status) = 'succeeded' THEN 3
        ELSE 4
      END,
      finished_at DESC NULLS LAST
), capability_health AS (
    SELECT source_id,
           jsonb_agg(
             jsonb_build_object(
               'capability', capability,
               'status', status,
               'finished_at', finished_at,
               'failure_detail', COALESCE(failure_detail, '')
             ) ORDER BY capability
           ) AS rows
    FROM capability_latest
    GROUP BY source_id
), parent_sec AS (
    SELECT latest.capability, latest.status, latest.started_at, latest.finished_at,
           latest.item_count, latest.instrument_count, latest.failure_detail,
           success.finished_at AS last_success_at
    FROM latest_run latest
    LEFT JOIN latest_success success ON success.source_id = latest.source_id
    WHERE latest.source_id = 'sec_edgar'
), base AS (
    SELECT source.id AS source_id, source.name AS source_name,
           source.family AS source_family, source.kind AS source_kind,
           source.origin, source.enabled, source.ingestion_mode,
           source.source_url, source.capabilities,
           COALESCE(capability_health.rows, '[]'::jsonb) AS capability_health,
           {source_refresh_jobs_sql()} AS refresh_jobs,
           {source_primary_refresh_job_sql()} AS refresh_job,
           CASE
             WHEN source.capabilities ? 'legacy_import' OR source.origin = 'legacy-duckdb'
               OR source.id LIKE 'legacy-%' THEN NULL
             WHEN source.id = 'watchlist_quote' THEN NULL
             WHEN source.id = 'robinhood' THEN 259200
             WHEN source.id IN ('ibkr', 'moomoo') THEN 3600
             WHEN source.id = 'arco' THEN 14400
             WHEN source.family = 'research' THEN 3600
             WHEN source.family = 'social' THEN 1800
             WHEN source.family IN ('events', 'disclosures', 'filing') THEN 86400
             WHEN source.family IN ('market_data', 'estimates') THEN 86400
             WHEN source.family IN ('calendar', 'fundamentals', 'migration', 'legacy', 'news',
                                    'private_graph', 'blog', 'podcast', 'transcript') THEN NULL
             ELSE 172800
           END AS stale_after_seconds,
           CASE
             WHEN source.family = 'broker' THEN 'broker'
             WHEN source.family IN ('calendar', 'events') THEN 'events'
             WHEN source.family IN ('disclosures', 'filing') THEN 'filings'
             WHEN source.family IN ('market_data', 'estimates', 'fundamentals') THEN 'market_data'
             WHEN source.family IN ('news', 'research', 'blog', 'podcast', 'transcript') THEN 'research'
             WHEN source.family IN ('social', 'private_graph') THEN 'social'
             WHEN source.family IN ('migration', 'legacy') THEN 'legacy'
             ELSE 'other'
           END AS operational_group,
           COALESCE(worst.capability,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.capability END) AS latest_capability,
           COALESCE(worst.status,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.status END) AS run_status,
           COALESCE(latest.started_at,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.started_at END) AS last_started_at,
           COALESCE(latest.finished_at, latest.started_at,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.finished_at END) AS last_attempt_at,
           COALESCE(worst.finished_at, worst.started_at,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.finished_at END) AS status_at,
           COALESCE(success.finished_at,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.last_success_at END) AS last_success_at,
           data_run.finished_at AS last_data_at,
           COALESCE(data_run.item_count, 0) AS item_count,
           COALESCE(data_run.instrument_count, 0) AS ticker_count,
           COALESCE(worst.failure_detail,
                    CASE WHEN source.kind = 'sec_filing' THEN parent.failure_detail END, '') AS failure_detail,
           latest.source_id IS NULL AND source.kind = 'sec_filing'
             AND parent.finished_at IS NOT NULL AS inherited_check
    FROM ingest.source source
    LEFT JOIN latest_run latest ON latest.source_id = source.id
    LEFT JOIN worst_capability worst ON worst.source_id = source.id
    LEFT JOIN latest_success success ON success.source_id = source.id
    LEFT JOIN latest_data_run data_run ON data_run.source_id = source.id
    LEFT JOIN capability_health ON capability_health.source_id = source.id
    LEFT JOIN parent_sec parent ON true
), classified AS (
    SELECT base.*,
           CASE
             WHEN NOT enabled THEN 'disabled'
             WHEN last_success_at IS NULL THEN 'missing'
             WHEN stale_after_seconds IS NOT NULL
               AND last_success_at < now() - make_interval(secs => stale_after_seconds)
               THEN 'stale'
             ELSE 'fresh'
           END AS freshness_status
    FROM base
)
SELECT classified.*,
       CASE
         WHEN NOT enabled THEN 'disabled'
         WHEN lower(COALESCE(run_status, '')) = 'failed' THEN 'failed'
         WHEN lower(COALESCE(run_status, '')) IN ('partial', 'rate_limited', 'skipped') THEN 'degraded'
         WHEN lower(COALESCE(run_status, '')) = 'running'
           AND status_at < now() - make_interval(secs => GREATEST(COALESCE(stale_after_seconds, 0), 10800))
           THEN 'degraded'
         WHEN lower(COALESCE(run_status, '')) = 'running' THEN 'running'
         WHEN freshness_status = 'missing' THEN 'missing'
         WHEN freshness_status = 'stale' THEN 'stale'
         ELSE 'healthy'
       END AS effective_status,
       CASE
         WHEN failure_detail ILIKE '%BROWSER_CONNECT%'
           OR failure_detail ILIKE '%profile%not connected%'
           THEN 'Reconnect the configured OpenCLI browser profile, then rerun this source.'
         WHEN failure_detail ILIKE '%403 Forbidden%'
           THEN 'Upstream rejected part of the request; verify headers/rate limits and retry.'
         WHEN lower(COALESCE(run_status, '')) = 'skipped'
           THEN 'Check provider configuration or connectivity before retrying.'
         WHEN lower(COALESCE(run_status, '')) = 'running'
           AND status_at < now() - make_interval(secs => GREATEST(COALESCE(stale_after_seconds, 0), 10800))
           THEN 'The previous refresh appears abandoned; retry the owning job.'
         WHEN lower(COALESCE(run_status, '')) = 'running'
           THEN 'Refresh is currently in progress.'
         WHEN refresh_job IS NULL AND freshness_status IN ('missing', 'stale')
           THEN 'No direct refresh job is currently wired for this source.'
         WHEN freshness_status = 'missing'
           THEN 'Run the owning refresh job to establish a successful check.'
         WHEN freshness_status = 'stale'
           THEN 'Run the owning refresh job; the last successful check exceeded its cadence.'
         ELSE ''
       END AS remediation,
       CASE
         WHEN stale_after_seconds IS NULL THEN 'event driven'
         WHEN stale_after_seconds < 3600 THEN (stale_after_seconds / 60)::text || ' min'
         WHEN stale_after_seconds < 86400 THEN (stale_after_seconds / 3600)::text || ' hr'
         ELSE (stale_after_seconds / 86400)::text || ' day'
       END AS cadence_label
FROM classified
ORDER BY
  CASE
    WHEN NOT enabled THEN 6
    WHEN lower(COALESCE(run_status, '')) = 'failed' THEN 0
    WHEN lower(COALESCE(run_status, '')) IN ('partial', 'rate_limited', 'skipped') THEN 1
    WHEN lower(COALESCE(run_status, '')) = 'running' THEN 2
    WHEN freshness_status = 'missing' THEN 3
    WHEN freshness_status = 'stale' THEN 4
    ELSE 5
  END,
  operational_group, source_name
"""


def source_health_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Envelope used by the standalone source-catalog endpoint."""

    groups: dict[str, list[str]] = defaultdict(list)
    counts = {
        "total": len(rows),
        "enabled": 0,
        "healthy": 0,
        "attention": 0,
        "failed": 0,
        "disabled": 0,
    }
    last_success_at = None
    for row in rows:
        groups[str(row.get("operational_group") or "other")].append(str(row.get("source_id") or ""))
        status = str(row.get("effective_status") or "missing")
        if status == "disabled":
            counts["disabled"] += 1
        else:
            counts["enabled"] += 1
            if status == "healthy":
                counts["healthy"] += 1
            else:
                counts["attention"] += 1
            if status == "failed":
                counts["failed"] += 1
        observed = row.get("last_success_at")
        if observed is not None and (last_success_at is None or observed > last_success_at):
            last_success_at = observed
    return {
        "rows": rows,
        "groups": dict(groups),
        "summary": {**counts, "last_success_at": last_success_at},
        "generated_from": "postgresql.ingest.source+ingest.run",
        "status": {"ready": True, "source": "postgresql"},
    }
