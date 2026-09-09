from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from conftest import typed_config
from investment_panel.domain.decision import market_session_bounds
from investment_panel.core.option_trade_ticket import build_option_trade_ticket
from investment_panel.domain.portfolio.risk_policy import PortfolioAssignmentPolicy, RiskPolicySnapshot
from investment_panel.infrastructure.postgres.actions import ActionRepository
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository, current_option_publication_answers
from investment_panel.infrastructure.postgres.confirmed_daily_prices import completed_trading_dates, confirmed_daily_bars
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.options_analysis import DEFAULT_PARAMETERS, FEATURE_VERSION, refresh_options_radar
from investment_panel.infrastructure.postgres.options_calibration import calibration_profiles
from investment_panel.infrastructure.postgres.options_paper_execution import PAPER_MARK_KEY, OptionsPaperExecutionRepository
from investment_panel.infrastructure.postgres.options_paper_ledger import shared_sleeve_blockers
from investment_panel.infrastructure.postgres.options_experiments import (
    EXPERIMENT_VERSION, advance_experiment_shadows, experiment_candidate, experiment_publication_row,
    experiment_identity,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.strategy_parameters import merge_strategy_parameters
from investment_panel.jobs.options_paper_execution import run_experiments


@pytest.fixture
def experiment_context(migrated_postgres_dsn, monkeypatch):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    now = datetime.now(UTC) - timedelta(seconds=2)
    ingestion = IngestionRepository(runtime)
    ingestion.register_source("test-experiment", name="Experiment test", family="test", kind="option_chain")
    with runtime.transaction() as connection:
        parent = connection.execute(
            "INSERT INTO analysis.strategy_revision (strategy_key, revision, name, status, parameters, authority_group, created_at, promoted_at) "
            "VALUES ('options-radar-core', 3, 'Incumbent', 'active', %s, 'options-radar-core', %s, %s) RETURNING id",
            [Jsonb(DEFAULT_PARAMETERS), now - timedelta(days=150), now - timedelta(days=150)],
        ).fetchone()["id"]
        changes = {"min_open_interest": 100}
        candidate = connection.execute(
            "INSERT INTO analysis.strategy_revision (strategy_key, revision, name, status, parameters, authority_group, supersedes_id, created_at) "
            "VALUES ('options-radar-core__experiment', 1, 'Candidate', 'candidate', %s, 'options-radar-core', %s, %s) RETURNING id",
            [Jsonb(merge_strategy_parameters(DEFAULT_PARAMETERS, changes)), parent, now - timedelta(hours=1)],
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO analysis.agent_task (task_kind, status, request, result, created_at) "
            "VALUES ('strategy_mutation_proposal', 'completed', '{}', %s, %s)",
            [Jsonb({"candidate_revision_id": candidate, "proposed_parameter_changes": changes}), now - timedelta(hours=1)],
        )
        connection.execute(
            "INSERT INTO analysis.strategy_evaluation (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics) "
            "VALUES (%s, 'walk_forward', %s, %s, 'pass', '{}')",
            [candidate, now - timedelta(minutes=59), now - timedelta(minutes=59)],
        )
    monkeypatch.setattr("investment_panel.infrastructure.postgres.options_experiments.is_market_open", lambda _: True)
    try:
        yield runtime, ingestion, now, parent, candidate
    finally:
        runtime.close()


def _capture(runtime, ingestion, at, *, bid=0.48, ask=0.50, complete=True, available_at=None, open_interest=1000, include_put=False, bid_size=10, ask_size=10, symbols=("NVDA",), contracts=()):
    with ingestion.run("test-experiment", "option_quotes", started_at=at) as run:
        snapshot = ingestion.store_option_snapshot(
            run.id, source_id="test-experiment", observed_at=at, market_session="regular", universe="test",
            rows=[{
                "symbol": symbol, "expiration": (at.date() + timedelta(days=40)).isoformat(),
                "strike": 160, "option_type": option_type, "underlying_price": 155,
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "bid_size": bid_size, "ask_size": ask_size,
                "volume": 1000, "open_interest": open_interest, "iv": 0.3, "delta": 0.4,
                "last_trade_at": at, "captured_at": at, "market_data_status": "live",
                "style": "american", "settlement": "physical", "deliverable_key": f"{symbol.lower()}-standard",
                "standard_contract_verified": True, **contract,
            } for symbol in symbols for option_type in (["call", "put"] if include_put else ["call"])
              for contract in (contracts or ({},))],
        )
    with runtime.transaction() as connection:
        connection.execute("UPDATE raw.option_snapshot SET capture_state = %s WHERE id = %s", ["complete" if complete else "partial", snapshot["snapshot_id"]])
        connection.execute("UPDATE raw.option_quote SET available_at = %s WHERE snapshot_id = %s", [available_at or at, snapshot["snapshot_id"]])
    return snapshot


def test_application_login_produces_and_advances_experiment_shadows(experiment_context, application_postgres_dsn):
    owner, ingestion, now, _parent, candidate = experiment_context
    application = DatabaseRuntime(application_postgres_dsn)
    application.open()
    try:
        with application.read() as connection:
            identity = connection.execute(
                "SELECT current_user, session_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            assert identity["current_user"] == identity["session_user"] == "market_app"
            assert identity["rolsuper"] is False and identity["rolbypassrls"] is False
            assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "3s"
            assert connection.execute("SELECT pg_get_userbyid(relowner) AS owner FROM pg_class WHERE oid = 'analysis.shadow_trade'::regclass").fetchone()["owner"] != "market_app"
        _capture(owner, ingestion, now)
        incumbent = refresh_options_radar(application, source_id="test-experiment", code_version="application-producer")
        assert incumbent["shadow_trades"] == 1
        enabled = typed_config(application_postgres_dsn, raw={"analysis": {"options_decision_system": {"strategy_auto_promotion_enabled": True}}})
        candidate_run = run_experiments(application, enabled)
        assert candidate_run["status"] == "ok" and candidate_run["candidate_revision_id"] == candidate
        assert candidate_run["publication"]["shadow_trades"] == 1
        _capture(owner, ingestion, now + timedelta(seconds=20))
        assert advance_experiment_shadows(application, now=now + timedelta(seconds=21))["entered"] == 2
        _capture(owner, ingestion, now + timedelta(seconds=40), bid=1.1, ask=1.12)
        completed = run_experiments(application, typed_config(application_postgres_dsn), now=now + timedelta(seconds=41))
        assert completed["status"] == "disabled" and completed["observations"]["closed"] == 2
        with application.read() as connection:
            outcomes = connection.execute(
                "SELECT shadow.status, shadow.entry_at, shadow.exit_at, outcome.objective_version, outcome.sample_eligible "
                "FROM analysis.shadow_trade shadow JOIN analysis.option_outcome outcome ON outcome.shadow_trade_id = shadow.id"
            ).fetchall()
            assert len(outcomes) == 2
            assert all(row["status"] == "closed" and row["entry_at"] < row["exit_at"] and row["sample_eligible"]
                       and row["objective_version"] == EXPERIMENT_VERSION for row in outcomes)
    finally:
        application.close()


def test_incumbent_and_candidate_gain_real_forward_calibration_before_promotion(experiment_context):
    runtime, ingestion, now, parent, candidate = experiment_context
    _capture(runtime, ingestion, now)
    incumbent = refresh_options_radar(runtime, source_id="test-experiment", code_version="experiment-test")
    challenger = refresh_options_radar(runtime, source_id="test-experiment", code_version="experiment-test", candidate_revision_id=candidate)
    assert incumbent["shadow_trades"] == 1 and challenger["shadow_trades"] == 1
    with runtime.read() as connection:
        shadows = connection.execute("SELECT source_kind, status, entry_at FROM analysis.shadow_trade ORDER BY created_at").fetchall()
        assert all(row["source_kind"] == "options_paper_experiment" and row["status"] == "pending" and row["entry_at"] is None for row in shadows)
        active = connection.execute("SELECT id FROM analysis.strategy_revision WHERE status = 'active'").fetchone()["id"]
        assert active == parent
        assert all(row["publication_id"] != challenger["publication_id"] for row in current_option_publication_answers(connection))
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=5))["entered"] == 0
    # Incomplete captures and quotes not yet available cannot become a shadow fill.
    _capture(runtime, ingestion, now + timedelta(seconds=30), complete=False)
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=31))["entered"] == 0
    _capture(runtime, ingestion, now + timedelta(seconds=60), available_at=now + timedelta(seconds=65))
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=64))["entered"] == 0
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=66))["entered"] == 2
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=67))["closed"] == 0
    _capture(runtime, ingestion, now + timedelta(seconds=90), bid=1.1, ask=1.12)
    disabled = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=SimpleNamespace(strategy_auto_promotion_enabled=False)))
    completed = run_experiments(runtime, disabled, now=now + timedelta(seconds=91))
    assert completed["status"] == "disabled" and completed["observations"]["closed"] == 2
    with runtime.read() as connection:
        outcomes = connection.execute(
            "SELECT outcome.*, shadow.metrics, shadow.source_kind FROM analysis.option_outcome outcome "
            "JOIN analysis.shadow_trade shadow ON shadow.id = outcome.shadow_trade_id"
        ).fetchall()
        assert len(outcomes) == 2
        for row in outcomes:
            assert row["objective_version"] == EXPERIMENT_VERSION and row["sample_eligible"] is True
            assert row["promotion_eligible"] is False
            assert row["entry_fill_at"] < row["exit_fill_at"]
            assert row["current_return"] == pytest.approx(((1.1 - .5) * 100 - 1.3) / 50)
            assert row["metrics"]["entry_quotes"][0]["quote_id"]
            assert row["metrics"]["exit_quotes"][0]["source_id"] == "test-experiment"
        assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [candidate]).fetchone()["status"] == "candidate"
        # Use the fixture's forward observation clock for the evaluator handoff.
        from investment_panel.infrastructure.postgres.strategy_learning import OBSERVATION_QUERY, OUTCOME_QUERY

        observed_query = OUTCOME_QUERY.replace("now()", f"TIMESTAMPTZ '{(now + timedelta(seconds=92)).isoformat()}'")
        for revision in (parent, candidate):
            measured = connection.execute(observed_query, [revision, revision]).fetchall()
            assert len(measured) == 1 and measured[0]["current_return"] > 0
        cohort_query = OBSERVATION_QUERY.replace("now()", f"TIMESTAMPTZ '{(now + timedelta(seconds=92)).isoformat()}'")
        cohort = connection.execute(cohort_query, [[parent, candidate], now - timedelta(hours=1)]).fetchall()
        assert len(cohort) == 2 and all(row["shadow_status"] == "closed" and row["payload"] for row in cohort)
    for revision in (parent, candidate):
        profiles = calibration_profiles(runtime, revision, feature_version=FEATURE_VERSION)
        assert profiles[0]["sample_size"] == 1 and profiles[0]["ready"] is False


def test_candidate_gate_parent_and_unobserved_paper_authority_stay_closed(experiment_context):
    runtime, ingestion, now, parent, candidate = experiment_context
    with runtime.read() as connection:
        assert experiment_candidate(connection, candidate, as_of=datetime.now(UTC))["id"] == candidate
        with pytest.raises(ValueError, match="passing shadow"):
            experiment_candidate(connection, candidate, as_of=datetime.now(UTC), require_shadow=True)
    _capture(runtime, ingestion, now)
    published = refresh_options_radar(runtime, source_id="test-experiment", code_version="experiment-test", candidate_revision_id=candidate)
    rows = AnalysisRepository(runtime).publication_rows(f"options-paper-experiment:{candidate}", "option_paper_experiment")
    with runtime.read() as connection:
        with pytest.raises(ValueError, match="passing shadow"):
            experiment_publication_row(connection, published["publication_id"], rows[0]["decision_id"], as_of=datetime.now(UTC))
    with runtime.transaction() as connection:
        connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent])
    with runtime.read() as connection:
        with pytest.raises(ValueError, match="parent"):
            experiment_candidate(connection, candidate, as_of=datetime.now(UTC))


