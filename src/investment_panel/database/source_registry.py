"""Persist configured source enablement independently from refresh execution."""

from __future__ import annotations

from collections.abc import Sequence

from investment_panel.database.runtime import DatabaseRuntime


def set_source_enabled(runtime: DatabaseRuntime, source_id: str, enabled: bool) -> None:
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE ingest.source SET enabled = %s, updated_at = now() WHERE id = %s",
            [enabled, source_id],
        )


def sync_research_source_enablement(
    runtime: DatabaseRuntime,
    *,
    news_ids: Sequence[str],
    blog_sources: Sequence[tuple[str, str]],
    news_enabled: bool,
    blogs_enabled: bool,
    x_enabled: bool,
) -> None:
    """Synchronize configurable live-source state without fetching data."""

    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE ingest.source SET enabled = false, updated_at = now() "
            "WHERE id LIKE 'news_%' OR id LIKE 'blog_%'"
        )
        for source_id in [source_id for source_id in news_ids if source_id]:
            connection.execute(
                """INSERT INTO ingest.source
                   (id, name, family, kind, origin, capabilities, enabled)
                   VALUES (%s, %s, 'research', 'news', 'configured', '{"news": true}'::jsonb, %s)
                   ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled,
                     capabilities = EXCLUDED.capabilities, updated_at = now()""",
                [source_id, source_id.removeprefix("news_").replace("_", " ").title(), news_enabled],
            )
        for source_id, capability in [(source_id, capability) for source_id, capability in blog_sources if source_id]:
            connection.execute(
                """INSERT INTO ingest.source
                   (id, name, family, kind, origin, capabilities, enabled)
                   VALUES (%s, %s, 'research', 'blog', 'configured', jsonb_build_object(%s::text, true), %s)
                   ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled,
                     capabilities = EXCLUDED.capabilities, updated_at = now()""",
                [source_id, source_id.removeprefix("blog_").replace("_", " ").title(), capability, blogs_enabled],
            )
        if x_enabled:
            connection.execute(
                "UPDATE ingest.source SET capabilities = capabilities || '{\"x_list\": true}'::jsonb, "
                "enabled = true, updated_at = now() WHERE id = 'birdclaw_primary_tweets'"
            )
        else:
            connection.execute(
                "UPDATE ingest.source SET capabilities = capabilities - 'x_list' - 'x_account', "
                "updated_at = now() WHERE id = 'birdclaw_primary_tweets'"
            )
