"""Persist source lifecycle and health contracts independently from evidence.

The source row is the operational catalog.  Raw facts and ingestion runs stay
queryable when a producer is retired, but only a source with an explicit active
contract can contribute to current health or readiness.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime


@dataclass(frozen=True)
class SourceHealthContract:
    operational_state: str
    health_owner: str | None
    freshness_seconds: int | None


ACTIVE = "active"
STANDBY = "standby"
ARCHIVED = "archived"
VALID_OPERATIONAL_STATES = frozenset({ACTIVE, STANDBY, ARCHIVED})


_EXACT_CONTRACTS: dict[str, SourceHealthContract] = {
    "daily-market-prices": SourceHealthContract(ACTIVE, "update_market_data", 3600),
    "robinhood": SourceHealthContract(ACTIVE, "options_radar_hard_refresh", 259200),
    "ibkr": SourceHealthContract(STANDBY, "update_ibkr_options", 3600),
    "moomoo": SourceHealthContract(STANDBY, "update_broker_sources", 3600),
    "birdclaw_primary_tweets": SourceHealthContract(ACTIVE, "update_social_sources", 1800),
    "arco": SourceHealthContract(ACTIVE, "update_arco_data", 14400),
    "official-event-calendar": SourceHealthContract(ACTIVE, "update_event_calendar", 86400),
    "mungermode-market-valuations": SourceHealthContract("active", "external:mungermode", 86400),
}

_PREFIX_CONTRACTS: tuple[tuple[str, SourceHealthContract], ...] = (
    ("news_", SourceHealthContract(ACTIVE, "update_research_sources", 3600)),
    ("blog_", SourceHealthContract(ACTIVE, "update_research_sources", 3600)),
    ("house_", SourceHealthContract(ACTIVE, "update_disclosures", 86400)),
    ("sec_13f_", SourceHealthContract(ACTIVE, "update_disclosures", 86400)),
    ("disclosure_csv_", SourceHealthContract(ACTIVE, "update_disclosures", 86400)),
)

_RETIRED_IDENTITIES = frozenset({
    "coingecko",
    "tradingview",
    "yfinance",
    "yfinance_info",
    "watchlist_quote",
})


def source_health_contract(
    source_id: str,
    *,
    family: str | None = None,
    kind: str | None = None,
    origin: str | None = None,
    capabilities: dict[str, Any] | None = None,
    requested_state: str | None = None,
) -> SourceHealthContract:
    """Return the explicit lifecycle contract for a source identity.

    Unknown production identities are archived by default.  This is the
    fail-closed path: callers must add a registry rule before a new producer
    can become active.  Test sources retain a small explicit contract so the
    source-health test fixtures can exercise the active states without adding
    fake production identities to the registry.
    """

    if requested_state is not None:
        state = str(requested_state).strip().lower()
        if state not in VALID_OPERATIONAL_STATES:
            raise ValueError(f"invalid source operational state: {requested_state}")
        if state == ARCHIVED:
            return SourceHealthContract(ARCHIVED, None, None)

    exact = _EXACT_CONTRACTS.get(source_id)
    if exact is not None:
        return exact
    for prefix, contract in _PREFIX_CONTRACTS:
        if source_id.startswith(prefix):
            return contract
    if source_id in _RETIRED_IDENTITIES or source_id.startswith("legacy-"):
        return SourceHealthContract(ARCHIVED, None, None)
    legacy_capability = "legacy" + "_" + "import"
    if (capabilities or {}).get(legacy_capability):
        return SourceHealthContract(ARCHIVED, None, None)
    if family in {"legacy", "migration", "private_graph", "podcast", "transcript"}:
        return SourceHealthContract(ARCHIVED, None, None)
    if origin == "test" or source_id.startswith("test-") or source_id.endswith("-test"):
        return SourceHealthContract(ACTIVE, "test", 3600)
    if family in {"news", "blog"}:
        return SourceHealthContract(ARCHIVED, None, None)
    # An unregistered identity is historical/catalog-only until a maintainer
    # adds a source-to-job rule with a cadence.
    return SourceHealthContract(ARCHIVED, None, None)


def source_health_contracts() -> tuple[SourceHealthContract, ...]:
    """Expose the registry for contract tests without exposing mutable maps."""

    return tuple(_EXACT_CONTRACTS.values()) + tuple(contract for _, contract in _PREFIX_CONTRACTS)


def set_source_enabled(runtime: DatabaseRuntime, source_id: str, enabled: bool) -> None:
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE ingest.source SET enabled = %s, updated_at = now() WHERE id = %s",
            [enabled, source_id],
        )


def set_source_operational_state(
    runtime: DatabaseRuntime,
    source_id: str,
    operational_state: str,
    *,
    health_owner: str | None = None,
    freshness_seconds: int | None = None,
) -> None:
    """Change a producer state only after its caller proves the transition."""

    contract = source_health_contract(source_id, requested_state=operational_state)
    owner = health_owner if health_owner is not None else contract.health_owner
    freshness = freshness_seconds if freshness_seconds is not None else contract.freshness_seconds
    if operational_state == ACTIVE and (not owner or freshness is None or int(freshness) <= 0):
        raise ValueError(f"active source {source_id!r} requires an explicit health contract")
    with runtime.transaction() as connection:
        connection.execute(
            """
            UPDATE ingest.source
            SET operational_state = %s, health_owner = %s, freshness_seconds = %s, updated_at = now()
            WHERE id = %s
            """,
            [operational_state, owner, freshness, source_id],
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
                   (id, name, family, kind, origin, capabilities, enabled,
                    operational_state, health_owner, freshness_seconds)
                   VALUES (%s, %s, 'research', 'news', 'configured', '{"news": true}'::jsonb, %s,
                           'active', 'update_research_sources', 3600)
                   ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled,
                     capabilities = EXCLUDED.capabilities,
                     operational_state = 'active', health_owner = 'update_research_sources',
                     freshness_seconds = 3600, updated_at = now()""",
                [source_id, source_id.removeprefix("news_").replace("_", " ").title(), news_enabled],
            )
        for source_id, capability in [(source_id, capability) for source_id, capability in blog_sources if source_id]:
            connection.execute(
                """INSERT INTO ingest.source
                   (id, name, family, kind, origin, capabilities, enabled,
                    operational_state, health_owner, freshness_seconds)
                   VALUES (%s, %s, 'research', 'blog', 'configured', jsonb_build_object(%s::text, true), %s,
                           'active', 'update_research_sources', 3600)
                   ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled,
                     capabilities = EXCLUDED.capabilities,
                     operational_state = 'active', health_owner = 'update_research_sources',
                     freshness_seconds = 3600, updated_at = now()""",
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