@pytest.mark.parametrize("evaluation_type", ["shadow", "execution_grade_paper"])
def test_terminal_evidence_blocks_entries_but_preserves_existing_shadow_holdings(experiment_context, evaluation_type):
    runtime, ingestion, now, _parent, candidate = experiment_context
    with runtime.transaction() as connection:
        connection.execute(
            "INSERT INTO analysis.strategy_evaluation (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics) "
            "VALUES (%s, 'shadow', %s, %s, 'pass', '{}')", [candidate, now - timedelta(seconds=1), now - timedelta(seconds=1)],
        )
    _capture(runtime, ingestion, now, symbols=("NVDA", "AMD"))
    published = refresh_options_radar(runtime, source_id="test-experiment", code_version="terminal-entry-test", candidate_revision_id=candidate)
    _capture(runtime, ingestion, now + timedelta(seconds=20))
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=21))["entered"] == 1
    with runtime.transaction() as connection:
        connection.execute(
            "INSERT INTO analysis.strategy_evaluation (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics) "
            "VALUES (%s, %s, %s, %s, 'blocked_terminal_evidence', '{}')",
            [candidate, evaluation_type, now + timedelta(seconds=30), now + timedelta(seconds=30)],
        )
    with runtime.read() as connection:
        with pytest.raises(ValueError, match="blocked_terminal_evidence"):
            experiment_candidate(connection, candidate, as_of=now + timedelta(seconds=31))
        assert experiment_candidate(connection, candidate, as_of=now + timedelta(seconds=31), require_shadow=True, for_entry=False)["id"] == candidate
        decision_id = connection.execute(
            "SELECT shadow.decision_id FROM analysis.shadow_trade shadow WHERE shadow.status = 'entered'",
        ).fetchone()["decision_id"]
        with pytest.raises(ValueError, match="blocked_terminal_evidence"):
            experiment_publication_row(connection, published["publication_id"], str(decision_id), as_of=now + timedelta(seconds=31))
        assert experiment_publication_row(connection, published["publication_id"], str(decision_id), as_of=now + timedelta(seconds=31), for_entry=False)
    _capture(runtime, ingestion, now + timedelta(seconds=40), symbols=("NVDA", "AMD"))
    result = advance_experiment_shadows(runtime, now=now + timedelta(seconds=41))
    assert result == {"entered": 0, "closed": 0, "unfilled": 1, "unmeasurable": 0, "rejected": 0}
    with runtime.transaction() as connection:
        assert connection.execute("SELECT count(*) AS count FROM analysis.shadow_trade WHERE status = 'entered'").fetchone()["count"] == 1
        # A genuine later qualification failure still fails holding authority.
        connection.execute(
            "INSERT INTO analysis.strategy_evaluation (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics) "
            "VALUES (%s, 'shadow', %s, %s, 'fail', '{}')", [candidate, now + timedelta(seconds=50), now + timedelta(seconds=50)],
        )
        with pytest.raises(ValueError, match="passing shadow"):
            experiment_candidate(connection, candidate, as_of=now + timedelta(seconds=51), require_shadow=True, for_entry=False)


def test_rejected_independent_opportunities_remain_in_both_denominators(experiment_context):
    runtime, ingestion, now, parent, candidate = experiment_context
    _capture(runtime, ingestion, now, open_interest=0, include_put=True)
    for candidate_id in (None, candidate):
        result = refresh_options_radar(runtime, source_id="test-experiment", code_version="reject-test", candidate_revision_id=candidate_id)
        assert result["shadow_trades"] == 2
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT decision.strategy_revision_id, count(DISTINCT decision.episode_key) AS episodes, "
            "bool_and(shadow.status = 'rejected') AS all_rejected FROM analysis.shadow_trade shadow "
            "JOIN analysis.decision decision ON decision.id = shadow.decision_id GROUP BY decision.strategy_revision_id"
        ).fetchall()
        assert {row["strategy_revision_id"] for row in rows} == {parent, candidate}
        assert all(row["episodes"] == 2 and row["all_rejected"] for row in rows)
        assert connection.execute("SELECT count(*) AS count FROM analysis.option_outcome").fetchone()["count"] == 0
        from investment_panel.infrastructure.postgres.strategy_learning import OBSERVATION_QUERY

        cohort = connection.execute(OBSERVATION_QUERY, [[parent, candidate], now - timedelta(hours=1)]).fetchall()
        assert len(cohort) == 4 and all(row["shadow_status"] == "rejected" for row in cohort)


@pytest.mark.parametrize("legacy_pending", [False, True])
def test_score_rejections_stay_confirmed_cash_without_shadow_fills(experiment_context, monkeypatch, legacy_pending):
    from investment_panel.infrastructure.postgres.strategy_learning import OBSERVATION_QUERY, OUTCOME_QUERY, forward_cohort

    runtime, ingestion, now, parent, candidate = experiment_context
    reference = now

    class ObservationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return reference + timedelta(seconds=10)

    monkeypatch.setattr("investment_panel.infrastructure.postgres.options_analysis.datetime", ObservationClock)
    # Two explicit fixture sessions let the fixed cohort exclude its first
    # partial session and still retain a confirmed CASH observation per side.
    for reference in (now, now + timedelta(days=1)):
        _capture(runtime, ingestion, reference, open_interest=100, contracts=({"delta": 0},))
        for candidate_id in (None, candidate):
            result = refresh_options_radar(runtime, source_id="test-experiment", code_version="score-reject", candidate_revision_id=candidate_id)
            assert result["shadow_trades"] == 1
    with runtime.transaction() as connection:
        rows = connection.execute(
            "SELECT decision.state, decision.score, decision.sample_eligible, shadow.status "
            "FROM analysis.shadow_trade shadow JOIN analysis.decision decision ON decision.id = shadow.decision_id",
        ).fetchall()
        assert len(rows) == 4
        assert all(row["state"] == "REJECTED" and row["score"] < 55 and row["sample_eligible"] for row in rows)
        assert all(row["status"] == "rejected" for row in rows)
        if legacy_pending:
            connection.execute("UPDATE analysis.shadow_trade SET status = 'pending', pending_entry_reason = 'later_quote_required'")
    _capture(runtime, ingestion, reference + timedelta(seconds=20))
    advance = advance_experiment_shadows(runtime, now=reference + timedelta(seconds=21))
    assert advance["entered"] == 0 and advance["closed"] == 0 and advance["rejected"] == (4 if legacy_pending else 0)
    clock_sql = f"TIMESTAMPTZ '{(reference + timedelta(seconds=22)).isoformat()}'"
    with runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS count FROM analysis.option_outcome").fetchone()["count"] == 0
        observations = connection.execute(OBSERVATION_QUERY.replace("now()", clock_sql), [[parent, candidate], now - timedelta(hours=1)]).fetchall()
        assert len(observations) == 4 and all(row["shadow_status"] == "rejected" for row in observations)
        assert all(row["pending_entry_reason"] == "candidate_gate_rejected" and row["entry_at"] is None and row["entry_price"] is None for row in observations)
        assert all(connection.execute(OUTCOME_QUERY.replace("now()", clock_sql), [revision, revision]).fetchall() == [] for revision in (parent, candidate))
        proposal_id = str(experiment_candidate(connection, candidate, as_of=reference + timedelta(seconds=22))["proposal_id"])
    baseline, proposed, cohort = forward_cohort(observations, [], [], candidate_id=candidate, parent_id=parent, proposal_id=proposal_id, span_days=30)
    assert baseline == proposed == []
    assert len(cohort["baseline_cash_keys"]) == len(cohort["proposed_cash_keys"]) == 1
    assert cohort["window_complete"] is False


def test_candidate_and_promoted_revision_select_after_the_same_expectancy_scoring(experiment_context, monkeypatch):
    from investment_panel.infrastructure.postgres.options_expressions import enrich_long_option_expectancy

    runtime, ingestion, now, parent, candidate = experiment_context
    _capture(runtime, ingestion, now, contracts=(
        {"strike": 160, "bid": 1.9, "ask": 2.0, "mid": 1.95},
        {"strike": 180, "bid": .499, "ask": .5, "mid": .4995},
    ))
    # A bounded deterministic history isolates the retention/scoring seam. The
    # cheaper contract leads liquidity scoring but expires out of the money.
    monkeypatch.setattr("investment_panel.infrastructure.postgres.options_expressions._histories",
                        lambda _connection, cutoffs, _limits: {
                            instrument: [100 * 1.1 ** (index / 28) for index in range(60)]
                            for instrument in cutoffs
                        })
    initial_contracts = []

    def score(runtime, run_id, calibrated_ready):
        with runtime.read() as connection:
            rows = connection.execute(
                "SELECT contract.strike FROM analysis.decision decision "
                "JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id "
                "JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id "
                "WHERE decision.run_id = %s ORDER BY decision.score DESC, decision.decision_key", [run_id],
            ).fetchall()
            initial_contracts.append([float(row["strike"]) for row in rows])
        return enrich_long_option_expectancy(runtime, run_id, calibrated_ready)

    monkeypatch.setattr("investment_panel.infrastructure.postgres.options_analysis.enrich_long_option_expectancy", score)
    before = refresh_options_radar(runtime, source_id="test-experiment", code_version="selection-before", candidate_revision_id=candidate)
    assert before["empirical_long_options"] == 2 and before["shadow_trades"] == 1
    with runtime.transaction() as connection:
        connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent])
        connection.execute("UPDATE analysis.strategy_revision SET status = 'active', promoted_at = now() WHERE id = %s", [candidate])
    after = refresh_options_radar(runtime, source_id="test-experiment", code_version="selection-after")
    assert after["empirical_long_options"] == 2 and initial_contracts == [[180, 160], [180, 160]]
    with runtime.read() as connection:
        selected = []
        for run_id in (before["analysis_run_id"], after["analysis_run_id"]):
            rows = connection.execute(
                "SELECT contract.strike, decision.score FROM analysis.decision decision "
                "JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id "
                "JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id "
                "WHERE decision.run_id = %s AND option_decision.structure = 'long_call' "
                "ORDER BY (decision.state = 'REJECTED'), decision.score DESC, decision.decision_key", [run_id],
            ).fetchall()
            assert len(rows) == 2 and rows[0]["score"] > rows[1]["score"]
            selected.append(float(rows[0]["strike"]))
        assert selected == [160, 160]
        observed = connection.execute(
            "SELECT contract.strike FROM analysis.shadow_trade shadow "
            "JOIN analysis.option_decision option_decision ON option_decision.decision_id = shadow.decision_id "
            "JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id "
            "WHERE shadow.source_kind = 'options_paper_experiment'",
        ).fetchall()
        assert [float(row["strike"]) for row in observed] == [160]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"score_weights": {"liquidity": .1}}, "unsupported_parameters"),
    ({"min_open_interest": 1}, "requires_rejected_or_shadow_outcomes"),
])
def test_candidate_observation_rejects_unsupported_and_relaxed_changes(experiment_context, changes, reason):
    runtime, _ingestion, _now, parent, _candidate = experiment_context
    with runtime.transaction() as connection:
        candidate = connection.execute(
            "INSERT INTO analysis.strategy_revision (strategy_key, revision, name, status, parameters, authority_group, supersedes_id) "
            "VALUES ('unsupported-experiment', 1, 'Unsupported candidate', 'candidate', %s, 'options-radar-core', %s) RETURNING id",
            [Jsonb(merge_strategy_parameters(DEFAULT_PARAMETERS, changes)), parent],
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO analysis.agent_task (task_kind, status, request, result) "
            "VALUES ('strategy_mutation_proposal', 'completed', '{}', %s)",
            [Jsonb({"candidate_revision_id": candidate, "proposed_parameter_changes": changes})],
        )
    with runtime.read() as connection:
        with pytest.raises(ValueError, match=reason):
            experiment_candidate(connection, candidate, as_of=datetime.now(UTC))


def test_scheduled_candidate_staging_uses_a_clock_after_its_publication(experiment_context, monkeypatch):
    runtime, _ingestion, _now, _parent, candidate = experiment_context
    clocks = {}

    def publish(*_args, **kwargs):
        assert kwargs["candidate_revision_id"] == candidate
        clocks["published_at"] = datetime.now(UTC)
        return {"publication_id": "isolated-experiment-publication"}

    def stage(_self, **kwargs):
        clocks["staged_at"] = kwargs["now"]
        assert kwargs["experiment_publication_id"] == "isolated-experiment-publication"
        return []

    monkeypatch.setattr("investment_panel.jobs.options_paper_execution.refresh_options_radar", publish)
    monkeypatch.setattr(OptionsPaperExecutionRepository, "stage_current_ready", stage)
    settings = SimpleNamespace(strategy_auto_promotion_enabled=True, options_paper_actions_enabled=True, radar_paper_actions_enabled=True,
                               options_risk_sleeve_capital=25000, daily_loss_halt_pct=.02, max_recovery_open_positions=2)
    config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings))
    assert run_experiments(runtime, config)["status"] == "ok"
    assert clocks["published_at"] <= clocks["staged_at"]


