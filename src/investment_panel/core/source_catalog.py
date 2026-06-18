"""Authoritative data-source catalog: category → primary/fallback/cadence.

This module is the backend source of truth for *what* data the panel pulls,
*which provider is primary vs. fallback*, *how often* it refreshes, and *which
refresh job* owns it. It moves the implicit source→category knowledge that used
to live on the frontend (``views/health/types.ts``) into the backend so the
Health page can be driven by an API instead of re-deriving categories.

The catalog is declarative only — it declares the wiring; live status is joined
in ``core/panel/catalog.py:build_source_catalog_health``. Cadence labels/seconds
mirror ``app/scheduler.py`` knobs and ``decision/freshness.py`` windows rather
than inventing new literals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from investment_panel.core.decision import stale_after_label


@dataclass(frozen=True)
class DataCategory:
    """One declared data category with its primary/fallback provider chain."""

    id: str
    label: str
    family: str
    primary: str
    fallback: list[str] = field(default_factory=list)
    cadence_label: str = "provider contract"
    cadence_seconds: int | None = None
    refresh_job: str = ""
    stale_after: str = ""
    # source_type values (as emitted by build_source_freshness / source_runs)
    # that roll up into this category.
    source_types: list[str] = field(default_factory=list)
    # True when a backend live fetcher pulls this directly (opencli/free_sources);
    # False for catalog-only declarations (e.g. podcasts/transcripts) and for
    # internally computed data.
    live_fetcher: bool = False


# Cadence seconds mirror the scheduler defaults so the two cannot drift:
#   MARKET_RADAR_REFRESH_SECONDS (900), MARKET_SOURCE_REFRESH_SECONDS (3600),
#   MARKET_SOCIAL_REFRESH_SECONDS (1800), MARKET_RESEARCH_REFRESH_SECONDS (3600),
#   MARKET_ENVIRONMENT_REFRESH_SECONDS (3600).
SOURCE_CATALOG: list[DataCategory] = [
    DataCategory(
        id="options",
        label="Option Chains",
        family="market_data",
        primary="robinhood",
        fallback=["ibkr", "tradingview", "yfinance"],
        cadence_label="hourly",
        cadence_seconds=3600,
        refresh_job="update_robinhood_options",
        stale_after=stale_after_label("options"),
        source_types=["options"],
        live_fetcher=True,
    ),
    DataCategory(
        id="quotes",
        label="Intraday Quotes",
        family="market_data",
        primary="robinhood",
        fallback=["tradingview", "yfinance"],
        cadence_label="~15 min",
        cadence_seconds=900,
        refresh_job="refresh_options_radar_signal_robinhood",
        stale_after=stale_after_label("intraday_quote"),
        source_types=["intraday_quote"],
        live_fetcher=True,
    ),
    DataCategory(
        id="daily_prices",
        label="Closing / Daily Prices",
        family="market_data",
        primary="yfinance",
        fallback=["tradingview"],
        cadence_label="daily",
        cadence_seconds=86400,
        refresh_job="full_market_refresh",
        stale_after=stale_after_label("closing_quote"),
        source_types=["closing_quote"],
        live_fetcher=True,
    ),
    DataCategory(
        id="crypto_quotes",
        label="Crypto Quotes",
        family="market_data",
        primary="coingecko",
        fallback=[],
        cadence_label="daily",
        cadence_seconds=86400,
        refresh_job="update_crypto_data",
        stale_after=stale_after_label("crypto_quote"),
        source_types=["crypto_quote"],
        live_fetcher=True,
    ),
    DataCategory(
        id="fundamentals",
        label="Fundamentals",
        family="market_data",
        primary="sec_companyfacts",
        fallback=["yfinance"],
        cadence_label="filing cadence",
        cadence_seconds=None,
        refresh_job="update_free_sources",
        stale_after=stale_after_label("fundamental"),
        source_types=["fundamental"],
        live_fetcher=True,
    ),
    DataCategory(
        id="daily",
        label="Daily Analyses",
        family="market_data",
        primary="internal_compute",
        fallback=[],
        cadence_label="1 trading day",
        cadence_seconds=None,
        refresh_job="refresh_decision_models",
        stale_after=stale_after_label("daily"),
        source_types=["daily"],
        live_fetcher=False,
    ),
    DataCategory(
        id="filings",
        label="Filings & 13F",
        family="filing",
        primary="sec_edgar",
        fallback=["house_disclosures"],
        cadence_label="filing cadence",
        cadence_seconds=None,
        refresh_job="update_disclosures",
        stale_after=stale_after_label("filing"),
        source_types=["filing"],
        live_fetcher=True,
    ),
    DataCategory(
        id="social",
        label="X / Social",
        family="social",
        primary="x_list",
        fallback=["x_account", "arco_birdclaw"],
        cadence_label="~30 min (paced)",
        cadence_seconds=1800,
        refresh_job="update_social_sources",
        stale_after=stale_after_label("arco_thesis"),
        source_types=["arco_thesis"],
        live_fetcher=True,
    ),
    DataCategory(
        id="news",
        label="News",
        family="blog",
        primary="bloomberg",
        fallback=["reuters", "google-news", "tradingview", "hackernews"],
        cadence_label="hourly",
        cadence_seconds=3600,
        refresh_job="update_research_sources",
        stale_after=stale_after_label("news"),
        source_types=["news"],
        live_fetcher=True,
    ),
    DataCategory(
        id="blogs",
        label="Blogs & Memos",
        family="blog",
        primary="substack",
        fallback=["web_rss", "medium"],
        cadence_label="daily",
        cadence_seconds=86400,
        refresh_job="update_research_sources",
        stale_after=stale_after_label("daily"),
        source_types=["blog"],
        live_fetcher=True,
    ),
    DataCategory(
        id="podcasts",
        label="Podcasts & Transcripts",
        family="podcast",
        primary="deferred",
        fallback=[],
        cadence_label="deferred",
        cadence_seconds=None,
        refresh_job="",
        stale_after="not applicable",
        source_types=["podcast", "transcript"],
        live_fetcher=False,
    ),
    DataCategory(
        id="broker",
        label="Broker",
        family="market_data",
        primary="ibkr",
        fallback=["moomoo"],
        cadence_label="per job",
        cadence_seconds=None,
        refresh_job="update_broker_sources",
        stale_after=stale_after_label("provider_health"),
        source_types=["provider_health"],
        live_fetcher=True,
    ),
    DataCategory(
        id="ingestion_runs",
        label="Source Ingestion Runs",
        family="other",
        primary="internal_compute",
        fallback=[],
        cadence_label="per job",
        cadence_seconds=None,
        refresh_job="",
        stale_after=stale_after_label("provider_run"),
        source_types=["provider_run", "documentation"],
        live_fetcher=False,
    ),
]


SOURCE_CATALOG_BY_ID: dict[str, DataCategory] = {category.id: category for category in SOURCE_CATALOG}


def catalog_source_types() -> set[str]:
    """Union of every source_type the catalog claims to cover."""

    types: set[str] = set()
    for category in SOURCE_CATALOG:
        types.update(category.source_types)
    return types


def category_for_source_type(source_type: str) -> DataCategory | None:
    for category in SOURCE_CATALOG:
        if source_type in category.source_types:
            return category
    return None
