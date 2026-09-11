from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import typed_config
from investment_panel.domain.research.stock_alpha import TARGET_VERSION
from investment_panel.domain.decision import build_ticker_decision
from investment_panel.infrastructure.postgres.decision_inbox import DecisionInboxRepository
from investment_panel.infrastructure.postgres.research_summary import research_summary
from investment_panel.infrastructure.postgres.runtime import API_PROFILE, DatabaseRuntime
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository


def test_research_summary_reads_latest_evidence_and_feedback_separately(
    migrated_postgres_dsn, application_postgres_dsn,
):
    owner = DatabaseRuntime(migrated_postgres_dsn)
    runtime = DatabaseRuntime(application_postgres_dsn)
    owner.open()
    runtime.open()
    now = datetime.now(UTC) - timedelta(minutes=1)
    try:
        with owner.transaction() as connection:
            strategy_id = connection.execute("""
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group, created_at)
                VALUES ('summary-test', 1, 'Summary test strategy', 'draft', '{}', 'summary-test', %s)
                RETURNING id
            """, [now]).fetchone()["id"]
            for offset, sample in [(-2, 8), (-1, 12), (10, 900)]:
                connection.execute("""
                    INSERT INTO analysis.strategy_evaluation
                        (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics, evidence)
                    VALUES (%s, 'out_of_sample', %s, %s, 'incomplete', %s, %s)
                """, [strategy_id, now + timedelta(minutes=offset), now + timedelta(minutes=offset),
                    Jsonb({"target_version": TARGET_VERSION, "effective_sample_size": 120, "oos_sample_size": sample,
                        "lower_confidence_net_utility_after_costs": 0.04,
                        "validation": {"gates": {"sample_size": {"passed": False}}}}),
                    Jsonb({"walk_forward": True, "purge_embargo": True})])
            for offset in range(9):
                connection.execute("""
                    INSERT INTO analysis.strategy_evaluation
                        (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics, evidence)
                    VALUES (%s, 'strategy_signal', %s, %s, 'available', %s, '[]')
                """, [strategy_id, now - timedelta(seconds=offset + 1), now - timedelta(seconds=offset + 1),
                    Jsonb({"actionability": "research_only", "value": 0.1, "direction": "long"})])
            connection.execute("""
                INSERT INTO analysis.strategy_evaluation
                    (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics, evidence, lineage)
                VALUES (%s, 'strategy_signal', %s, %s, 'available', %s, '[]', %s)
            """, [strategy_id, now - timedelta(seconds=20), now - timedelta(seconds=20),
                Jsonb({"actionability": "research_only", "value": 0.2, "direction": "short"}),
                Jsonb({"mode": "replay"})])
            ids = []
            for state in ["open", "acknowledged", "review_complete"]:
                ids.append(str(connection.execute("""
                    INSERT INTO app.decision_inbox_item (dedupe_key, event_type, user_state, reviewed_at)
                    VALUES (%s, 'high_priority_research', %s, %s) RETURNING id
                """, [str(uuid4()), state, now if state == "review_complete" else None]).fetchone()["id"]))
        before = research_summary(runtime, typed_config(application_postgres_dsn))
        strategy = next(row for row in before["strategies"] if row["strategy_revision_id"] == strategy_id)
        oos = next(item for item in strategy["evaluations"] if item["stage"] == "out_of_sample")
        assert oos["independent_sample_count"] == 12
        assert sum(item["stage"] == "strategy_signal" for item in strategy["evaluations"]) == 8
        assert any(item["stage"] == "out_of_sample" for item in strategy["evaluations"])
        assert strategy["failed_gates"] == ["sample_size"]
        assert strategy["included_count"] is None
        assert strategy["excluded_count"] is None
        assert before["review_activity"] == {
            "total": 3, "acknowledged": 1, "completed": 1, "rated": 0,
            "helpful": 0, "not_helpful": 0, "window_days": 30, "helpful_rate": None,
        }
        inbox = DecisionInboxRepository(runtime)
        first = inbox.set_usefulness(ids[0], useful=True)
        same = inbox.set_usefulness(ids[0], useful=True)
        assert first == same
        inbox.set_usefulness(ids[1], useful=False)
        assert inbox.set_usefulness(str(uuid4()), useful=False) is None
        after = research_summary(runtime, typed_config(application_postgres_dsn))
        assert after["review_activity"] == {
            "total": 3, "acknowledged": 1, "completed": 1, "rated": 2,
            "helpful": 1, "not_helpful": 1, "window_days": 30, "helpful_rate": 0.5,
        }
        rows = {row["id"]: row for row in inbox.rows()["items"]}
        assert rows[ids[0]]["user_state"] == "open"
        assert rows[ids[1]]["user_state"] == "acknowledged"
        assert isinstance(rows[ids[0]]["usefulness_updated_at"], str)
        with runtime.snapshot() as connection:
            assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == f"{API_PROFILE.statement_timeout_ms // 1000}s"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.transaction() as connection:
                connection.execute("UPDATE app.decision_inbox_item SET payload = '{}' WHERE id = %s::uuid", [ids[0]])
    finally:
        runtime.close()
        owner.close()