@pytest.mark.parametrize("regime,clock_offset", [("trend_up", 0), ("trend_down", 0), ("trend_up", 60)])
def test_candidate_records_only_the_regime_available_at_its_decision(experiment_context, monkeypatch, regime, clock_offset):
    runtime, ingestion, now, _parent, candidate = experiment_context
    _capture(runtime, ingestion, now)
    monkeypatch.setattr("investment_panel.infrastructure.postgres.options_analysis.refresh_symbol_trend_features",
                        lambda *_args, **_kwargs: {"market_regime": {"state": regime, "quality_status": "complete",
                            "as_of": (now + timedelta(seconds=clock_offset)).isoformat()}})
    published = refresh_options_radar(runtime, source_id="test-experiment", code_version="regime-test", candidate_revision_id=candidate)
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT option_decision.market_regime, option_decision.market_regime_detail "
            "FROM analysis.option_decision option_decision JOIN analysis.decision decision ON decision.id = option_decision.decision_id "
            "WHERE decision.run_id = %s", [published["analysis_run_id"]],
        ).fetchall()
        assert rows and all(row["market_regime"] == (regime if clock_offset == 0 else None) for row in rows)
        assert all(row["market_regime_detail"]["quality_status"] == ("complete" if clock_offset == 0 else "unavailable") for row in rows)


def test_qualified_candidate_after_ten_blocked_candidates_is_not_starved(experiment_context, monkeypatch):
    runtime, _ingestion, now, parent, candidate = experiment_context
    previous = AnalysisRepository(runtime).start_run("options-paper-experiment", input_cutoff=now,
        code_version="prior-cycle", inputs={}, strategy_revision_id=candidate)
    AnalysisRepository(runtime).finish_run(previous, "failed", {})
    with runtime.transaction() as connection:
        for index in range(10):
            connection.execute(
                "INSERT INTO analysis.strategy_revision (strategy_key, revision, name, status, parameters, authority_group, supersedes_id) "
                "VALUES (%s, 1, 'Unproposed', 'candidate', %s, 'options-radar-core', %s)",
                [f"unproposed-{index}", Jsonb(DEFAULT_PARAMETERS), parent],
            )
    monkeypatch.setattr("investment_panel.jobs.options_paper_execution.refresh_options_radar",
                        lambda *_args, **kwargs: {"candidate_revision_id": kwargs["candidate_revision_id"]})
    settings = SimpleNamespace(strategy_auto_promotion_enabled=True, options_paper_actions_enabled=False,
                               options_risk_sleeve_capital=25000)
    result = run_experiments(runtime, SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings)))
    assert result["status"] == "ok" and result["candidate_revision_id"] == candidate
    assert len(result["blocked"]) == 10


def test_shadow_drawdown_measures_a_decline_from_peak_wealth(experiment_context):
    runtime, ingestion, now, _parent, candidate = experiment_context
    _capture(runtime, ingestion, now)
    refresh_options_radar(runtime, source_id="test-experiment", code_version="drawdown-test", candidate_revision_id=candidate)
    _capture(runtime, ingestion, now + timedelta(seconds=20))
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=21))["entered"] == 1
    _capture(runtime, ingestion, now + timedelta(seconds=140), bid=.913, ask=.933)
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=141))["closed"] == 0
    _capture(runtime, ingestion, now + timedelta(seconds=160), bid=.613, ask=.633)
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=161))["closed"] == 0
    with runtime.read() as connection:
        metrics = connection.execute("SELECT metrics FROM analysis.shadow_trade").fetchone()["metrics"]
        assert metrics["initial_wealth"] == 1 and metrics["peak_return"] == pytest.approx(.8)
        assert metrics["current_return"] == pytest.approx(.2) and metrics["max_drawdown"] == pytest.approx(-1 / 3)
        assert metrics["drawdown_peak_quotes"][0]["quote_id"] != metrics["drawdown_trough_quotes"][0]["quote_id"]
    _capture(runtime, ingestion, now + timedelta(seconds=180), bid=.2, ask=.22)
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=181))["closed"] == 1
    with runtime.read() as connection:
        outcome = connection.execute("SELECT max_drawdown FROM analysis.option_outcome").fetchone()
        assert outcome["max_drawdown"] == pytest.approx(.374 / 1.8 - 1)


def _ready_paper_publication(experiment_context, monkeypatch, *, experimental=True, sleeve_capital=25000, analysis_runtime=None):
    runtime, ingestion, now, parent, candidate = experiment_context
    monkeypatch.setattr("investment_panel.infrastructure.postgres.actions.is_market_open", lambda _: True)
    monkeypatch.setattr("investment_panel.infrastructure.postgres.options_paper_execution.is_market_open", lambda _: True)
    _capture(runtime, ingestion, now)
    with runtime.transaction() as connection:
        connection.execute(
            "INSERT INTO analysis.strategy_evaluation (strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics) "
            "VALUES (%s, 'shadow', now(), 'pass', '{}')", [candidate],
        )
        quote = connection.execute(
            "SELECT quote.contract_id, quote.snapshot_id, snapshot.ingest_run_id, contract.underlying_instrument_id AS instrument_id "
            "FROM raw.option_quote quote JOIN catalog.option_contract contract ON contract.id = quote.contract_id "
            "JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id LIMIT 1"
        ).fetchone()
        connection.execute(
            "INSERT INTO raw.broker_account_snapshot (source_id, ingest_run_id, account_key, observed_at, net_liquidation, buying_power, cash_balance) "
            "VALUES ('test-experiment', %s, 'paper-test', %s, 100000, 100000, 100000)",
            [quote["ingest_run_id"], now],
        )
    with runtime.read() as connection:
        context = experiment_candidate(connection, candidate, as_of=datetime.now(UTC), require_shadow=True) if experimental else None
    analysis = AnalysisRepository(analysis_runtime or runtime)
    run_id = analysis.start_run("options-paper-experiment" if experimental else "options-radar", input_cutoff=now, code_version="ready-fixture",
                                inputs={"experiment": experiment_identity(context)} if experimental else {}, strategy_revision_id=candidate if experimental else parent)
    decision_id = analysis.store_option_decision(
        run_id, decision_key="paper-fixture", instrument_id=quote["instrument_id"], contract_id=quote["contract_id"],
        snapshot_id=quote["snapshot_id"], quote_observed_at=now, state="READY", score=90, rank=1,
        inputs={}, strategy_revision_id=candidate if experimental else parent, lane="radar",
        details={"structure": "long_call", "entry_price": .5, "max_loss": 50, "quality_status": "complete"},
    )
    ticket = build_option_trade_ticket(
        decision_id=str(decision_id), symbol="NVDA", structure="long_call", expiration=now.date() + timedelta(days=40),
        legs=[{"contract_id": quote["contract_id"], "option_type": "call", "side": "buy", "strike": 160,
               "bid": .48, "ask": .5, "bid_size": 10, "ask_size": 10, "open_interest": 1000, "quote_time": now}],
        entry_price=.5, one_unit_max_loss=50, state="READY", evaluated_at=now, market_session="regular",
        sleeve_capital=sleeve_capital, broker_available_capital=100000,
        thesis={"direction": "long", "invalidation": "Exit below the stated thesis level"},
        forecast={"probability_semantics": "calibrated_exact_cohort", "probability_profit": .7,
                  "effective_sample_size": 42, "lower_95_expected_value": 10},
        provenance={"quote_source": "test-experiment"}, lane="radar",
    )
    assert ticket["state"] == "READY"
    if experimental:
        ticket["experiment"] = experiment_identity(context, str(run_id))
    payload = {"decision_id": str(decision_id), "ticket": ticket, **({"experiment": ticket["experiment"]} if experimental else {}),
               "data_source": "test-experiment", "structure": "long_call", "captured_at": now,
               "last_trade_at": now, "bid_size": 10, "ask_size": 10, "execution_ready": True}
    publication_id = analysis.publish(run_id, f"options-paper-experiment:{candidate}" if experimental else "options-radar",
                                      {"option_paper_experiment" if experimental else "option_radar_opportunity": [payload]}, complete_run_summary={})
    return SimpleNamespace(analysis=analysis, ticket=ticket, payload=payload, run_id=run_id,
                           publication_id=publication_id, decision_id=decision_id, quote=quote)


