from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from app.data_access.loaders import load_postgres_tables
from investment_panel.core.decision import build_ticker_decision
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from conftest import typed_config


def _decision(symbol: str, as_of: datetime):
    return build_ticker_decision(
        symbol,
        {"decision_queue": [{"symbol": symbol, "stance": "NEUTRAL", "available_at": (as_of - timedelta(minutes=1)).isoformat()}]},
        as_of=as_of,
    )


def _insert_instruments(runtime: DatabaseRuntime, symbols: list[str]) -> None:
    with runtime.transaction() as connection:
        for symbol in symbols:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                [symbol, f"{symbol} test instrument"],
            )


def _duplicate_revision(runtime: DatabaseRuntime, revision: str) -> None:
    with runtime.transaction() as connection:
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
            SELECT instrument_id, decision_revision || ':duplicate', contract_version, as_of,
                   published_at, input_hash, code_version, experiment_id,
                   tactical, fundamental, capital_action, resolution, policy_version,
                   opportunity_episode_id, opportunity_cutoff, opportunity_episode,
                   risk_policy, expressions, selected_expression, data_requests,
                   learning_history, input_manifest, market_state_publication_id,
                   market_state_snapshot, portfolio_impacts, risk_policy_snapshot, status
            FROM analysis.ticker_decision
            WHERE decision_revision = %s
            """,
            [revision],
        )


def _duplicate_episode_with_new_timestamp(runtime: DatabaseRuntime, revision: str) -> None:
    with runtime.transaction() as connection:
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
            SELECT instrument_id, decision_revision || ':episode-duplicate', contract_version,
                   as_of + interval '1 minute', published_at + interval '1 minute',
                   input_hash, code_version, experiment_id,
                   tactical, fundamental, capital_action, resolution, policy_version,
                   opportunity_episode_id, opportunity_cutoff, opportunity_episode,
                   risk_policy, expressions, selected_expression, data_requests,
                   learning_history, input_manifest, market_state_publication_id,
                   market_state_snapshot, portfolio_impacts, risk_policy_snapshot, status
            FROM analysis.ticker_decision
            WHERE decision_revision = %s
            """,
            [revision],
        )


def test_current_ticker_selector_is_pit_valid_and_fails_closed_for_bad_authority(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    reference = datetime.now(UTC) - timedelta(hours=1)
    symbols = ["W1P6HIST", "W1P6FUTURE", "W1P6DUP", "W1P6BAD"]
    _insert_instruments(runtime, symbols)
    repository = TickerDecisionRepository(runtime)
    try:
        historical = _decision("W1P6HIST", reference - timedelta(days=2))
        current = _decision("W1P6HIST", reference)
        repository.publish(historical)
        repository.publish(current)

        future = _decision("W1P6FUTURE", reference + timedelta(days=1))
        repository.publish(future)

        duplicate = _decision("W1P6DUP", reference)
        repository.publish(duplicate)
        _duplicate_revision(runtime, duplicate.decision_revision)

        malformed = _decision("W1P6BAD", reference)
        repository.publish(malformed)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.ticker_decision SET contract_version = 'malformed' WHERE decision_revision = %s",
                [malformed.decision_revision],
            )

        rows = load_postgres_tables(typed_config(migrated_postgres_dsn), ("ticker_decisions",))[0]["ticker_decisions"]
        by_symbol = {row["ticker"]: row for row in rows}

        assert set(by_symbol) == {"W1P6HIST"}
        assert by_symbol["W1P6HIST"]["decision_revision"] == current.decision_revision
        assert repository.latest("W1P6FUTURE") is None
        assert repository.latest("W1P6DUP") is None
        assert repository.latest("W1P6BAD") is None
    finally:
        runtime.close()


def test_current_ticker_selector_rejects_duplicate_episode_across_timestamps(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    reference = datetime.now(UTC) - timedelta(hours=1)
    _insert_instruments(runtime, ["W1P6EPISODE"])
    repository = TickerDecisionRepository(runtime)
    try:
        decision = _decision("W1P6EPISODE", reference)
        repository.publish(decision)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.ticker_decision SET published_at = now() - interval '2 minutes' WHERE decision_revision = %s",
                [decision.decision_revision],
            )
        _duplicate_episode_with_new_timestamp(runtime, decision.decision_revision)

        assert repository.latest("W1P6EPISODE") is None
        rows = load_postgres_tables(typed_config(migrated_postgres_dsn), ("ticker_decisions",))[0]["ticker_decisions"]
        assert rows == []
    finally:
        runtime.close()