def test_research_counts_the_trial_universe_and_limits_current_ideas(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    now = datetime.now(UTC) - timedelta(minutes=1)
    try:
        with runtime.transaction() as connection:
            hypothesis = connection.execute("""
                INSERT INTO analysis.hypothesis
                    (hypothesis_key, statement, mechanism_class, falsification, input_hash)
                VALUES ('summary', 'Demand improves.', 'growth', 'Sales decline.', %s) RETURNING id
            """, ["a" * 64]).fetchone()["id"]
            family = connection.execute("""
                INSERT INTO analysis.experiment_family (hypothesis_id, family_key, name, input_hash)
                VALUES (%s, 'summary', 'Summary family', %s) RETURNING id
            """, [hypothesis, "b" * 64]).fetchone()["id"]
            revision = connection.execute("""
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group, hypothesis_id, experiment_family_id)
                VALUES ('summary', 1, 'Summary', 'draft', '{}', 'summary', %s, %s) RETURNING id
            """, [hypothesis, family]).fetchone()["id"]
            trial = connection.execute("""
                INSERT INTO analysis.research_trial (experiment_family_id, trial_key, input_cutoff, code_version, input_hash)
                VALUES (%s, 'trial', %s, 'test', %s) RETURNING id
            """, [family, now, "c" * 64]).fetchone()["id"]
            connection.execute("""
                INSERT INTO analysis.trial_universe_manifest
                    (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash)
                VALUES (%s, %s, 4, '[]', %s)
            """, [trial, now, "d" * 64])
            connection.execute("""
                INSERT INTO analysis.trial_result (research_trial_id, result_kind, observed_at, available_at, outcome, input_hash)
                VALUES (%s, 'validation', %s, %s, %s, %s)
            """, [trial, now, now, Jsonb({"gates": {"denominator_complete": {"passed": False}}}), "e" * 64])
            instruments = {}
            for symbol in ["HELD", "WATCHONE", "WATCHTWO", "BADRANK", "FUTURE"]:
                instruments[symbol] = connection.execute("""
                    INSERT INTO catalog.instrument (symbol, name, asset_class)
                    VALUES (%s, %s, 'equity') RETURNING id
                """, [symbol, symbol]).fetchone()["id"]
                connection.execute("INSERT INTO app.watchlist_item (instrument_id, watch_state) VALUES (%s, 'watching')", [instruments[symbol]])
            connection.execute("INSERT INTO app.portfolio_position (instrument_id, quantity, average_cost) VALUES (%s, 2, 10)", [instruments["HELD"]])
            for symbol, eligible in [("HELD", True), ("WATCHONE", True), ("BADRANK", False)]:
                connection.execute("""
                    INSERT INTO analysis.universe_observation
                        (research_trial_id, instrument_id, cutoff, eligible, exclusion_reason, observed_at, available_at, input_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, [trial, instruments[symbol], now, eligible, None if eligible else "missing_quote", now, now, "f" * 64])
        repository = TickerDecisionRepository(runtime)
        for symbol, rank in [("HELD", 99), ("WATCHONE", 1), ("WATCHTWO", 2), ("BADRANK", -1), ("FUTURE", 1)]:
            as_of = now + timedelta(days=1) if symbol == "FUTURE" else now
            decision = build_ticker_decision(symbol, {"decision_queue": [{"symbol": symbol, "stance": "NEUTRAL", "available_at": (now - timedelta(minutes=1)).isoformat()}]}, as_of=as_of)
            repository.publish(decision)
            with runtime.transaction() as connection:
                connection.execute("""
                    UPDATE analysis.ticker_decision SET input_manifest = input_manifest || %s,
                        fundamental = fundamental || %s WHERE decision_revision = %s
                """, [Jsonb({"opportunity_rank": {"research_rank": rank}, "inputs": {"theses": [{"thesis_json": {
                    "core_thesis": "Demand improves.", "scenarios": {"bear": {"rationale": "Sales remain weak."}},
                    "catalysts": [{"title": "Next earnings"}], "invalidation_rules": [{"text": "Sales decline."}],
                }}]}}), Jsonb({"invalidation": None}), decision.decision_revision])
        summary = research_summary(runtime, typed_config(migrated_postgres_dsn))
        strategy = next(row for row in summary["strategies"] if row["strategy_revision_id"] == revision)
        assert (strategy["included_count"], strategy["excluded_count"], strategy["expected_count"]) == (2, 1, 4)
        assert strategy["failed_gates"] == ["denominator_complete"]
        assert [idea["ticker"] for idea in summary["ideas"]] == ["HELD", "WATCHONE", "WATCHTWO"]
        held = summary["ideas"][0]
        assert held["holding_weight_pct"] is None  # Cost basis is not a current valuation.
        assert held["thesis"] == "Demand improves."
        assert held["countercase"] == "Sales remain weak."
        assert held["invalidation"] == "Sales decline."
        assert held["catalyst"] == "Next earnings"
    finally:
        runtime.close()


def test_research_terminal_window_requires_a_new_trial_and_preserves_unknowns(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    now = datetime.now(UTC) - timedelta(minutes=1)
    try:
        with runtime.transaction() as connection:
            strategy_id = connection.execute("""
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group)
                VALUES ('terminal-summary', 1, 'Terminal summary', 'candidate', '{}', 'options-radar-core')
                RETURNING id
            """).fetchone()["id"]
            for offset, verdict in [(-1, "collecting_data"), (0, "blocked_terminal_evidence")]:
                connection.execute("""
                    INSERT INTO analysis.strategy_evaluation
                        (strategy_revision_id, evaluation_type, evaluated_at, available_at,
                         period_start, period_end, verdict, metrics, evidence)
                    VALUES (%s, 'shadow', %s, %s, %s, %s, %s, %s, %s)
                """, [strategy_id, now + timedelta(minutes=offset), now + timedelta(minutes=offset),
                    now - timedelta(days=31), now - timedelta(days=1), verdict,
                    Jsonb({"proposed": {"sample_size": 30}, "comparison_denominator": 31,
                        "unmatched_episodes": 1, "comparison_window_complete": True,
                        "comparison_lower_95": None, "cohort": {"terminal_unmatched_episodes": 1}}),
                    Jsonb({"options_comparison_version": "options-independent-comparison-v1"})])
        summary = research_summary(runtime, typed_config(application_postgres_dsn))
        strategy = next(row for row in summary["strategies"] if row["strategy_revision_id"] == strategy_id)
        stage = strategy["evaluations"][0]
        assert strategy["status"] == "candidate"
        assert strategy["failed_gates"] == ["shadow: blocked_terminal_evidence"]
        assert "terminal evidence gap" in strategy["next_observation"]
        assert "Preserve its results" in strategy["next_observation"]
        assert "new prospective trial" in strategy["next_observation"]
        assert stage["independent_sample_count"] == 30
        assert stage["comparison_denominator"] == 31 and stage["unmatched_episodes"] == 1
        assert stage["comparison_window_complete"] is True
        assert stage["net_return_lower_bound"] is None and stage["brier_score"] is None
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) AS count FROM analysis.strategy_evaluation WHERE strategy_revision_id = %s",
                [strategy_id],
            ).fetchone()["count"] == 2
    finally:
        runtime.close()