@pytest.mark.parametrize("partial_thesis", [False, True])
def test_application_login_stages_and_manages_candidate_paper(experiment_context, application_postgres_dsn, monkeypatch, partial_thesis):
    owner, ingestion, now, _parent, _candidate = experiment_context
    application = DatabaseRuntime(application_postgres_dsn)
    application.open()
    try:
        capital, quantity = (50000, 2) if partial_thesis else (25000, 1)
        ready = _ready_paper_publication(experiment_context, monkeypatch, sleeve_capital=capital, analysis_runtime=application)
        staged = ActionRepository(application).stage_option_paper_entry(
            decision_id=ready.decision_id, idempotency_key="application-paper", ticket_version=1,
            quantity=quantity, limit_price=.5, current_options_risk_sleeve_capital=capital,
            experiment_publication_id=ready.publication_id,
        )
        execution = OptionsPaperExecutionRepository(application)
        _capture(owner, ingestion, now + timedelta(seconds=10), ask_size=1)
        filled = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))
        assert filled["status"] == "filled" and filled["filled_quantity"] == 1
        if partial_thesis:
            with owner.transaction() as connection:
                connection.execute("INSERT INTO app.thesis (instrument_id, revision, status, thesis, updated_at) VALUES (%s, 1, 'current', %s, %s)",
                                   [ready.quote["instrument_id"], Jsonb({"lifecycle_status": "invalidated"}), now + timedelta(seconds=20)])
        _capture(owner, ingestion, now + timedelta(seconds=20), bid=1.1, ask=1.12)
        exited = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=21))
        assert exited["status"] == "closed" and exited["exit_quantity"] == 1
        assert exited["reason"] == ("thesis_invalidated_or_closed" if partial_thesis else "profit_target")
        with application.read() as connection:
            paper = connection.execute("SELECT quantity, filled_quantity, exited_quantity, fees, ticket_snapshot FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
            assert paper["quantity"] == quantity and paper["filled_quantity"] == paper["exited_quantity"] == 1
            assert float(paper["fees"]) == pytest.approx(1.3)
            assert paper["ticket_snapshot"]["experiment"] == ready.ticket["experiment"]
            journal = connection.execute("SELECT action, quantity FROM app.trade_journal WHERE decision_id = %s ORDER BY created_at", [ready.decision_id]).fetchall()
            assert [(row["action"], row["quantity"]) for row in journal] == [("paper_entry", 1), (f"paper_exit:{exited['reason']}", 1)]
    finally:
        application.close()


@pytest.mark.parametrize("future_entry_journal", [False, True])
def test_application_login_exits_use_all_partial_entry_prices_and_paid_fees(experiment_context, application_postgres_dsn, monkeypatch, future_entry_journal):
    owner, ingestion, now, _parent, _candidate = experiment_context
    application = DatabaseRuntime(application_postgres_dsn)
    application.open()
    try:
        ready = _ready_paper_publication(experiment_context, monkeypatch, sleeve_capital=50000)
        staged = ActionRepository(application).stage_option_paper_entry(
            decision_id=ready.decision_id, idempotency_key="different-entry-prices", ticket_version=1,
            quantity=2, limit_price=.5, current_options_risk_sleeve_capital=50000,
            experiment_publication_id=ready.publication_id,
        )
        execution = OptionsPaperExecutionRepository(application)
        for seconds, bid, ask in [(10, .48, .5), (20, .38, .4)]:
            _capture(owner, ingestion, now + timedelta(seconds=seconds), bid=bid, ask=ask, ask_size=1)
            filled = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=seconds + 1))
            assert filled["status"] == "filled" and filled["fill_price"] == ask
        if future_entry_journal:
            with owner.transaction() as connection:
                connection.execute(
                    "UPDATE app.trade_journal SET created_at = %s WHERE details->>'paper_order_id' = %s AND action = 'paper_entry' AND price = .4",
                    [now + timedelta(days=1), staged["paper_order_id"]],
                )
        for seconds, bid, ask, expected_status, expected_pnl in [(30, .2, .22, "filled", -26.3), (40, .1, .11, "closed", -36.3)]:
            _capture(owner, ingestion, now + timedelta(seconds=seconds), bid=bid, ask=ask, bid_size=1)
            exited = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=seconds + 1))
            assert exited["status"] == expected_status and exited["exit_quantity"] == 1
            assert exited["net_pnl"] is None if future_entry_journal else exited["net_pnl"] == pytest.approx(expected_pnl)
            if future_entry_journal:
                # An unknown partial exit blocks more risk while the remaining
                # holding still reaches the next execution loop and exits.
                with application.read() as connection:
                    assert "shared_exit_accounting_unreconciled" in shared_sleeve_blockers(
                        connection, now=now + timedelta(seconds=seconds + 1), lane="radar", sleeve_capital=50000,
                        daily_loss_halt_pct=.02, max_open_positions=None,
                    )
                with pytest.raises(ValueError, match="shared_exit_accounting_unreconciled"):
                    ActionRepository(application).stage_option_paper_entry(
                        decision_id=ready.decision_id, idempotency_key=f"blocked-after-unknown-{seconds}", ticket_version=1,
                        quantity=1, limit_price=.5, current_options_risk_sleeve_capital=50000,
                        experiment_publication_id=ready.publication_id,
                    )
        with application.read() as connection:
            paper = connection.execute("SELECT * FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
            assert paper["actual_fill_price"] == .5  # This snapshot is the first fill, not the cash basis.
            assert paper["filled_quantity"] == paper["exited_quantity"] == 2
            assert float(paper["entry_fees"]) == float(paper["exit_fees"]) == pytest.approx(1.3)
            journal = connection.execute(
                "SELECT action, quantity, price, details FROM app.trade_journal WHERE details->>'paper_order_id' = %s ORDER BY created_at, id",
                [staged["paper_order_id"]],
            ).fetchall()
            assert [float(row["price"]) for row in journal if row["action"] == "paper_entry"] == [.5, .4]
            assert [float(row["price"]) for row in journal if row["action"].startswith("paper_exit:")] == [.2, .1]
            assert all(row["quantity"] == 1 for row in journal)
            exits = [row["details"] for row in journal if row["action"].startswith("paper_exit:")]
            if future_entry_journal:
                assert all(row["net_pnl"] is None and row["allocated_entry_fees"] is None for row in exits)
                assert all(row["net_pnl_basis"] == "paper_fill_or_fee_journal_incomplete" for row in exits)
            else:
                assert sum(row["net_pnl"] for row in exits) == pytest.approx(-62.6)
                assert all(row["allocated_entry_fees"] == .65 and len(row["entry_journal_ids"]) == 2 for row in exits)
                assert all(row["net_pnl_basis"] == "journal_entry_vwap_and_paid_fees" for row in exits)
                assert "shared_daily_loss_halt" in shared_sleeve_blockers(
                    connection, now=now + timedelta(seconds=41), lane="radar", sleeve_capital=3000,
                    daily_loss_halt_pct=.02, max_open_positions=None,
                )
        if future_entry_journal:
            from investment_panel.infrastructure.postgres.options_paper_ledger import shared_sleeve_loss_state

            # The missing fee/fill journal becomes available after both exits.
            # Reconcile from that evidence without rewriting the unknown exits.
            with owner.transaction() as connection:
                connection.execute(
                    "UPDATE app.trade_journal SET created_at = %s WHERE details->>'paper_order_id' = %s AND action = 'paper_entry' AND price = .4",
                    [now + timedelta(seconds=42), staged["paper_order_id"]],
                )
            with application.read() as connection:
                state = shared_sleeve_loss_state(connection, now=now + timedelta(seconds=43))
                assert state == {"value": pytest.approx(-62.6), "unresolved_exits": 0, "reconciled_exits": 2}
                assert shared_sleeve_blockers(
                    connection, now=now + timedelta(seconds=43), lane="radar", sleeve_capital=50000,
                    daily_loss_halt_pct=.02, max_open_positions=None,
                ) == []
                assert "shared_daily_loss_halt" in shared_sleeve_blockers(
                    connection, now=now + timedelta(seconds=43), lane="radar", sleeve_capital=3000,
                    daily_loss_halt_pct=.02, max_open_positions=None,
                )
                assert all(row["details"]["net_pnl"] is None for row in connection.execute(
                    "SELECT details FROM app.trade_journal WHERE details->>'paper_order_id' = %s AND action LIKE 'paper_exit:%%'",
                    [staged["paper_order_id"]],
                ).fetchall())
    finally:
        application.close()


@pytest.mark.parametrize("exit_multiplier", [100, 10])
def test_real_exit_preserves_multiplier_conflict_after_catalog_changes(experiment_context, application_postgres_dsn, monkeypatch, exit_multiplier):
    owner, ingestion, now, _parent, _candidate = experiment_context
    application = DatabaseRuntime(application_postgres_dsn)
    application.open()
    try:
        ready = _ready_paper_publication(experiment_context, monkeypatch)
        staged = ActionRepository(application).stage_option_paper_entry(
            decision_id=ready.decision_id, idempotency_key="multiplier-exit", ticket_version=1,
            quantity=1, limit_price=.5, current_options_risk_sleeve_capital=25000,
            experiment_publication_id=ready.publication_id,
        )
        execution = OptionsPaperExecutionRepository(application)
        _capture(owner, ingestion, now + timedelta(seconds=10))
        assert execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))["status"] == "filled"
        _capture(owner, ingestion, now + timedelta(seconds=20), bid=.2, ask=.22)
        with owner.transaction() as connection:
            connection.execute("UPDATE catalog.option_contract SET multiplier = %s WHERE id = %s", [exit_multiplier, ready.quote["contract_id"]])
        result = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=21))
        assert result["status"] == "closed"
        assert result["net_pnl"] is None if exit_multiplier != 100 else result["net_pnl"] == pytest.approx(-31.3)
        with application.read() as connection:
            journal = connection.execute("SELECT details FROM app.trade_journal WHERE details->>'paper_order_id' = %s AND action LIKE 'paper_exit:%%'",
                                         [staged["paper_order_id"]]).fetchone()["details"]
            assert journal["entry_contract_multiplier"] == 100 and journal["exit_contract_multiplier"] == exit_multiplier
            assert journal["net_pnl_basis"] == ("paper_contract_multiplier_conflict" if exit_multiplier != 100 else "journal_entry_vwap_and_paid_fees")
            assert ("shared_exit_accounting_unreconciled" in shared_sleeve_blockers(
                connection, now=now + timedelta(seconds=21), lane="radar", sleeve_capital=25000,
                daily_loss_halt_pct=.02, max_open_positions=None,
            )) is (exit_multiplier != 100)
        # A later correct catalog/quote cannot change the multiplier observed
        # at the completed exit or turn its unknown loss into a numeric return.
        with owner.transaction() as connection:
            connection.execute("UPDATE catalog.option_contract SET multiplier = 100 WHERE id = %s", [ready.quote["contract_id"]])
        _capture(owner, ingestion, now + timedelta(seconds=30))
        with application.read() as connection:
            assert ("shared_exit_accounting_unreconciled" in shared_sleeve_blockers(
                connection, now=now + timedelta(days=1), lane="radar", sleeve_capital=25000,
                daily_loss_halt_pct=.02, max_open_positions=None,
            )) is (exit_multiplier != 100)
        if exit_multiplier != 100:
            with pytest.raises(ValueError, match="shared_exit_accounting_unreconciled"):
                ActionRepository(application).stage_option_paper_entry(
                    decision_id=ready.decision_id, idempotency_key="blocked-after-multiplier-conflict", ticket_version=1,
                    quantity=1, limit_price=.5, current_options_risk_sleeve_capital=25000,
                    experiment_publication_id=ready.publication_id,
                )
    finally:
        application.close()


@pytest.mark.parametrize("gap", ["different_next_multiplier", "missing_entry_multiplier", "conflicting_entry_multiplier"])
def test_partial_fill_never_replaces_its_original_multiplier_basis(experiment_context, application_postgres_dsn, monkeypatch, gap):
    owner, ingestion, now, _parent, _candidate = experiment_context
    application = DatabaseRuntime(application_postgres_dsn)
    application.open()
    try:
        ready = _ready_paper_publication(experiment_context, monkeypatch, sleeve_capital=50000)
        staged = ActionRepository(application).stage_option_paper_entry(
            decision_id=ready.decision_id, idempotency_key="partial-multiplier", ticket_version=1,
            quantity=2, limit_price=.5, current_options_risk_sleeve_capital=50000,
            experiment_publication_id=ready.publication_id,
        )
        execution = OptionsPaperExecutionRepository(application)
        _capture(owner, ingestion, now + timedelta(seconds=10), ask_size=1)
        assert execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))["filled_quantity"] == 1
        _capture(owner, ingestion, now + timedelta(seconds=20), bid=.38, ask=.4, ask_size=1)
        with owner.transaction() as connection:
            if gap == "different_next_multiplier":
                connection.execute("UPDATE catalog.option_contract SET multiplier = 10 WHERE id = %s", [ready.quote["contract_id"]])
            elif gap == "missing_entry_multiplier":
                connection.execute("UPDATE app.trade_journal SET details = details - 'contract_multiplier' WHERE details->>'paper_order_id' = %s", [staged["paper_order_id"]])
            else:
                connection.execute("UPDATE app.trade_journal SET details = details || %s WHERE details->>'paper_order_id' = %s",
                                   [Jsonb({"contract_multiplier": 10}), staged["paper_order_id"]])
        result = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=21))
        assert result["reason"] == "exit_not_triggered"
        with application.read() as connection:
            paper = connection.execute("SELECT * FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
            assert paper["status"] == "entered" and paper["filled_quantity"] == 1 and paper["contract_multiplier"] == 100
            assert paper["execution_quote"]["entry_remainder_cancellation_v1"]["cancelled_quantity"] == 1
            assert paper["execution_quote"][PAPER_MARK_KEY]["status"] == "unknown"
            assert connection.execute("SELECT count(*) AS value FROM app.trade_journal WHERE details->>'paper_order_id' = %s AND action = 'paper_entry'",
                                      [staged["paper_order_id"]]).fetchone()["value"] == 1
        with owner.transaction() as connection:
            connection.execute("UPDATE catalog.option_contract SET multiplier = 100 WHERE id = %s", [ready.quote["contract_id"]])
        _capture(owner, ingestion, now + timedelta(seconds=30), bid=.2, ask=.22)
        exited = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=31))
        assert exited["status"] == "closed" and exited["exit_quantity"] == 1
        assert exited["net_pnl"] == pytest.approx(-31.3) if gap == "different_next_multiplier" else exited["net_pnl"] is None
        with application.read() as connection:
            assert ("shared_exit_accounting_unreconciled" in shared_sleeve_blockers(
                connection, now=now + timedelta(seconds=31), lane="radar", sleeve_capital=50000,
                daily_loss_halt_pct=.02, max_open_positions=None,
            )) is (gap != "different_next_multiplier")
    finally:
        application.close()


