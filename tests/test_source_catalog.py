"""Catalog declaration + freshness-coverage invariants."""

from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.core.db import db, init_db
from investment_panel.core.panel import build_source_catalog_health
from investment_panel.core.source_catalog import SOURCE_CATALOG, catalog_source_types
from investment_panel.core.source_ingestion.canonical import record_source_run

# The literal set of source_type values emitted by
# core/decision/builders.py:build_source_freshness. If a new source_type is added
# there, the catalog must learn to cover it (this test forces that).
FRESHNESS_SOURCE_TYPES = {
    "intraday_quote",
    "closing_quote",
    "crypto_quote",
    "options",
    "news",
    "daily",
    "fundamental",
    "filing",
    "arco_thesis",
    "provider_run",
    "provider_health",
    "documentation",
}


def test_every_category_resolves_a_primary_and_cadence() -> None:
    for category in SOURCE_CATALOG:
        assert category.id, "category id is required"
        assert category.primary, f"category {category.id} has no primary"
        assert category.cadence_label, f"category {category.id} has no cadence_label"
        assert category.source_types, f"category {category.id} declares no source_types"


def test_catalog_source_types_cover_freshness_emitted_types() -> None:
    union = catalog_source_types()
    missing = sorted(FRESHNESS_SOURCE_TYPES - union)
    assert missing == [], f"catalog does not cover freshness source_types: {missing}"


def test_live_fetcher_flags_match_plan() -> None:
    by_id = {c.id: c for c in SOURCE_CATALOG}
    # Live-fetched categories.
    for cid in ("options", "quotes", "social", "news", "blogs", "broker"):
        assert by_id[cid].live_fetcher is True, f"{cid} should be a live fetcher"
    # Catalog-only / internally computed.
    for cid in ("daily", "podcasts"):
        assert by_id[cid].live_fetcher is False, f"{cid} should not be a live fetcher"


def test_category_ids_are_unique() -> None:
    ids = [c.id for c in SOURCE_CATALOG]
    assert len(ids) == len(set(ids))


def test_social_and_blog_status_surface_from_source_runs(tmp_path) -> None:
    """Live X/blog sources report via source_runs, not the freshness index.

    The catalog rollup must surface their latest run status (so an ingesting X
    feed shows healthy, not perpetually "unknown").
    """

    db_path = tmp_path / "catalog.duckdb"
    init_db(db_path)
    now = datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        record_source_run(
            con,
            source_id="birdclaw_primary_tweets",
            run_id="run_x_1",
            capability="x_account",
            started_at=now,
            finished_at=now,
            status="ok",
            item_count=30,
            ticker_count=0,
            failure_detail="",
            raw={},
        )
        record_source_run(
            con,
            source_id="blog_example.substack.com",
            run_id="run_blog_1",
            capability="substack",
            started_at=now,
            finished_at=now,
            status="ok",
            item_count=5,
            ticker_count=0,
            failure_detail="",
            raw={},
        )
        health = build_source_catalog_health(con)

    by_id = {c["id"]: c for c in health["categories"]}
    assert by_id["social"]["primary"]["status"] != "unknown"
    assert by_id["social"]["primary"]["tone"] == "good"
    assert by_id["blogs"]["primary"]["status"] != "unknown"
    assert by_id["blogs"]["primary"]["tone"] == "good"
