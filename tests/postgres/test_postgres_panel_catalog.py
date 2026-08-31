from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from conftest import typed_config
from investment_panel.core.decision import build_ticker_decision
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.panel_models import MODEL_ALIASES, QUERY_POLICIES, load_postgres_tables
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository


def test_panel_query_catalog_owns_alias_and_symbol_scope_policy() -> None:
    assert MODEL_ALIASES["ticker_memos"] == "research_packets"
    assert QUERY_POLICIES["research_packets"].symbol_scoped is True
    assert QUERY_POLICIES["research_packets"].exclude_future_rows is True
    assert QUERY_POLICIES["catalysts"].allow_symbol_less is True
    assert QUERY_POLICIES["options_ticker_signals"].custom_loader == "options_ticker_signals"


def test_every_query_alias_resolves_to_owned_policy() -> None:
    query_aliases = {
        alias: target
        for alias, target in MODEL_ALIASES.items()
        if target not in {"symbol_decision_snapshots"}
    }
    missing = sorted(target for target in query_aliases.values() if target not in QUERY_POLICIES)
    assert missing == []


def test_today_action_query_bounds_payloads_and_handles_poisoned_rank(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    symbol = f"TQ{uuid4().hex[:8].upper()}"
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                [symbol, symbol],
            )
        decision = build_ticker_decision(
            symbol,
            {"decision_queue": [{
                "symbol": symbol,
                "stance": "NEUTRAL",
                "available_at": (as_of - timedelta(minutes=1)).isoformat(),
            }]},
            as_of=as_of,
        )
        published = TickerDecisionRepository(runtime).publish(decision)
        with runtime.transaction() as connection:
            manifest = dict(connection.execute(
                "SELECT input_manifest FROM analysis.ticker_decision WHERE id = %s::uuid",
                [published["ticker_decision_id"]],
            ).fetchone()["input_manifest"])
            manifest.update({
                "opportunity_rank": {"ticker": symbol, "trade_rank": "9" * 10_000},
                "trade_plan": {"padding": "x" * 400_000},
            })
            connection.execute(
                "UPDATE analysis.ticker_decision SET input_manifest = %s WHERE id = %s::uuid",
                [Jsonb(manifest), published["ticker_decision_id"]],
            )

        rows = load_postgres_tables(
            typed_config(migrated_postgres_dsn),
            ("today_ticker_actions",),
            query_row_limits={"today_ticker_actions": 100},
        )[0]["today_ticker_actions"]

        row = next(item for item in rows if item["ticker"] == symbol)
        assert row["opportunity_rank"]["trade_rank"] == "9" * 10_000
        assert row["trade_plan"] is None
        assert not {
            "input_manifest", "market_state_snapshot", "portfolio_impacts",
            "tactical", "fundamental", "expressions",
        }.intersection(row)
    finally:
        runtime.close()


def test_ticker_governance_projection_binds_one_options_radar_revision(
    migrated_postgres_dsn: str,
) -> None:
    upgrade_database(migrated_postgres_dsn)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        options_key = f"phase7-panel-options-{uuid4()}"
        unrelated_key = f"phase7-panel-unrelated-{uuid4()}"
        with runtime.transaction() as connection:
            options_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group) "
                "VALUES (%s, 1, %s, 'active', %s, 'options-radar-core') RETURNING id",
                [options_key, options_key, Jsonb({})],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group) "
                "VALUES (%s, 1, %s, 'active', %s, 'unrelated-strategy')",
                [unrelated_key, unrelated_key, Jsonb({})],
            )
        tables, _ = load_postgres_tables(
            typed_config(migrated_postgres_dsn), ("ticker_policy_learning",),
        )
        first = tables["ticker_policy_learning"][0]
        assert first["governance_authority"] == "available"
        assert first["governance_strategy_revision_id"] == options_id
        assert first["governance_strategy_key"] == options_key

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'candidate' WHERE id = %s",
                [options_id],
            )
        tables, _ = load_postgres_tables(
            typed_config(migrated_postgres_dsn), ("ticker_policy_learning",),
        )
        second = tables["ticker_policy_learning"][0]
        assert second["governance_authority"] == "unavailable"
        assert second["governance_strategy_revision_id"] is None
        assert second["governance_evaluations"] == []
    finally:
        runtime.close()