def _expiration_close_fixture(runtime, ingestion, expiration, *, price, observed_at, available_at, confirmed_at, actual_clocks=False):
    source = "test-expiration-close"
    ingestion.register_source(source, name=source, family="test", kind="daily_bars")
    run_id = ingestion.start_run(source, "price_bars", **({} if actual_clocks else {"started_at": confirmed_at - timedelta(seconds=1)}))
    assert ingestion.store_price_bars(run_id, source, [{"symbol": "NVDA", "date": expiration.isoformat(), "close": price, "is_complete": True}]) == 1
    ingestion.finish_run(run_id, "succeeded")
    if actual_clocks:
        with runtime.read() as connection:
            fact = connection.execute("SELECT id, instrument_id FROM raw.price_bar WHERE ingest_run_id = %s", [run_id]).fetchone()
            confirmed = confirmed_daily_bars(
                connection, [fact["instrument_id"]], as_of=datetime.now(UTC), trading_dates=[expiration], require_session_close=True,
            )[fact["instrument_id"]][0]
            observed_at, available_at, confirmed_at = (confirmed[key].astimezone(UTC) for key in ("observed_at", "available_at", "confirmed_at"))
            assert observed_at == market_session_bounds(expiration)[1].astimezone(UTC)
            assert observed_at <= available_at <= confirmed_at
    else:
        # Negative evidence fixtures deliberately exercise malformed clocks.
        with runtime.transaction() as connection:
            connection.execute("UPDATE ingest.run SET finished_at = %s WHERE id = %s", [confirmed_at, run_id])
            fact = connection.execute("SELECT id FROM raw.price_bar WHERE ingest_run_id = %s", [run_id]).fetchone()
            connection.execute("UPDATE raw.price_bar SET observed_at = %s, available_at = %s WHERE id = %s", [observed_at, available_at, fact["id"]])
            connection.execute("UPDATE raw.price_bar_confirmation SET fact_available_at = %s WHERE fact_id = %s AND ingest_run_id = %s", [available_at, fact["id"], run_id])
            connection.execute("UPDATE raw.price_bar_fact_availability SET fact_available_at = %s WHERE fact_id = %s AND ingest_run_id = %s", [available_at, fact["id"], run_id])
    return {"basis": "confirmed_expiration_daily_close", "close": price, "trading_date": expiration.isoformat(),
            "session_close_at": market_session_bounds(expiration)[1].astimezone(UTC).isoformat(),
            "source_id": source, "fact_id": fact["id"], "fact_table": "raw.price_bar", "ingest_run_id": str(run_id),
            "observed_at": observed_at.isoformat(), "available_at": available_at.isoformat(), "confirmed_at": confirmed_at.isoformat()}


@pytest.mark.parametrize("previously_exited,entry_journal_complete,closing_price,mark_case", [
    (0, True, 8, "valid"), (1, True, 8, "valid"), (0, False, 8, "valid"),
    (0, True, 10, "valid"), (0, True, 12, "valid"),
    (0, True, 8, "producer_winter"), (0, True, 8, "producer_early_close"), (0, True, 8, "multiplier_conflict"),
    *[(0, True, 8, case) for case in ("missing", "stale", "premarket", "intraday", "future_available", "future_confirmation", "future_observed", "disabled", "failed", "nonfinite")],
])
def test_application_login_settles_preauthorized_csp_without_changing_policy(experiment_context, application_postgres_dsn, monkeypatch, previously_exited, entry_journal_complete, closing_price, mark_case):
    owner, ingestion, now, parent, _candidate = experiment_context
    application = DatabaseRuntime(application_postgres_dsn)
    application.open()
    try:
        valid_mark = mark_case in {"valid", "producer_winter", "producer_early_close", "multiplier_conflict"}
        entry_multiplier = 10 if mark_case == "multiplier_conflict" else 100
        accounting_complete = entry_journal_complete and entry_multiplier == 100
        # This separate holding must still be managed after the assigned order.
        ready = _ready_paper_publication(experiment_context, monkeypatch, experimental=False)
        other = ActionRepository(application).stage_option_paper_entry(
            decision_id=ready.decision_id, idempotency_key="after-assignment", ticket_version=1,
            quantity=1, limit_price=.5, current_options_risk_sleeve_capital=25000,
        )
        execution = OptionsPaperExecutionRepository(application)
        _capture(owner, ingestion, now + timedelta(seconds=10))
        assert execution._manage_one(other["paper_order_id"], now + timedelta(seconds=11))["status"] == "filled"
        expiration = {"producer_winter": date(2026, 1, 2), "producer_early_close": date(2025, 11, 28)}.get(mark_case, completed_trading_dates(now, count=1)[0])
        session_close = market_session_bounds(expiration)[1].astimezone(UTC)
        entry_at = session_close - timedelta(days=2)
        with owner.transaction() as connection:
            connection.execute(
                "UPDATE analysis.strategy_revision SET created_at = least(created_at, %s), promoted_at = least(promoted_at, %s) WHERE id = %s",
                [entry_at - timedelta(days=1), entry_at - timedelta(days=1), parent],
            )
        historical = _capture(owner, ingestion, entry_at, bid=.5, ask=.52, contracts=(
            {"strike": 10, "option_type": "put", "expiration": expiration.isoformat(), "underlying_price": 12},
        ))
        with owner.read() as connection:
            contract_id = connection.execute(
                "SELECT contract_id FROM raw.option_quote WHERE snapshot_id = %s", [historical["snapshot_id"]],
            ).fetchone()["contract_id"]
        analysis = AnalysisRepository(owner)
        run_id = analysis.start_run("options-radar", input_cutoff=entry_at, code_version="preauthorized-csp-fixture", strategy_revision_id=parent, inputs={})
        decision_id = analysis.store_option_decision(
            run_id, decision_key="preauthorized-csp", instrument_id=ready.quote["instrument_id"], contract_id=contract_id,
            snapshot_id=historical["snapshot_id"], quote_observed_at=entry_at,
            state="READY", score=90, rank=1, strategy_revision_id=parent, lane="radar", inputs={},
            details={"structure": "cash_secured_put", "entry_price": .5, "quality_status": "complete"},
        )
        risk = RiskPolicySnapshot(policy_version="risk-policy.v2:preauthorized-csp-fixture", sleeve_capital=100000, broker_available_capital=100000,
                                  cash_balance=100000, buying_power=100000, account_observed_at=entry_at)
        assignment_policy = PortfolioAssignmentPolicy(
            paper_assignment_allowed=True, risk_policy_version=risk.policy_version, thesis_direction="bullish",
            thesis_as_of=entry_at, thesis_preferred_structures=("cash_secured_put",), account_as_of=entry_at,
            account_source="postgresql", cash_balance=100000, buying_power=100000,
            required_cash=1000, symbol_limit=5000, aggregate_limit=15000, evaluated_at=entry_at,
        )
        ticket = build_option_trade_ticket(
            decision_id=str(decision_id), symbol="NVDA", structure="cash_secured_put", expiration=expiration,
            legs=[{"contract_id": contract_id, "option_type": "put", "side": "sell", "strike": 10,
                   "bid": .5, "ask": .52, "bid_size": 10, "ask_size": 10, "open_interest": 1000, "quote_time": entry_at}],
            entry_price=.5, one_unit_max_loss=None, secured_cash=1000, state="READY", evaluated_at=entry_at,
            market_session="regular", sleeve_capital=100000, risk_policy_snapshot=risk,
            assignment_policy=assignment_policy, lane="radar",
            thesis={"direction": "bullish", "invalidation": "Preauthorized paper assignment fixture"},
            forecast={"probability_semantics": "calibrated_exact_cohort", "probability_profit": .7,
                      "effective_sample_size": 42, "lower_95_expected_value": 10},
        )
        assert ticket["state"] == "READY", ticket["blockers"]
        assert ticket["assignment_policy"]["eligible"] is True
        assert ticket["risk"]["recommended_quantity"] >= 2
        publication_id = analysis.publish(run_id, "options-radar", {"option_radar_opportunity": [{"decision_id": str(decision_id), "ticket": ticket}]}, complete_run_summary={})
        ticket["publication_lineage"] = {"publication_id": str(publication_id), "publication_scope": "options-radar"}
        policy = {"live_order_submission": False, "assignment_policy": ticket["assignment_policy"], "ticket_version": 1}
        prior_quotes = {"mid": .51, "spread": .02, "entry_quote_identity": "preauthorized-paper-fill",
                        PAPER_MARK_KEY: {"status": "unknown", "reason": "credit_or_assignment_return_basis_unavailable"}}
        with owner.transaction() as connection:
            paper_id = connection.execute(
                "INSERT INTO app.paper_order (decision_id, instrument_id, created_at, side, quantity, limit_price, status, "
                "policy_result, policy_snapshot, ticket_version, ticket_snapshot, lane, structure, reserved_collateral, "
                "actual_fill_price, filled_at, filled_quantity, exited_quantity, fees, entry_fees, exit_fees, contract_multiplier, execution_quote) "
                "VALUES (%s, %s, %s, 'sell', 2, .5, %s, %s, %s, 1, %s, 'radar', 'cash_secured_put', 2000, .5, %s, 2, %s, %s, 1.3, %s, %s, %s) RETURNING id",
                [decision_id, ready.quote["instrument_id"], entry_at, "partial_exited" if previously_exited else "entered",
                 Jsonb(policy), Jsonb(policy), Jsonb(ticket), entry_at, previously_exited, 1.3 + .65 * previously_exited,
                 .65 * previously_exited, entry_multiplier, Jsonb(prior_quotes)],
            ).fetchone()["id"]
            leg = ticket["legs"][0]
            connection.execute(
                "INSERT INTO app.paper_order_leg (paper_order_id, leg_index, contract_id, option_type, side, strike, bid, ask, bid_size, ask_size, quote_time, open_interest, volume) "
                "VALUES (%s, 0, %s, 'put', 'sell', 10, .5, .52, 10, 10, %s, 1000, %s)",
                [paper_id, contract_id, leg["quote_time"], leg.get("volume")],
            )
            entry_journal_ids = []
            if entry_journal_complete:
                entry_journal_ids.append(str(connection.execute(
                    "INSERT INTO app.trade_journal (decision_id, instrument_id, created_at, action, quantity, price, rationale, details) "
                    "VALUES (%s, %s, %s, 'paper_entry', 2, .5, 'deterministic_options_paper_execution', %s) RETURNING id",
                    [decision_id, ready.quote["instrument_id"], entry_at,
                     Jsonb({"paper_order_id": str(paper_id), "fees": 1.3, "contract_multiplier": entry_multiplier, "idempotency_key": "preauthorized-entry"})],
                ).fetchone()["id"]))
            if previously_exited:
                connection.execute(
                    "INSERT INTO app.trade_journal (decision_id, instrument_id, created_at, action, quantity, price, rationale, details) "
                    "VALUES (%s, %s, %s, 'paper_exit:profit_target', 1, .25, 'deterministic_options_paper_execution', %s)",
                    [decision_id, ready.quote["instrument_id"], now,
                     Jsonb({"paper_order_id": str(paper_id), "fees": .65, "net_pnl": 23.7,
                            "entry_contract_multiplier": entry_multiplier, "exit_contract_multiplier": 100,
                            "idempotency_key": "preauthorized-partial-exit"})],
                )
        with ingestion.run("test-experiment", "assignment-quote") as run:
            # Even a fresh ITM spot must not become an expiration settlement.
            ingestion.store_quotes(run.id, "test-experiment", [{"symbol": "NVDA", "price": 1, "observed_at": now + timedelta(seconds=20)}])
        mark = None
        if mark_case != "missing":
            if valid_mark:
                assert execution._manage_one(str(paper_id), datetime.now(UTC))["reason"] == "expiration_settlement_pending"
            bar_date = completed_trading_dates(session_close - timedelta(seconds=1), count=1)[0] if mark_case == "stale" else expiration
            observed_at = now + timedelta(seconds=60) if mark_case == "future_observed" else market_session_bounds(bar_date)[1].astimezone(UTC)
            available_at = now + timedelta(seconds=18)
            confirmed_at = now + timedelta(seconds=19)
            if mark_case in {"premarket", "intraday"}:
                observed_at = session_close - timedelta(hours=10 if mark_case == "premarket" else 1)
                available_at = observed_at + timedelta(seconds=1)
                confirmed_at = available_at + timedelta(seconds=1)
            elif mark_case == "future_available":
                available_at = now + timedelta(seconds=29)
                confirmed_at = now + timedelta(seconds=30)
            elif mark_case == "future_confirmation":
                confirmed_at = now + timedelta(seconds=30)
            mark = _expiration_close_fixture(
                application if valid_mark else owner, IngestionRepository(application) if valid_mark else ingestion,
                bar_date, price=closing_price, observed_at=observed_at,
                available_at=available_at, confirmed_at=confirmed_at, actual_clocks=valid_mark,
            )
            with owner.transaction() as connection:
                if mark_case == "disabled":
                    connection.execute("UPDATE ingest.source SET enabled = false WHERE id = %s", [mark["source_id"]])
                elif mark_case == "failed":
                    connection.execute("UPDATE ingest.run SET status = 'failed' WHERE id = %s", [mark["ingest_run_id"]])
                elif mark_case == "nonfinite":
                    connection.execute("UPDATE raw.price_bar SET close = 'NaN'::float8 WHERE id = %s", [mark["fact_id"]])
        _capture(owner, ingestion, now + timedelta(seconds=20), bid=.6, ask=.62)
        settlement_at = now + timedelta(seconds=21)
        managed = execution.manage_orders(lanes=["radar"], decision_inbox_enabled=False, now=settlement_at, limit=10)
        reason = "assignment" if closing_price < 10 else "expiration_unassigned"
        assert [(row["paper_order_id"], row["reason"]) for row in managed] == [
            (str(paper_id), reason if valid_mark else "expiration_settlement_pending"),
            (other["paper_order_id"], "exit_not_triggered"),
        ]
        if not valid_mark:
            with application.read() as connection:
                pending = connection.execute("SELECT * FROM app.paper_order WHERE id = %s", [paper_id]).fetchone()
                assert pending["status"] == "entered" and pending["exited_quantity"] == 0 and pending["exit_price"] is None
                assert pending["policy_result"] == pending["policy_snapshot"] == policy and pending["ticket_snapshot"] == ticket
                assert pending["execution_quote"]["assignment"]["status"] == "pending_settlement"
                assert {key: pending["execution_quote"][key] for key in prior_quotes} == prior_quotes
                assert float(pending["fees"]) == pytest.approx(1.3)
                assert connection.execute("SELECT count(*) AS count FROM app.trade_journal WHERE details->>'paper_order_id' = %s AND action LIKE 'paper_exit:%%'", [str(paper_id)]).fetchone()["count"] == 0
            if mark_case == "future_confirmation":
                later = execution._manage_one(str(paper_id), now + timedelta(seconds=31))
                assert later["reason"] == "assignment" and later["status"] == "closed"
            return
        intrinsic = max(10 - closing_price, 0)
        with application.read() as connection:
            paper = connection.execute("SELECT * FROM app.paper_order WHERE id = %s", [paper_id]).fetchone()
            assert paper["policy_result"] == paper["policy_snapshot"] == policy and paper["ticket_snapshot"] == ticket
            assert paper["status"] == "exited" and paper["filled_quantity"] == paper["exited_quantity"] == 2
            assert paper["exit_price"] == intrinsic and paper["contract_multiplier"] == 100 and float(paper["fees"]) == pytest.approx(2.6)
            assert {key: paper["execution_quote"][key] for key in prior_quotes} == prior_quotes
            assert paper["execution_quote"]["assignment"] == {
                "status": "assigned" if intrinsic else "expired_unassigned", "strike": 10, "underlying_price": closing_price, "multiplier": 100,
                "contract_count": 2 - previously_exited, "settlement_value": intrinsic * 100 * (2 - previously_exited),
                "settled_at": settlement_at.isoformat(), "assignment_fee": .65 * (2 - previously_exited) if intrinsic else 0,
                "settlement_fee": .65 * (2 - previously_exited), "expiration_mark": mark,
                "allocated_entry_fees": .65 * (2 - previously_exited) if accounting_complete else None,
                "net_pnl": round(((.5 - intrinsic) * 100 - 1.3) * (2 - previously_exited), 2) if accounting_complete else None,
                "entry_journal_ids": entry_journal_ids if entry_multiplier == 100 else [],
                "net_pnl_basis": "paper_contract_multiplier_conflict" if entry_multiplier != 100 else "journal_entry_vwap_and_paid_fees" if entry_journal_complete else "paper_fill_or_fee_journal_incomplete",
                "entry_contract_multiplier": entry_multiplier, "exit_contract_multiplier": 100,
            }
            journal = connection.execute(
                "SELECT action, quantity, price, details FROM app.trade_journal WHERE details->>'paper_order_id' = %s "
                "AND action LIKE 'paper_exit:%%' ORDER BY created_at, id", [str(paper_id)],
            ).fetchall()
            assert sum(row["quantity"] for row in journal) == 2
            assert sum(row["details"]["fees"] for row in journal) == pytest.approx(1.3)
            assert journal[-1]["action"] == f"paper_exit:{reason}" and journal[-1]["price"] == intrinsic
            assert journal[-1]["quantity"] == 2 - previously_exited
            if accounting_complete:
                assert sum(row["details"]["net_pnl"] for row in journal) == pytest.approx(-127.6 if previously_exited else ((.5 - intrinsic) * 100 - 1.3) * 2)
                if intrinsic:
                    assert "shared_daily_loss_halt" in shared_sleeve_blockers(
                        connection, now=settlement_at, lane="radar", sleeve_capital=5000,
                        daily_loss_halt_pct=.02, max_open_positions=None,
                    )
            else:
                assert journal[-1]["details"]["net_pnl"] is None
                assert "shared_exit_accounting_unreconciled" in shared_sleeve_blockers(
                    connection, now=settlement_at, lane="radar", sleeve_capital=5000,
                    daily_loss_halt_pct=.02, max_open_positions=None,
                )
            assert not connection.execute("SELECT has_column_privilege(current_user, 'app.paper_order', 'policy_result', 'UPDATE') AS allowed").fetchone()["allowed"]
        again = execution.manage_orders(lanes=["radar"], decision_inbox_enabled=False, now=now + timedelta(seconds=22), limit=10)
        assert len(again) == 1 and again[0]["paper_order_id"] == other["paper_order_id"]
        with application.read() as connection:
            assert connection.execute(
                "SELECT count(*) AS count FROM app.trade_journal WHERE details->>'paper_order_id' = %s AND action IN ('paper_exit:assignment', 'paper_exit:expiration_unassigned')",
                [str(paper_id)],
            ).fetchone()["count"] == 1
            assert float(connection.execute("SELECT fees FROM app.paper_order WHERE id = %s", [paper_id]).fetchone()["fees"]) == pytest.approx(2.6)
            if entry_multiplier != 100:
                assert "shared_exit_accounting_unreconciled" in shared_sleeve_blockers(
                    connection, now=now + timedelta(seconds=22), lane="radar", sleeve_capital=5000,
                    daily_loss_halt_pct=.02, max_open_positions=None,
                )
    finally:
        application.close()