def test_compact_funnel_episode_validation_fails_closed(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    as_of = datetime.now(UTC) - timedelta(minutes=2)
    symbols = [
        "FUNNELVALID",
        "FUNNELEMPTY",
        "FUNNELFUTURE",
        "FUNNELCUTOFF",
        "FUNNELDUP",
        "FUNNELIDENTITY",
        "FUNNELNAIVE",
        "FUNNELNOSOURCE",
        "FUNNELROWCUTOFF",
        "FUNNELOVERSIZE",
        "FSELTERMS",
    ]
    _insert_instruments(runtime, symbols)
    repository = TickerDecisionRepository(runtime)
    try:
        decisions = {symbol: _decision(symbol, as_of) for symbol in symbols}
        for decision in decisions.values():
            repository.publish(decision)

        with runtime.transaction() as connection:
            # Make the fixture a required Funnel candidate so the following
            # mutations exercise episode validation instead of intentional
            # blocked-row omission.
            connection.execute(
                """
                UPDATE analysis.ticker_decision
                SET expressions = jsonb_set(
                        expressions, '{STOCK,availability_status}',
                        to_jsonb('available'::text), true
                    ),
                    opportunity_episode = jsonb_set(
                        opportunity_episode, '{expressions,STOCK,availability_status}',
                        to_jsonb('available'::text), true
                    )
                """
            )
            connection.execute(
                "UPDATE analysis.ticker_decision "
                "SET portfolio_impacts = jsonb_set("
                "portfolio_impacts, '{STOCK,market_state_publication_id}', 'null'::jsonb)"
            )
            for symbol in symbols[1:]:
                decision = decisions[symbol]
                row = connection.execute(
                    "SELECT opportunity_episode FROM analysis.ticker_decision "
                    "WHERE decision_revision = %s",
                    [decision.decision_revision],
                ).fetchone()
                episode = dict(row["opportunity_episode"])
                lineage = [dict(item) for item in episode["input_lineage"]]
                if symbol == "FUNNELEMPTY":
                    lineage = [{}]
                elif symbol == "FUNNELFUTURE":
                    lineage[0]["available_at"] = (as_of + timedelta(seconds=1)).isoformat()
                elif symbol == "FUNNELCUTOFF":
                    lineage[0]["cutoff"] = (as_of - timedelta(seconds=1)).isoformat()
                elif symbol == "FUNNELDUP":
                    lineage.append(dict(lineage[0]))
                elif symbol == "FUNNELIDENTITY":
                    lineage[0]["opportunity_episode_id"] = "wrong-episode"
                elif symbol == "FUNNELNAIVE":
                    lineage[0]["available_at"] = as_of.replace(tzinfo=None).isoformat()
                elif symbol == "FUNNELNOSOURCE":
                    lineage[0].pop("source_id")
                episode["input_lineage"] = lineage
                if symbol == "FUNNELOVERSIZE":
                    episode["oversize_padding"] = "x" * 262_145
                connection.execute(
                    "UPDATE analysis.ticker_decision SET opportunity_episode = %s "
                    "WHERE decision_revision = %s",
                    [Jsonb(episode), decision.decision_revision],
                )
                if symbol == "FSELTERMS":
                    connection.execute(
                        "UPDATE analysis.ticker_decision "
                        "SET selected_expression = jsonb_set("
                        "selected_expression, '{rationale}', to_jsonb(%s::text)) "
                        "WHERE decision_revision = %s",
                        ["Corrupt selected economic terms.", decision.decision_revision],
                    )
                if symbol == "FUNNELROWCUTOFF":
                    connection.execute(
                        "UPDATE analysis.ticker_decision SET opportunity_cutoff = %s "
                        "WHERE decision_revision = %s",
                        [as_of - timedelta(seconds=1), decision.decision_revision],
                    )

        reference = datetime.now(UTC) + timedelta(minutes=1)
        rows = repository._current_funnel_rows(reference=reference)
        assert {row["ticker"] for row in rows} == set(symbols)
        assert all("has_valid_opportunity_lineage" not in row for row in rows)
        rows_by_ticker = {row["ticker"]: row for row in rows}
        valid_row = rows_by_ticker["FUNNELVALID"]
        assert valid_row["opportunity_episode"] is not None
        assert valid_row["opportunity_cutoff_match"] is True
        assert valid_row["opportunity_expressions_match"] is True
        assert valid_row["opportunity_selected_expression_match"] is True
        assert rows_by_ticker["FUNNELROWCUTOFF"]["opportunity_cutoff_match"] is False
        assert rows_by_ticker["FUNNELOVERSIZE"]["opportunity_episode"] is None
        assert (
            rows_by_ticker["FSELTERMS"]["opportunity_selected_expression_match"]
            is False
        )

        funnel = repository.decision_funnel(now=reference)
        facts = next(
            stage for stage in funnel["stages"]
            if stage["stage"] == "point_in_time_facts"
        )
        assert facts["count"] == 1
        assert facts["top_blockers"][0] == {
            "reason": "ticker_decision_contract_invalid",
            "count": len(symbols) - 1,
            "affected_symbols": sorted(symbols[1:]),
        }
    finally:
        runtime.close()
