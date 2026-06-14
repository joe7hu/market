"""Catalog declaration + freshness-coverage invariants."""

from __future__ import annotations

from investment_panel.core.source_catalog import SOURCE_CATALOG, catalog_source_types

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