@pytest.mark.parametrize("rollback", [False, True])
def test_incumbent_paper_holds_original_publication_until_holding_policy_exits(experiment_context, monkeypatch, rollback):
    runtime, ingestion, now, parent, candidate = experiment_context
    ready = _ready_paper_publication(experiment_context, monkeypatch, experimental=False)
    staged = ActionRepository(runtime).stage_option_paper_entry(
        decision_id=ready.decision_id, idempotency_key="incumbent-holding", ticket_version=1,
        quantity=1, limit_price=.5, current_options_risk_sleeve_capital=25000,
    )
    execution = OptionsPaperExecutionRepository(runtime)
    _capture(runtime, ingestion, now + timedelta(seconds=10))
    assert execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))["status"] == "filled"
    _capture(runtime, ingestion, now + timedelta(seconds=20), bid=.6, ask=.62)
    refreshed = refresh_options_radar(runtime, source_id="test-experiment", code_version="routine-refresh")
    with runtime.transaction() as connection:
        order = dict(connection.execute("SELECT * FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone())
        ticket = dict(order["ticket_snapshot"])
        assert execution._current_ticket(connection, order, ticket, as_of=now + timedelta(seconds=21))[0] is None
        assert execution._current_ticket(connection, order, ticket, as_of=now + timedelta(seconds=21), for_entry=False)[0]
        wrong = {**ticket, "publication_lineage": {**ticket["publication_lineage"], "publication_id": refreshed["publication_id"]}}
        assert execution._current_ticket(connection, order, wrong, as_of=now + timedelta(seconds=21), for_entry=False)[0] is None
        if rollback:
            connection.execute("UPDATE analysis.strategy_revision SET status = 'rolled_back' WHERE id = %s", [parent])
            connection.execute("UPDATE analysis.strategy_revision SET status = 'active', promoted_at = %s WHERE id = %s", [now + timedelta(seconds=20), candidate])
    held = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=21))
    if rollback:
        assert held["status"] == "closed" and held["reason"] == "holding_strategy_authority_invalid"
    else:
        assert held["status"] == "filled" and held["reason"] == "exit_not_triggered"
        _capture(runtime, ingestion, now + timedelta(seconds=40), bid=.2, ask=.22)
        exited = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=41))
        assert exited["status"] == "closed" and exited["reason"] == "stop_loss"
    with runtime.read() as connection:
        paper = connection.execute("SELECT status, ticket_snapshot, filled_quantity, exited_quantity FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
        assert paper["status"] == "exited" and paper["filled_quantity"] == paper["exited_quantity"] == 1
        assert paper["ticket_snapshot"] == ticket and paper["ticket_snapshot"]["publication_lineage"]["publication_id"] == str(ready.publication_id)


@pytest.mark.parametrize("rollback", [False, True])
def test_candidate_paper_survives_its_promotion_but_not_rollback(experiment_context, monkeypatch, rollback):
    runtime, ingestion, now, parent, candidate = experiment_context
    ready = _ready_paper_publication(experiment_context, monkeypatch)
    staged = ActionRepository(runtime).stage_option_paper_entry(
        decision_id=ready.decision_id, idempotency_key="promotion-holding", ticket_version=1,
        quantity=1, limit_price=.5, current_options_risk_sleeve_capital=25000, experiment_publication_id=ready.publication_id,
    )
    execution = OptionsPaperExecutionRepository(runtime)
    _capture(runtime, ingestion, now + timedelta(seconds=10))
    assert execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))["status"] == "filled"
    with runtime.transaction() as connection:
        connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent])
        connection.execute("UPDATE analysis.strategy_revision SET status = 'active', promoted_at = %s WHERE id = %s", [now + timedelta(seconds=20), candidate])
    _capture(runtime, ingestion, now + timedelta(seconds=30), bid=.6, ask=.62)
    held = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=31))
    assert held["status"] == "filled" and held["reason"] == "exit_not_triggered"
    with runtime.transaction() as connection:
        with pytest.raises(ValueError, match="core candidate proposal required"):
            experiment_publication_row(connection, str(ready.publication_id), str(ready.decision_id), as_of=now + timedelta(seconds=31))
        if rollback:
            connection.execute("UPDATE analysis.strategy_revision SET status = 'rolled_back' WHERE id = %s", [candidate])
            connection.execute("UPDATE analysis.strategy_revision SET status = 'active' WHERE id = %s", [parent])
    _capture(runtime, ingestion, now + timedelta(seconds=40), bid=.6 if rollback else 1.1, ask=.62 if rollback else 1.12)
    exited = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=41))
    assert exited["status"] == "closed"
    assert exited["reason"] == ("core candidate proposal required" if rollback else "profit_target")
    with runtime.read() as connection:
        paper = connection.execute("SELECT decision_id, ticket_snapshot, filled_quantity, exited_quantity FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
        assert paper["decision_id"] == ready.decision_id and paper["filled_quantity"] == paper["exited_quantity"] == 1
        assert paper["ticket_snapshot"]["experiment"] == ready.ticket["experiment"]


@pytest.mark.parametrize("promotion", ["valid", "future", "rolled_back", "other_successor"])
def test_only_exact_promoted_candidate_keeps_entered_shadow(experiment_context, promotion):
    runtime, ingestion, now, parent, candidate = experiment_context
    _capture(runtime, ingestion, now, symbols=("NVDA", "AMD"))
    refresh_options_radar(runtime, source_id="test-experiment", code_version="promotion-shadow", candidate_revision_id=candidate)
    _capture(runtime, ingestion, now + timedelta(seconds=20))
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=21))["entered"] == 1
    with runtime.transaction() as connection:
        if promotion == "rolled_back":
            connection.execute("UPDATE analysis.strategy_revision SET status = 'rolled_back' WHERE id = %s", [candidate])
        else:
            connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent])
            if promotion == "other_successor":
                connection.execute(
                    "INSERT INTO analysis.strategy_revision (strategy_key, revision, name, status, parameters, authority_group, supersedes_id, promoted_at) "
                    "VALUES ('other-successor', 1, 'Other successor', 'active', %s, 'options-radar-core', %s, %s)",
                    [Jsonb(DEFAULT_PARAMETERS), parent, now + timedelta(seconds=30)],
                )
            else:
                connection.execute("UPDATE analysis.strategy_revision SET status = 'active', promoted_at = %s WHERE id = %s",
                                   [now + timedelta(seconds=300 if promotion == "future" else 30), candidate])
    _capture(runtime, ingestion, now + timedelta(seconds=40), symbols=("NVDA", "AMD"))
    result = advance_experiment_shadows(runtime, now=now + timedelta(seconds=41))
    assert result["unfilled"] == 1 and result["entered"] == result["closed"] == 0
    assert result["unmeasurable"] == (0 if promotion == "valid" else 1)
    if promotion == "valid":
        _capture(runtime, ingestion, now + timedelta(seconds=60), bid=1.1, ask=1.12)
        assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=61))["closed"] == 1
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM analysis.option_outcome").fetchone()["count"] == 1


