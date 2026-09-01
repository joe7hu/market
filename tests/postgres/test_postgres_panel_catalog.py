from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.data_access.loaders import load_panel_scope_data, today_plan_for_row, today_rank_for_row
from app.data_access.payloads import panel_snapshot_payload
from conftest import typed_config
from investment_panel.core.decision import (
    bind_trade_plan,
    build_ticker_decision,
    build_trade_plan,
    trade_expression_identity,
)
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.panel_models import (
    MODEL_ALIASES,
    QUERY_POLICIES,
    load_postgres_tables,
    today_authority_pages,
)
import investment_panel.database.panel_models as panel_models
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository


def test_panel_query_catalog_owns_alias_and_symbol_scope_policy() -> None:
    assert MODEL_ALIASES["ticker_memos"] == "research_packets"
    assert QUERY_POLICIES["research_packets"].symbol_scoped is True
    assert QUERY_POLICIES["research_packets"].exclude_future_rows is True
    assert QUERY_POLICIES["catalysts"].allow_symbol_less is True
    assert QUERY_POLICIES["options_ticker_signals"].custom_loader == "options_ticker_signals"


def test_screener_is_candidate_bounded_and_compact(migrated_postgres_dsn: str) -> None:
    tables, metadata = load_postgres_tables(
        typed_config(migrated_postgres_dsn),
        ("screener",),
        query_row_limits={"screener": 5},
    )

    assert len(tables["screener"]) <= 5
    assert metadata["table_counts"]["screener"] >= len(tables["screener"])
    assert all("__panel_total_count" not in row for row in tables["screener"])


def test_screener_keeps_the_api_maximum_page_window() -> None:
    captured: list[str] = []

    class Result:
        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            return []

    class Connection:
        def execute(self, query: str) -> Result:
            captured.append(query)
            return Result()

    panel_models._universe_screen_rows(Connection(), limit=10_500)

    assert len(captured) == 1
    assert "candidate_rank <= 10500" in captured[0]
    assert "candidate_rank <= 500" not in captured[0]


def test_ticker_option_queries_keep_symbol_filter_before_dense_joins() -> None:
    captured: list[tuple[str, object]] = []

    class Result:
        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            return []

    class Connection:
        def execute(self, query: str, parameters: object = None) -> Result:
            captured.append((query, parameters))
            return Result()

    connection = Connection()
    assert panel_models._liquidity_rows(connection, symbols={"QQQ"}, limit=24) == []
    assert panel_models._options_payoff_scenario_rows(connection, symbols={"QQQ"}, limit=24) == []

    liquidity_query, liquidity_parameters = captured[0]
    payoff_query, payoff_parameters = captured[1]
    assert "snapshot.history_symbol IN (SELECT symbol FROM requested_symbols)" in liquidity_query
    assert "latest_snapshots AS MATERIALIZED" in liquidity_query
    assert liquidity_parameters == [["QQQ"], 24]
    assert "candidate_instruments AS MATERIALIZED" in payoff_query
    assert "WHERE instrument.symbol = ANY(%s)" in payoff_query
    assert payoff_query.index("candidate_instruments AS MATERIALIZED") < payoff_query.index("JOIN analysis.option_decision")
    assert "LIMIT 200" not in payoff_query
    assert payoff_query.rindex("JOIN raw.option_quote") < payoff_query.rindex("LIMIT %s")
    assert payoff_query.rindex("ORDER BY decision.as_of DESC, decision.rank") < payoff_query.rindex("LIMIT %s")
    assert payoff_parameters == [["QQQ"], 24]


def test_symbol_scoped_technicals_keep_positive_close_guard() -> None:
    captured: list[tuple[str, object]] = []

    class Result:
        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            return []

    class Connection:
        def execute(self, query: str, parameters: object = None) -> Result:
            captured.append((query, parameters))
            return Result()

    assert panel_models.technical_rows(Connection(), symbols={"QQQ"}) == []

    query, parameters = captured[0]
    assert query.count("WHERE fact.interval = '1d' AND fact.close > 0") == 2
    assert parameters == [["QQQ"]]


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


