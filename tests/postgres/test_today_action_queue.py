from datetime import UTC, datetime, timedelta

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