@pytest.mark.parametrize("exit_quote_available", [True, False])
def test_thesis_invalidation_cancels_partial_remainder_and_exits_actual_holding(experiment_context, monkeypatch, exit_quote_available):
    runtime, ingestion, now, _parent, _candidate = experiment_context
    ready = _ready_paper_publication(experiment_context, monkeypatch, sleeve_capital=50000)
    assert ready.ticket["risk"]["recommended_quantity"] == 2
    staged = ActionRepository(runtime).stage_option_paper_entry(
        decision_id=ready.decision_id, idempotency_key="partial-thesis", ticket_version=1,
        quantity=2, limit_price=.5, current_options_risk_sleeve_capital=50000, experiment_publication_id=ready.publication_id,
    )
    execution = OptionsPaperExecutionRepository(runtime)
    _capture(runtime, ingestion, now + timedelta(seconds=10), ask_size=1)
    filled = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))
    assert filled["status"] == "filled" and filled["filled_quantity"] == 1
    with runtime.transaction() as connection:
        connection.execute("INSERT INTO app.thesis (instrument_id, revision, status, thesis, updated_at) VALUES (%s, 1, 'current', %s, %s)",
                           [ready.quote["instrument_id"], Jsonb({"lifecycle_status": "invalidated"}), now + timedelta(seconds=20)])
    _capture(runtime, ingestion, now + timedelta(seconds=20), bid_size=10 if exit_quote_available else 0)
    result = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=21))
    if not exit_quote_available:
        assert result["status"] == "filled" and "pending_executable_quote" in result["reason"]
        with runtime.read() as connection:
            paper = connection.execute("SELECT status, filled_quantity, exited_quantity FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
            assert paper["status"] == "entered" and paper["filled_quantity"] == 1 and not paper["exited_quantity"]
        _capture(runtime, ingestion, now + timedelta(seconds=30))
        result = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=31))
    assert result["status"] == "closed" and result["reason"] == "thesis_invalidated_or_closed" and result["exit_quantity"] == 1
    with runtime.read() as connection:
        paper = connection.execute("SELECT status, quantity, filled_quantity, exited_quantity, execution_quote FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
        assert paper["status"] == "exited" and paper["quantity"] == 2 and paper["filled_quantity"] == paper["exited_quantity"] == 1
        assert paper["execution_quote"]["entry_remainder_cancellation_v1"]["reason"] == "thesis_invalidated_or_closed"
        journal = connection.execute("SELECT action, quantity FROM app.trade_journal WHERE decision_id = %s ORDER BY created_at", [ready.decision_id]).fetchall()
        assert [(row["action"], row["quantity"]) for row in journal] == [("paper_entry", 1), ("paper_exit:thesis_invalidated_or_closed", 1)]


@pytest.mark.parametrize("observe_gap,exit_bid,entry_window", [
    (False, .6, "current"), (True, .6, "current"), (False, .4, "current"),
    (False, 1.1, "holding"), (False, .6, "expired_unfilled"), (False, 1.1, "terminal_blocked_holding"),
])
def test_candidate_paper_uses_same_risk_checks_and_immutable_experiment_provenance(experiment_context, monkeypatch, observe_gap, exit_bid, entry_window):
    ready = _ready_paper_publication(experiment_context, monkeypatch)
    runtime, ingestion, now, parent, candidate = experiment_context
    ticket, publication_id, decision_id = ready.ticket, ready.publication_id, ready.decision_id
    arguments = dict(decision_id=decision_id, idempotency_key="experiment-risk", ticket_version=1, quantity=1,
                     limit_price=.5, current_options_risk_sleeve_capital=25000, experiment_publication_id=publication_id)
    actions = ActionRepository(runtime)
    with pytest.raises(ValueError, match="no unique current publication"):
        actions.stage_option_paper_entry(**{key: value for key, value in arguments.items() if key != "experiment_publication_id"})
    with pytest.raises(ValueError, match="ticket recommendation"):
        actions.stage_option_paper_entry(**{**arguments, "quantity": 2})
    with pytest.raises(ValueError, match="current options risk sleeve"):
        actions.stage_option_paper_entry(**{**arguments, "current_options_risk_sleeve_capital": 100})
    execution = OptionsPaperExecutionRepository(runtime)
    staged_rows = execution.stage_current_ready(
        enabled_lanes=["radar"], sleeve_capital=25000, daily_loss_halt_pct=.02,
        max_open_positions=2, now=datetime.now(UTC), limit=1, experiment_publication_id=str(publication_id),
    )
    assert len(staged_rows) == 1
    staged = staged_rows[0]
    assert staged["status"] == "staged"
    with runtime.read() as connection:
        order = connection.execute("SELECT policy_snapshot, ticket_snapshot FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()
        assert order["policy_snapshot"]["experiment"]["candidate_revision_id"] == candidate
        assert order["ticket_snapshot"]["publication_lineage"]["publication_id"] == str(publication_id)
        assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [candidate]).fetchone()["status"] == "candidate"
    if entry_window == "expired_unfilled":
        _capture(runtime, ingestion, now + timedelta(seconds=130))
        expired = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=131))
        assert expired["status"] == "rejected" and expired["reason"] == "ticket_expired"
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM app.trade_journal WHERE quantity > 0").fetchone()["count"] == 0
        return
    _capture(runtime, ingestion, now + timedelta(seconds=10))
    assert execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=11))["status"] == "filled"
    with runtime.read() as connection:
        mark = connection.execute("SELECT execution_quote FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()["execution_quote"][PAPER_MARK_KEY]
        assert mark["status"] == "observed" and mark["mark_count"] == 1
        assert mark["current_net_return"] == pytest.approx(-.066)
        assert mark["max_drawdown"] is None
    if entry_window == "terminal_blocked_holding":
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO analysis.strategy_evaluation (strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics) "
                "VALUES (%s, 'shadow', %s, %s, 'blocked_terminal_evidence', '{}')",
                [candidate, now + timedelta(seconds=20), now + timedelta(seconds=20)],
            )
    if observe_gap:
        _capture(runtime, ingestion, now + timedelta(seconds=14), bid=.7, ask=.72)
        assert execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=15))["reason"] == "exit_not_triggered"
        _capture(runtime, ingestion, now + timedelta(seconds=16), bid=.7, ask=.72, bid_size=0)
        execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=17))
        with runtime.read() as connection:
            mark = connection.execute("SELECT execution_quote FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()["execution_quote"][PAPER_MARK_KEY]
            assert mark["status"] == "unknown" and mark["current_net_return"] is None and mark["max_drawdown"] is None
            assert mark["mark_count"] == 2 and mark["observed_max_drawdown"] == pytest.approx(-.066)
    holding = entry_window in {"holding", "terminal_blocked_holding"}
    if holding:
        _capture(runtime, ingestion, now + timedelta(seconds=130))
        held = execution._manage_one(staged["paper_order_id"], now + timedelta(seconds=131))
        assert held["status"] == "filled" and held["reason"] == "exit_not_triggered"
        exit_at = now + timedelta(seconds=150)
    else:
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent])
        exit_at = now + timedelta(seconds=20)
    _capture(runtime, ingestion, exit_at, bid=exit_bid, ask=exit_bid + .02)
    exited = execution._manage_one(staged["paper_order_id"], exit_at + timedelta(seconds=1))
    assert exited["status"] == "closed"
    assert exited["reason"] == "profit_target" if holding else "parent" in exited["reason"]
    with runtime.read() as connection:
        entries = connection.execute("SELECT action, quantity, details FROM app.trade_journal WHERE decision_id = %s AND quantity > 0 ORDER BY created_at", [decision_id]).fetchall()
        assert len(entries) == 2 and all(row["quantity"] == 1 for row in entries)
        assert entries[0]["action"] == "paper_entry" and entries[1]["action"].startswith("paper_exit:")
        assert all(row["details"]["experiment"]["candidate_revision_id"] == candidate for row in entries)
        assert all(row["details"]["quotes"][0]["quote_id"] for row in entries)
        stored_quote = connection.execute("SELECT execution_quote FROM app.paper_order WHERE id = %s", [staged["paper_order_id"]]).fetchone()["execution_quote"]
        mark = stored_quote[PAPER_MARK_KEY]
        assert mark["status"] == "observed" and mark["mark_count"] == (3 if observe_gap or holding else 2)
        assert mark.get("missing_mark_count", 0) == int(observe_gap)
        exit_return = 2 * exit_bid - 1.026
        expected_peak = .374 if observe_gap else max(0, exit_return)
        assert mark["current_net_return"] == pytest.approx(exit_return)
        assert mark["peak_net_return"] == pytest.approx(expected_peak)
        assert mark["max_drawdown"] == pytest.approx(min(-.066, (1 + exit_return) / (1 + expected_peak) - 1))
        if observe_gap:
            assert mark["drawdown_peak_quotes"][0]["quote_id"] != mark["drawdown_trough_quotes"][0]["quote_id"]
        assert mark["actual_fees"] == .65 and mark["modeled_remaining_exit_fees"] == .65
        assert stored_quote["quotes"][0]["quote_id"] == entries[0]["details"]["quotes"][0]["quote_id"]
        if exit_bid < .5:
            assert mark["drawdown_peak_basis"] == "initial_entry_capital" and mark["drawdown_peak_quotes"] == []
            assert mark["drawdown_peak_journal_ids"] and mark["drawdown_peak_at"] == (now + timedelta(seconds=11)).isoformat()