def test_today_action_limit_keeps_exact_missing_plan_backlog_count(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    prefix = f"TB{uuid4().hex[:5].upper()}"
    symbols = [f"{prefix}{index:03d}" for index in range(105)]
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    try:
        instrument_ids: list[int] = []
        with runtime.transaction() as connection:
            for symbol in symbols:
                instrument_ids.append(connection.execute(
                    "INSERT INTO catalog.instrument (symbol, name, asset_class) "
                    "VALUES (%s, %s, 'equity') RETURNING id",
                    [symbol, symbol],
                ).fetchone()["id"])

        template = build_ticker_decision(
            symbols[0],
            {"decision_queue": [{
                "symbol": symbols[0],
                "stance": "NEUTRAL",
                "available_at": (as_of - timedelta(minutes=1)).isoformat(),
            }]},
            as_of=as_of,
        )
        published = TickerDecisionRepository(runtime).publish(template)
        with runtime.transaction() as connection:
            template_row = connection.execute(
                "SELECT input_manifest FROM analysis.ticker_decision WHERE id = %s::uuid",
                [published["ticker_decision_id"]],
            ).fetchone()
            for index, instrument_id in enumerate(instrument_ids):
                manifest = dict(template_row["input_manifest"])
                if index < 100:
                    revision = template.decision_revision if index == 0 else f"{template.decision_revision}:{index}"
                    episode_id = (
                        template.opportunity_episode_id
                        if index == 0
                        else f"{template.opportunity_episode_id}:{index}"
                    )
                    manifest.update({
                        "opportunity_rank": {
                            "ticker": symbols[index],
                            "decision_revision": revision,
                            "opportunity_episode_id": episode_id,
                            "trade_rank": index + 1,
                            "research_rank": index + 1,
                        },
                        "trade_plan": {"present": True},
                    })
                else:
                    manifest.pop("opportunity_rank", None)
                    manifest.pop("trade_plan", None)
                    if 102 <= index < 104:
                        manifest["trade_plan"] = {"present": "malformed-outside-sample"}
                    elif index == 104:
                        manifest["opportunity_rank"] = {
                            "ticker": "WRONG",
                            "decision_revision": f"{template.decision_revision}:{index}",
                            "opportunity_episode_id": f"{template.opportunity_episode_id}:{index}",
                            "trade_rank": index + 1,
                            "research_rank": index + 1,
                        }
                if index == 0:
                    connection.execute(
                        "UPDATE analysis.ticker_decision SET input_manifest = %s WHERE id = %s::uuid",
                        [Jsonb(manifest), published["ticker_decision_id"]],
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO analysis.ticker_decision (
                        instrument_id, decision_revision, contract_version, as_of,
                        published_at, input_hash, code_version, experiment_id,
                        tactical, fundamental, capital_action, resolution, policy_version,
                        opportunity_episode_id, opportunity_cutoff, opportunity_episode,
                        risk_policy, expressions, selected_expression, data_requests,
                        learning_history, input_manifest, market_state_publication_id,
                        market_state_snapshot, portfolio_impacts, risk_policy_snapshot, status
                    )
                    SELECT %s, decision_revision || %s, contract_version, as_of,
                           published_at, input_hash, code_version, experiment_id,
                           tactical, fundamental, capital_action, resolution, policy_version,
                           opportunity_episode_id || %s, opportunity_cutoff, opportunity_episode,
                           risk_policy, expressions, selected_expression, data_requests,
                           learning_history, %s, market_state_publication_id,
                           market_state_snapshot, portfolio_impacts, risk_policy_snapshot, status
                    FROM analysis.ticker_decision
                    WHERE id = %s::uuid
                    """,
                    [
                        instrument_id,
                        f":{index}",
                        f":{index}",
                        Jsonb(manifest),
                        published["ticker_decision_id"],
                    ],
                )

        config = typed_config(migrated_postgres_dsn)
        tables, metadata = load_postgres_tables(
            config,
            ("today_ticker_actions",),
            query_row_limits={"today_ticker_actions": 100},
        )
        rows = tables["today_ticker_actions"]

        assert len(rows) == 100
        assert metadata["table_counts"]["today_ticker_actions"] == 105
        assert all("__panel_total_count" not in row for row in rows)
        assert {row["missing_plan_count"] for row in rows} == {2}
        assert {row["opportunity_rank_count"] for row in rows} == {101}
        assert {row["trade_plan_count"] for row in rows} == {102}
        assert all(row["opportunity_rank"]["research_rank"] is not None for row in rows)
        assert all(row["trade_plan"].get("present") is True for row in rows)

        panel = load_panel_scope_data(config, "today")
        snapshot = panel_snapshot_payload(panel, "today")

        assert len(panel.rows("ticker_decisions")) == 3
        assert panel.metadata["today_action_input_count"] == 3
        assert {
            name: panel.metadata["table_counts"][name]
            for name in ("ticker_decisions", "opportunity_rank", "trade_plan")
        } == {
            "ticker_decisions": 105,
            "opportunity_rank": 101,
            "trade_plan": 102,
        }
        for table_name, total in (
            ("ticker_decisions", 105),
            ("opportunity_rank", 101),
            ("trade_plan", 102),
        ):
            assert snapshot["tables"][table_name]["count"] == total
            assert len(snapshot["tables"][table_name]["rows"]) == 3
        assert panel.metadata["today_missing_plan_count"] == 5
        assert all(
            "opportunity_rank_count" not in row and "trade_plan_count" not in row
            for row in panel.rows("ticker_decisions")
        )
        assert all(
            "opportunity_rank" not in row and "trade_plan" not in row
            for row in panel.rows("ticker_decisions")
        )

        sparse_panel = load_panel_scope_data(config, "today", offset=100, limit=1)
        sparse_snapshot = panel_snapshot_payload(sparse_panel, "today", offset=100, limit=1)

        assert len(sparse_panel.rows("ticker_decisions")) == 1
        assert len(sparse_panel.rows("opportunity_rank")) == 1
        assert len(sparse_panel.rows("trade_plan")) == 1
        assert sparse_panel.metadata["table_offsets"] == {
            "ticker_decisions": 100,
            "opportunity_rank": 100,
            "trade_plan": 100,
        }
        assert sparse_panel.rows("ticker_decisions")[0]["resolution"] is not None
        assert sparse_snapshot["tables"]["ticker_decisions"]["rows"][0]["ticker"] == symbols[104]
        assert sparse_snapshot["tables"]["opportunity_rank"]["rows"][0]["ticker"] == "WRONG"
        assert sparse_snapshot["tables"]["trade_plan"]["rows"][0]["present"] == "malformed-outside-sample"
        assert {
            name: sparse_snapshot["tables"][name]["count"]
            for name in ("ticker_decisions", "opportunity_rank", "trade_plan")
        } == {"ticker_decisions": 105, "opportunity_rank": 101, "trade_plan": 102}

        empty_page = load_panel_scope_data(config, "today", offset=10_000, limit=1)
        empty_snapshot = panel_snapshot_payload(empty_page, "today", offset=10_000, limit=1)

        assert all(not empty_page.rows(name) for name in ("ticker_decisions", "opportunity_rank", "trade_plan"))
        assert {
            name: empty_snapshot["tables"][name]["count"]
            for name in ("ticker_decisions", "opportunity_rank", "trade_plan")
        } == {"ticker_decisions": 105, "opportunity_rank": 101, "trade_plan": 102}
    finally:
        runtime.close()


def test_today_page_hydrates_plan_for_selected_decision_when_plan_stream_is_sparse(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    prefix = f"TP{uuid4().hex[:7].upper()}"
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    try:
        for index in (1, 2):
            symbol = f"{prefix}{index}"
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
            selected = decision.selected_expression
            assert selected is not None
            impact = decision.portfolio_impacts.get(selected.kind)
            publication_id = str(uuid4())
            rank = {
                "rank_id": f"rank:{symbol.lower()}",
                "ticker": symbol,
                "decision_revision": decision.decision_revision,
                "opportunity_episode_id": decision.opportunity_episode_id,
                "policy_version": decision.policy_version,
                "selected_expression_kind": selected.kind.value,
                "selected_expression_identity": trade_expression_identity(selected),
                "portfolio_impact_id": impact.impact_id if impact is not None else None,
                "market_state_publication_id": decision.market_state_publication_id,
                "ranking_publication_id": publication_id,
                "trade_rank": index,
                "research_rank": index,
                "evaluated_universe_complete": True,
                "trade_utility": 1.0,
            }
            if index == 1:
                published = TickerDecisionRepository(runtime).publish(
                    decision.model_copy(update={"opportunity_rank": rank})
                )
            else:
                plan = build_trade_plan(
                    decision=decision,
                    rank=rank,
                    publication_id=publication_id,
                )
                published = TickerDecisionRepository(runtime).publish(
                    bind_trade_plan(decision, plan).model_copy(update={"opportunity_rank": rank})
                )
            assert published["ticker_decision_id"]

        panel = load_panel_scope_data(
            typed_config(migrated_postgres_dsn), "today", offset=1, limit=1,
        )

        decision_row = panel.rows("ticker_decisions")[0]
        assert decision_row["ticker"] == f"{prefix}2"
        rank = today_rank_for_row(decision_row, panel.rows("opportunity_rank"), f"{prefix}2")
        assert rank is not None
        assert rank["evaluated_universe_complete"] is True
        assert rank["trade_utility"] == 1.0
        assert today_plan_for_row(decision_row, panel.rows("trade_plan"), rank, f"{prefix}2") is not None
        assert panel.rows("trade_plan") == []
    finally:
        runtime.close()


def test_today_authority_validates_plan_authority_without_returning_full_plan(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    symbol = f"TV{uuid4().hex[:8].upper()}"
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) "
                "VALUES (%s, %s, 'equity')",
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
        selected = decision.selected_expression
        assert selected is not None
        bundle_id = str(uuid4())
        impact = decision.portfolio_impacts.get(selected.kind)
        rank = {
            "rank_id": f"rank:{symbol.lower()}",
            "ticker": symbol,
            "decision_revision": decision.decision_revision,
            "opportunity_episode_id": decision.opportunity_episode_id,
            "policy_version": decision.policy_version,
            "selected_expression_kind": selected.kind.value,
            "selected_expression_identity": trade_expression_identity(selected),
            "portfolio_impact_id": impact.impact_id if impact is not None else None,
            "market_state_publication_id": decision.market_state_publication_id,
            "ranking_publication_id": bundle_id,
            "research_rank": None,
        }
        plan = build_trade_plan(
            decision=decision,
            rank=rank,
            publication_id=bundle_id,
        )
        bound = bind_trade_plan(decision, plan).model_copy(
            update={"opportunity_rank": rank},
        )
        published = TickerDecisionRepository(runtime).publish(bound)

        def authority_row() -> dict[str, object]:
            return next(
                row
                for page in today_authority_pages(
                    typed_config(migrated_postgres_dsn),
                    decision_limit=100,
                    rank_limit=1,
                    plan_limit=1,
                )
                for row in page
                if row["ticker"] == symbol
            )

        row = authority_row()
        assert row["validation_plan_valid"] is True
        assert "validation_plan" not in row
        valid_missing_count = load_panel_scope_data(
            typed_config(migrated_postgres_dsn), "today",
        ).metadata["today_missing_plan_count"]

        with runtime.transaction() as connection:
            manifest = dict(connection.execute(
                "SELECT input_manifest FROM analysis.ticker_decision "
                "WHERE id = %s::uuid",
                [published["ticker_decision_id"]],
            ).fetchone()["input_manifest"])
            base_rank = dict(manifest["opportunity_rank"])
            base_plan = dict(manifest["trade_plan"])
            null_identity_manifest = {
                **manifest,
                "opportunity_rank": {
                    **base_rank,
                    "portfolio_impact_id": None,
                    "market_state_publication_id": None,
                },
                "trade_plan": {
                    **base_plan,
                    "portfolio_impact_id": None,
                    "market_state_publication_id": None,
                },
            }
            connection.execute(
                "UPDATE analysis.ticker_decision SET input_manifest = %s "
                "WHERE id = %s::uuid",
                [Jsonb(null_identity_manifest), published["ticker_decision_id"]],
            )

        assert authority_row()["validation_plan_valid"] is False
        null_identity_missing_count = load_panel_scope_data(
            typed_config(migrated_postgres_dsn), "today",
        ).metadata["today_missing_plan_count"]
        assert null_identity_missing_count == valid_missing_count + 1

        with runtime.transaction() as connection:
            malformed_rank_manifest = {
                **manifest,
                "opportunity_rank": {**base_rank, "research_rank": "0"},
            }
            malformed_rank_manifest.pop("trade_plan", None)
            connection.execute(
                "UPDATE analysis.ticker_decision SET input_manifest = %s "
                "WHERE id = %s::uuid",
                [Jsonb(malformed_rank_manifest), published["ticker_decision_id"]],
            )

        malformed_rank_row = authority_row()
        assert malformed_rank_row["raw_research_rank_present"] is False
        assert malformed_rank_row["needs_missing_plan_validation"] is False
        malformed_rank_missing_count = load_panel_scope_data(
            typed_config(migrated_postgres_dsn), "today",
        ).metadata["today_missing_plan_count"]
        assert malformed_rank_missing_count == valid_missing_count + 1

        with runtime.transaction() as connection:
            poisoned_manifest = {
                **manifest,
                "trade_plan": {
                    **base_plan,
                    "decision_revision": "revision:poisoned",
                },
            }
            connection.execute(
                "UPDATE analysis.ticker_decision SET input_manifest = %s "
                "WHERE id = %s::uuid",
                [Jsonb(poisoned_manifest), published["ticker_decision_id"]],
            )

        assert authority_row()["validation_plan_valid"] is False
        invalid_missing_count = load_panel_scope_data(
            typed_config(migrated_postgres_dsn), "today",
        ).metadata["today_missing_plan_count"]
        assert invalid_missing_count == valid_missing_count + 1
    finally:
        runtime.close()


def test_today_authority_cursor_keeps_base_and_correction_in_one_snapshot(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    prefix = f"TS{uuid4().hex[:7].upper()}"
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    try:
        published_ids: list[str] = []
        for suffix in ("A", "B"):
            symbol = f"{prefix}{suffix}"
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
            published_ids.append(TickerDecisionRepository(runtime).publish(decision)["ticker_decision_id"])
        with runtime.transaction() as connection:
            manifest = dict(connection.execute(
                "SELECT input_manifest FROM analysis.ticker_decision WHERE id = %s::uuid",
                [published_ids[1]],
            ).fetchone()["input_manifest"])
            manifest["trade_plan"] = {"present": "before-cursor-mutation"}
            connection.execute(
                "UPDATE analysis.ticker_decision SET input_manifest = %s WHERE id = %s::uuid",
                [Jsonb(manifest), published_ids[1]],
            )

        pages = today_authority_pages(
            typed_config(migrated_postgres_dsn),
            decision_limit=100,
            rank_limit=3,
            plan_limit=3,
            batch_size=1,
        )
        first_page = next(pages)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.ticker_decision "
                "SET input_manifest = input_manifest - 'trade_plan' WHERE id = %s::uuid",
                [published_ids[1]],
            )
        rows = [*first_page, *(row for page in pages for row in page)]

        assert {row["missing_plan_count"] for row in rows} == {1}
        mutated_row = next(row for row in rows if row["ticker"] == f"{prefix}B")
        assert mutated_row["invalid_without_rank"] is True
        assert mutated_row["trade_plan_page"]["present"] == "before-cursor-mutation"
        assert rows[0]["missing_plan_count"] + sum(row["invalid_without_rank"] for row in rows) == 2
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