@pytest.mark.parametrize("cohort", [
    "losers", "duplicate_episode", "before_promotion", "invalid_lineage", "paper_winners",
    "paper_incomplete_quarantined", "paper_complete_quarantined", "paper_incomplete_nan_fee", "score_rejected",
    "multiple_paper_orders", "paper_open", "paper_partial_exit", "paper_missing_journal",
    "paper_missing_entry_multiplier", "paper_conflicting_exit_multiplier",
])
def test_rollback_requires_twenty_independent_current_incumbent_losses(experiment_context, monkeypatch, cohort):
    from investment_panel.infrastructure.postgres import strategy_learning
    from investment_panel.infrastructure.postgres.strategy_governance import StrategyGovernanceRepository

    runtime, ingestion, now, parent, active = experiment_context
    with runtime.transaction() as connection:
        connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent])
        connection.execute("UPDATE analysis.strategy_revision SET status = 'active', promoted_at = %s WHERE id = %s",
                           [now - timedelta(seconds=1), active])
    unresolved_paper = cohort in {"paper_open", "paper_partial_exit", "paper_missing_journal",
                                 "paper_missing_entry_multiplier", "paper_conflicting_exit_multiplier"}
    count = 19 if cohort == "duplicate_episode" else 21 if cohort == "multiple_paper_orders" or unresolved_paper else 20
    symbols = tuple(f"OBSA{chr(65 + index)}" for index in range(count))
    _capture(runtime, ingestion, now, symbols=symbols)
    first = refresh_options_radar(runtime, source_id="test-experiment", code_version="rollback-test")
    assert first["shadow_trades"] == len(symbols)
    if cohort == "duplicate_episode":
        second = refresh_options_radar(runtime, source_id="test-experiment", code_version="rollback-duplicate")
        # Two truthful observations of the same economic episode still count once.
        with runtime.transaction() as connection:
            row = connection.execute(
                "SELECT publication.id::text, item.payload FROM app.publication publication "
                "JOIN app.publication_content_item item ON item.publication_id = publication.id "
                "WHERE publication.analysis_run_id = %s AND publication.scope = %s "
                "AND item.model_name = 'option_paper_experiment' AND item.payload->>'ticker' = %s",
                [second["analysis_run_id"], f"options-paper-incumbent:{active}", symbols[0]],
            ).fetchone()
            metrics = connection.execute("SELECT metrics FROM analysis.shadow_trade LIMIT 1").fetchone()["metrics"]
            metrics.update(experiment=row["payload"]["experiment"], ticket=row["payload"]["ticket"], publication_id=row["id"])
            connection.execute(
                "INSERT INTO analysis.shadow_trade (decision_id, status, source_kind, pending_entry_reason, structure, metrics) "
                "VALUES (%s, 'pending', 'options_paper_experiment', 'later_quote_required', 'long_call', %s)",
                [row["payload"]["decision_id"], Jsonb(metrics)],
            )
    _capture(runtime, ingestion, now + timedelta(seconds=20), symbols=symbols)
    observations = 20 if cohort == "duplicate_episode" else count
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=21))["entered"] == observations
    _capture(runtime, ingestion, now + timedelta(seconds=40), bid=.2, ask=.22, symbols=symbols)
    assert advance_experiment_shadows(runtime, now=now + timedelta(seconds=41))["closed"] == observations
    with runtime.transaction() as connection:
        if cohort == "before_promotion":
            connection.execute("UPDATE analysis.strategy_revision SET promoted_at = %s WHERE id = %s", [now + timedelta(seconds=10), active])
        elif cohort == "invalid_lineage":
            connection.execute("UPDATE analysis.shadow_trade SET metrics = jsonb_set(metrics, '{experiment,run_id}', '\"wrong-run\"') WHERE id = (SELECT id FROM analysis.shadow_trade LIMIT 1)")
        elif cohort == "score_rejected":
            connection.execute("UPDATE analysis.decision SET state = 'REJECTED', score = 0 WHERE strategy_revision_id = %s", [active])
        elif cohort in {"paper_winners", "paper_incomplete_quarantined", "paper_complete_quarantined", "paper_incomplete_nan_fee", "multiple_paper_orders"} or unresolved_paper:
            if cohort.endswith("quarantined"):
                connection.execute("UPDATE analysis.option_outcome SET quarantine_reason = 'test_rejected_shadow'")
            elif cohort.endswith("nan_fee"):
                connection.execute("UPDATE analysis.option_outcome SET fee_total = 'NaN'::numeric")
            decisions = connection.execute("SELECT decision_id, decision.instrument_id FROM analysis.shadow_trade shadow JOIN analysis.decision decision ON decision.id = shadow.decision_id ORDER BY decision.id DESC").fetchall()
            if cohort == "multiple_paper_orders":
                # This most recent episode must remain in the trailing 20 as
                # unknown, rather than selecting its winner or an older loss.
                decisions = [decisions[0], decisions[0]]
            elif unresolved_paper:
                # Put unresolved exposure inside the trailing 20 while leaving
                # one extra valid loss that must not backfill the unknown.
                decisions = decisions[:1]
            for index, row in enumerate(decisions):
                exit_price = .2 if cohort == "multiple_paper_orders" and index == 1 else .7
                exit_at = now + timedelta(seconds=44 if cohort == "multiple_paper_orders" else 40)
                status = "entered" if cohort == "paper_open" else "partial_exited" if cohort == "paper_partial_exit" else "exited"
                exited_quantity = 0 if cohort == "paper_open" else .5 if cohort == "paper_partial_exit" else 1
                if cohort == "paper_open":
                    exit_at, exit_price = None, None
                paper = connection.execute(
                    "INSERT INTO app.paper_order (decision_id, instrument_id, side, quantity, limit_price, status, paper_only, "
                    "filled_at, actual_fill_price, exit_at, exit_price, filled_quantity, exited_quantity, fees, entry_slippage, exit_slippage, lane, contract_multiplier) "
                    "VALUES (%s, %s, 'buy', 1, .5, %s, true, %s, .5, %s, %s, 1, %s, 1.3, .01, .01, 'radar', 100) RETURNING id",
                    [row["decision_id"], row["instrument_id"], status, now + timedelta(seconds=22), exit_at, exit_price, exited_quantity],
                ).fetchone()["id"]
                for action, price in (("paper_entry", .5), ("paper_exit:stop_loss" if exit_price is not None and exit_price < .5 else "paper_exit:profit", exit_price)):
                    if action.startswith("paper_exit:") and (cohort.startswith("paper_incomplete_") or cohort in {"paper_open", "paper_missing_journal"}):
                        continue
                    multiplier_evidence = {"contract_multiplier": 100} if action == "paper_entry" else {"entry_contract_multiplier": 100, "exit_contract_multiplier": 100}
                    if cohort == "paper_missing_entry_multiplier" and action == "paper_entry":
                        multiplier_evidence = {}
                    elif cohort == "paper_conflicting_exit_multiplier" and action.startswith("paper_exit:"):
                        multiplier_evidence["exit_contract_multiplier"] = 10
                    connection.execute(
                        "INSERT INTO app.trade_journal (decision_id, instrument_id, action, quantity, price, rationale, details) "
                        "VALUES (%s, %s, %s, %s, %s, 'deterministic_options_paper_execution', %s)",
                        [row["decision_id"], row["instrument_id"], action, 1 if action == "paper_entry" else exited_quantity,
                         price, Jsonb({"paper_order_id": str(paper), "fees": .65, **multiplier_evidence})],
                    )
    # Use the fixture's forward clock without waiting or rewriting recorded timestamps.
    measured_at = now + timedelta(seconds=45)
    for name in ("OUTCOME_QUERY", "OBSERVATION_QUERY"):
        monkeypatch.setattr(strategy_learning, name, getattr(strategy_learning, name).replace("now()", f"TIMESTAMPTZ '{measured_at.isoformat()}'"))

    class ObservationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return measured_at if tz else measured_at.replace(tzinfo=None)

    monkeypatch.setattr(strategy_learning, "datetime", ObservationClock)
    if cohort == "score_rejected":
        with runtime.read() as connection:
            assert connection.execute(strategy_learning.OUTCOME_QUERY, [active, active]).fetchall() == []
    if cohort in {"paper_missing_entry_multiplier", "paper_conflicting_exit_multiplier"}:
        with runtime.read() as connection:
            paper_rows = [dict(row) for row in connection.execute(strategy_learning.OUTCOME_QUERY, [active, active]).fetchall() if row["paper_order_id"]]
        assert len(paper_rows) == 1 and paper_rows[0]["fill_multipliers_verified"] is False
        assert strategy_learning.paper_realized_return(paper_rows[0]) is None
    if cohort.endswith(("quarantined", "nan_fee")):
        with runtime.read() as connection:
            rows = connection.execute(strategy_learning.OUTCOME_QUERY, [active, active]).fetchall()
        assert len(rows) == 20 and all(row["current_return"] is None and row["peak_return"] is None for row in rows)
        measured = strategy_learning.measured_rows([dict(row) for row in rows])
        if cohort.startswith("paper_incomplete_"):
            assert measured == []
        else:
            assert len(measured) == 20
            assert all(row["current_return"] == pytest.approx(.374) and row["shadow_current_return"] is None for row in measured)
    assert StrategyGovernanceRepository(runtime).rollback_regressing_active() == int(cohort == "losers")
    with runtime.read() as connection:
        actual = connection.execute("SELECT id FROM analysis.strategy_revision WHERE authority_group = 'options-radar-core' AND status = 'active'").fetchone()["id"]
        assert actual == (parent if cohort == "losers" else active)


def test_multiple_public_paper_orders_cannot_supply_one_selected_execution_sample(experiment_context, monkeypatch):
    from investment_panel.infrastructure.postgres import strategy_learning
    from investment_panel.infrastructure.postgres.options_experiments import seed_experiment_shadows
    from investment_panel.domain.decision import promotion_readiness
    from investment_panel.infrastructure.postgres.strategy_governance import StrategyGovernanceRepository, paper_provenance_is_database_backed

    runtime, ingestion, now, _parent, candidate = experiment_context
    ready = _ready_paper_publication(experiment_context, monkeypatch, sleeve_capital=50000)
    ready.analysis.store_option_feature(
        ready.run_id, snapshot_id=ready.quote["snapshot_id"], contract_id=ready.quote["contract_id"],
        quote_observed_at=now, feature_version=FEATURE_VERSION, values={"dte": 40, "spread_pct": .04},
    )
    assert seed_experiment_shadows(runtime, [ready.payload], publication_id=str(ready.publication_id)) == 1
    arguments = dict(decision_id=ready.decision_id, ticket_version=1, quantity=1, limit_price=.5,
                     current_options_risk_sleeve_capital=50000, experiment_publication_id=ready.publication_id)
    actions = ActionRepository(runtime)
    first = actions.stage_option_paper_entry(**arguments, idempotency_key="episode-first-part")
    second = actions.stage_option_paper_entry(**arguments, idempotency_key="episode-second-part")
    assert first["status"] == second["status"] == "staged" and ready.ticket["risk"]["recommended_quantity"] == 2
    execution = OptionsPaperExecutionRepository(runtime)
    for order, entry_second, exit_second, exit_bid in ((first, 10, 20, 1.1), (second, 30, 40, .2)):
        _capture(runtime, ingestion, now + timedelta(seconds=entry_second))
        assert execution._manage_one(order["paper_order_id"], now + timedelta(seconds=entry_second + 1))["status"] == "filled"
        _capture(runtime, ingestion, now + timedelta(seconds=exit_second), bid=exit_bid, ask=exit_bid + .02)
        assert execution._manage_one(order["paper_order_id"], now + timedelta(seconds=exit_second + 1))["status"] == "closed"
    cutoff = now + timedelta(seconds=45)

    class ObservationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff if tz else cutoff.replace(tzinfo=None)

    monkeypatch.setattr(strategy_learning, "datetime", ObservationClock)
    with runtime.read() as connection:
        rows = connection.execute(
            strategy_learning.OUTCOME_QUERY.replace("now()", f"TIMESTAMPTZ '{cutoff.isoformat()}'"), [candidate, candidate],
        ).fetchall()
        observations = connection.execute(
            strategy_learning.OBSERVATION_QUERY.replace("now()", f"TIMESTAMPTZ '{cutoff.isoformat()}'"),
            [[candidate], now - timedelta(hours=1)],
        ).fetchall()
        assert len(rows) == len(observations) == 1
        assert rows[0]["paper_order_count"] == observations[0]["paper_order_count"] == 2
        assert strategy_learning.measured_rows([dict(row) for row in rows]) == []
        selected_winner = {
            "strategy_revision_id": candidate, "database_verified": True, "sample_size": 1,
            "paper_order_ids": [first["paper_order_id"]], "decision_ids": [str(ready.decision_id)],
        }
        assert paper_provenance_is_database_backed(connection, candidate, selected_winner, cutoff=cutoff) is False
        orders = connection.execute("SELECT id::text, created_at, actual_fill_price, exit_price FROM app.paper_order ORDER BY created_at, id").fetchall()
        assert orders[0]["exit_price"] > orders[0]["actual_fill_price"]
        assert orders[1]["exit_price"] < orders[1]["actual_fill_price"]
        # The count includes only orders that existed at the requested clock.
        episode_sql = strategy_learning.PAPER_EPISODE_ORDERS_SQL.replace("now()", "%s::timestamptz")
        for index, order in enumerate(orders, start=1):
            counted = connection.execute(
                f"SELECT episode.* FROM analysis.decision decision CROSS JOIN LATERAL ({episode_sql}) episode WHERE decision.id = %s",
                [order["created_at"], ready.decision_id],
            ).fetchone()
            assert counted["paper_order_count"] == index

    # The public governance read must reject previously stored winner-only
    # evidence even when its structure and old verified flag are valid.
    evidence = {
        "sample_size": 1, "source": "analysis.option_outcome",
        "method": "retained_actionable_decisions_forward_evaluation",
        "version": "phase7-governance-evidence-v1",
        "uncertainty": {"lower_95_expectancy": .01},
        "paper_execution": {**selected_winner, "source": "app.paper_order",
                            "paper_only": True, "completed_orders": 1},
    }
    prior_evaluation = {"evaluation_type": "execution_grade_paper", "verdict": "pass",
                        "evaluated_at": cutoff, "available_at": cutoff, "evidence": evidence}
    blocker = "execution_grade_paper_evidence_not_real"
    assert blocker not in promotion_readiness([prior_evaluation], now=cutoff)["blockers"]
    with runtime.transaction() as connection:
        connection.execute(
            "INSERT INTO analysis.strategy_evaluation "
            "(strategy_revision_id, evaluation_type, evaluated_at, available_at, verdict, metrics, evidence) "
            "VALUES (%s, 'execution_grade_paper', %s, %s, 'pass', '{}', %s)",
            [candidate, cutoff, cutoff, Jsonb(evidence)],
        )
    readiness = StrategyGovernanceRepository(runtime).promotion_readiness(candidate, cutoff=cutoff)
    assert blocker in readiness["blockers"] and readiness["promotion_eligible"] is False
