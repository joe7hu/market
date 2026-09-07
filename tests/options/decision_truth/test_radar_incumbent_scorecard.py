from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.dependencies import get_options_research
from app.routers.options import router as options_router
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.opportunity_scorecards import OpportunityScorecardRepository
from investment_panel.database.options_experiments import EXPERIMENT_VERSION, incumbent_identity
from investment_panel.database.options_paper_execution import OptionsPaperExecutionRepository
from investment_panel.database.runtime import DatabaseRuntime


@pytest.fixture
def radar_history(migrated_postgres_dsn, application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    reference = datetime.now(UTC) + timedelta(seconds=10)
    inception = reference - timedelta(days=100)
    owner = DatabaseRuntime(migrated_postgres_dsn)
    owner.open()
    try:
        ingestion = IngestionRepository(owner)
        ingestion.register_source("scorecard-test", name="Scorecard", family="test", kind="option_chain")
        with ingestion.run("scorecard-test", "option_quotes", started_at=inception) as run:
            capture = ingestion.store_option_snapshot(
                run.id, source_id="scorecard-test", observed_at=inception, market_session="regular", universe="test",
                rows=[{"symbol": "SCORE", "expiration": (reference + timedelta(days=50)).date().isoformat(),
                       "strike": 100, "option_type": "call", "bid": 1, "ask": 1.1, "mid": 1.05,
                       "deliverable_key": "score-standard"}],
            )
    finally:
        owner.close()
    with runtime.transaction() as connection:
        contract = connection.execute("SELECT id, underlying_instrument_id FROM catalog.option_contract").fetchone()
        revisions = {}
        for name, status in [("incumbent", "active"), ("candidate", "candidate"), ("old", "superseded")]:
            revisions[name] = connection.execute("""
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group, created_at, promoted_at)
                VALUES (%s, 1, %s, %s, '{}', 'options-radar-core', %s, %s) RETURNING id
            """, [f"scorecard-{name}", name, status, inception, inception]).fetchone()["id"]
    analysis = AnalysisRepository(runtime)

    def observe(episode, at, *, policy="incumbent", retained=True, outcome=True, ticket=None, legacy=False):
        revision = revisions[policy]
        kind = "options-paper-experiment" if policy == "candidate" else "options-radar"
        identity = incumbent_identity(revision)
        cohort = None if legacy else "option-scorecard-truth-v1:radar"
        run_id = analysis.start_run(
            kind, input_cutoff=at, code_version="scorecard-test", strategy_revision_id=revision,
            inputs={**({} if legacy else {"observation": identity}), "cutoff": at.isoformat(),
                    "episode": episode, "ticket": ticket},
        )
        identity = incumbent_identity(revision, str(run_id))
        with runtime.transaction() as connection:
            decision_id = connection.execute("""
                INSERT INTO analysis.decision
                    (run_id, instrument_id, decision_key, kind, state, as_of, input_hash,
                     strategy_revision_id, lane, episode_key, sample_eligible, calibration_cohort)
                VALUES (%s, %s, %s, 'option', 'WATCH', %s, %s, %s, 'radar', %s,
                        true, %s) RETURNING id
            """, [run_id, contract["underlying_instrument_id"], f"{episode}-{run_id}", at,
                "a" * 64, revision, episode, cohort]).fetchone()["id"]
            connection.execute("""
                INSERT INTO analysis.option_decision
                    (decision_id, contract_id, snapshot_id, quote_observed_at, probability_profit, structure)
                VALUES (%s, %s, %s, %s, .8, 'long_call')
            """, [decision_id, contract["id"], capture["snapshot_id"], inception])
        own_ticket = ticket or {"state": "WATCH", "experiment": identity}
        payload = {"stable_key": str(decision_id), "decision_id": str(decision_id),
                   "experiment": identity, "ticket": own_ticket}
        scope = f"options-paper-incumbent:{revision}" if retained else "options-radar"
        if policy == "candidate":
            scope = f"options-paper-experiment:{revision}"
        publication_id = analysis.publish(
            run_id, scope,
            {"option_paper_experiment" if retained else "option_radar_opportunity": [payload]},
            complete_run_summary={},
        )
        with runtime.transaction() as connection:
            shadow_id = None
            if retained:
                shadow_id = connection.execute("""
                    INSERT INTO analysis.shadow_trade
                        (decision_id, status, source_kind, entry_at, entry_price, exit_at, exit_price, metrics)
                    VALUES (%s, 'closed', 'options_paper_experiment', %s, 1, %s, 1.213, %s) RETURNING id
                """, [decision_id, at + timedelta(minutes=1), at + timedelta(hours=1),
                    Jsonb({"experiment": identity, "publication_id": str(publication_id), "ticket": own_ticket})]).fetchone()["id"]
            if outcome:
                connection.execute("""
                    INSERT INTO analysis.option_outcome
                        (decision_id, maturity_state, observed_through, current_return, realized_exit_return,
                         outcome_classification, objective_version, promotion_eligible, sample_eligible,
                         shadow_trade_id, entry_fill_at, entry_fill_price, exit_fill_at, exit_fill_price,
                         fee_total, slippage_total, lane, episode_key, calibration_cohort)
                    VALUES (%s, 'mature', %s, .2, .2, 'captured', %s, false, %s, %s,
                            %s, 1, %s, 1.213, 1.3, .1, 'radar', %s, %s)
                """, [decision_id, at + timedelta(hours=1), EXPERIMENT_VERSION if retained else "legacy",
                    retained, shadow_id, at + timedelta(minutes=1), at + timedelta(hours=1), episode, cohort])
        return {"decision_id": decision_id, "run_id": run_id, "shadow_id": shadow_id,
                "publication_id": publication_id, "strategy_revision_id": revision}

    try:
        yield runtime, reference, revisions, observe
    finally:
        runtime.close()


def test_retained_incumbent_outcomes_unlock_the_actual_radar_entry_gate(radar_history, migrated_postgres_dsn):
    runtime, reference, _revisions, observe = radar_history
    dates = [reference - timedelta(days=days) for days in range(90, 1, -1)
             if (reference - timedelta(days=days)).weekday() < 5][:30]
    for index, at in enumerate(dates):
        episode = f"radar:retained-{index}"
        observe(episode, at)
        # Repeated incumbent captures have ineligible generic marks; a newer
        # candidate and an older policy also share the stable episode identity.
        observe(episode, at + timedelta(hours=2), retained=False)
        observe(episode, at + timedelta(hours=3), policy="candidate")
        observe(episode, at + timedelta(hours=4), policy="old")
    for hours in (0, 1):
        observe("radar:legacy", dates[0] + timedelta(hours=hours), retained=False, legacy=True)
    sizing = observe("radar:sizing", dates[0], retained=False)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("""UPDATE analysis.decision SET quality_status = 'sizing_blocked',
            sample_eligible = false, quarantine_reason = 'quality_status_sizing_blocked',
            blockers = ARRAY['missing_cash_context'] WHERE id = %s""", [sizing["decision_id"]])
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["raw_observation_count"] == 63
    assert score["excluded_legacy_observation_count"] == 2
    assert score["excluded_legacy_independent_episode_count"] == 1
    assert score["quarantined_independent_episode_count"] == 0 and score["data_health_defects"] == {}
    assert score["independent_episode_count"] == 31
    assert score["resolved_independent_episode_count"] == 30
    assert score["status"] == "READY_FOR_REVIEW" and score["gaps"] == []
    assert score["expectancy"] == pytest.approx(.2)
    assert score["funnel"]["filled"] == score["funnel"]["closed"] == 0

    ticket = {"lane": "radar", "state": "READY", "ticket_version": 1, "structure": "long_call",
              "execution_ready_at": (reference - timedelta(minutes=1)).isoformat(),
              "expires_at": (reference + timedelta(minutes=10)).isoformat(),
              "risk": {"recommended_quantity": 1}, "entry": {"limit_price": 1}}
    ready = observe("radar:new-entry", reference - timedelta(minutes=1), retained=False, outcome=False, ticket=ticket)
    staged = []

    def stage(**kwargs):
        staged.append(kwargs)
        return {"status": "staged", "decision_id": str(kwargs["decision_id"])}

    paper = OptionsPaperExecutionRepository(runtime)
    # Leave publication loading and the real scorecard gate intact. The action
    # after that gate is recorded so this test does not bypass its own risk suite.
    paper.actions = SimpleNamespace(stage_option_paper_entry=stage)
    result = paper.stage_current_ready(
        enabled_lanes=["radar"], sleeve_capital=25000, daily_loss_halt_pct=.02,
        max_open_positions=3, now=reference, limit=1,
    )
    assert result == [{"status": "staged", "decision_id": str(ready["decision_id"])}]
    assert len(staged) == 1
    assert staged[0]["current_options_risk_sleeve_capital"] == 25000
    assert staged[0]["daily_loss_halt_pct"] == .02 and staged[0]["max_open_positions"] == 3

    # A missing marker from the current writer is a defect even though the
    # complete historical sample remains sufficient and old rows are excluded.
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("UPDATE analysis.decision SET calibration_cohort = NULL WHERE id = %s", [ready["decision_id"]])
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["resolved_independent_episode_count"] == 30 and score["status"] == "INVALID"
    assert score["data_health_defects"] == {"current_observation_truth_contract_missing": 1}
    assert score["excluded_legacy_observation_count"] == 2
    blocked = paper.stage_current_ready(
        enabled_lanes=["radar"], sleeve_capital=25000, daily_loss_halt_pct=.02,
        max_open_positions=3, now=reference, limit=1,
    )
    assert blocked[0]["reason"] == "radar_independent_episode_gate" and len(staged) == 1


@pytest.mark.parametrize("defect", [
    "wrong_identity", "wrong_ticket", "future_publication", "future_run", "wrong_episode",
    "ineligible", "nan_cost", "future_outcome", "rejected", "missing_truth",
])
def test_invalid_retained_outcome_cannot_fall_back_to_a_later_generic_mark(radar_history, migrated_postgres_dsn, defect):
    runtime, reference, revisions, observe = radar_history
    at = reference - timedelta(days=2)
    retained = observe("radar:one", at)
    observe("radar:one", at + timedelta(hours=2), retained=False)
    assert OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)["resolved_independent_episode_count"] == 1
    with psycopg.connect(migrated_postgres_dsn) as connection:
        if defect == "wrong_identity":
            connection.execute("UPDATE analysis.shadow_trade SET metrics = jsonb_set(metrics, '{experiment,incumbent_revision_id}', %s) WHERE id = %s",
                               [Jsonb(revisions["candidate"]), retained["shadow_id"]])
        elif defect == "wrong_ticket":
            connection.execute("UPDATE analysis.shadow_trade SET metrics = jsonb_set(metrics, '{ticket,state}', '\"READY\"') WHERE id = %s", [retained["shadow_id"]])
        elif defect == "future_publication":
            connection.execute("UPDATE app.publication SET published_at = %s WHERE id = %s", [reference + timedelta(days=1), retained["publication_id"]])
        elif defect == "future_run":
            connection.execute("UPDATE analysis.run SET input_cutoff = %s WHERE id = %s", [reference + timedelta(days=1), retained["run_id"]])
        elif defect == "rejected":
            connection.execute("UPDATE analysis.decision SET state = 'REJECTED' WHERE id = %s", [retained["decision_id"]])
        elif defect == "missing_truth":
            connection.execute("UPDATE analysis.decision SET calibration_cohort = NULL WHERE id = %s", [retained["decision_id"]])
        else:
            updates = {
                "wrong_episode": ("episode_key", "radar:other"), "ineligible": ("sample_eligible", False),
                "nan_cost": ("fee_total", "NaN"), "future_outcome": ("observed_through", reference + timedelta(days=1)),
            }
            column, value = updates[defect]
            connection.execute(f"UPDATE analysis.option_outcome SET {column} = %s WHERE decision_id = %s", [value, retained["decision_id"]])
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["independent_episode_count"] == (0 if defect == "missing_truth" else 1)
    assert score["resolved_independent_episode_count"] == 0
    assert score["status"] != "READY_FOR_REVIEW" and score["expectancy"] is None
    if defect == "missing_truth":
        assert score["data_health_defects"] == {"current_observation_truth_contract_missing": 1}
        assert score["quarantined_independent_episode_count"] == 1
        assert score["excluded_legacy_observation_count"] == 0


def test_candidate_only_and_unobserved_incumbent_marks_cannot_qualify_radar(radar_history):
    runtime, reference, _revisions, observe = radar_history
    at = reference - timedelta(days=2)
    observe("radar:one", at, policy="candidate")
    observe("radar:two", at, policy="old")
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["raw_observation_count"] == score["independent_episode_count"] == 0
    observe("radar:one", at + timedelta(hours=1), retained=False)
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["raw_observation_count"] == score["independent_episode_count"] == 1
    assert score["resolved_independent_episode_count"] == 0 and score["status"] == "COLLECTING"


def test_legacy_only_history_is_explicitly_excluded_and_has_no_returns(radar_history):
    runtime, reference, _revisions, observe = radar_history
    for hours in (0, 1):
        observe("radar:legacy", reference - timedelta(days=2, hours=hours), retained=False, legacy=True)
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["raw_observation_count"] == score["excluded_legacy_observation_count"] == 2
    assert score["excluded_legacy_independent_episode_count"] == 1
    assert score["independent_episode_count"] == score["resolved_independent_episode_count"] == 0
    assert score["quarantined_independent_episode_count"] == 0 and score["data_health_defects"] == {}
    assert score["status"] == "COLLECTING" and score["expectancy"] is score["lower_95_expectancy"] is None

    api = FastAPI()
    api.include_router(options_router)
    api.dependency_overrides[get_options_research] = lambda: SimpleNamespace(
        opportunity_scorecard=lambda **kwargs: OpportunityScorecardRepository(runtime).scorecard(as_of=reference, **kwargs),
    )
    response = TestClient(api).get("/api/opportunity-scorecard?lane=radar&window=120")
    assert response.status_code == 200
    payload = response.json()
    assert payload["excluded_legacy_observation_count"] == 2
    assert payload["excluded_legacy_independent_episode_count"] == 1
    assert payload["status"] == "COLLECTING" and payload["expectancy"] is None


def test_retained_shadow_without_truth_or_run_marker_is_still_a_current_defect(radar_history):
    runtime, reference, _revisions, observe = radar_history
    observe("radar:missing-truth", reference - timedelta(days=2), legacy=True)
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["status"] == "INVALID" and score["resolved_independent_episode_count"] == 0
    assert score["data_health_defects"] == {"current_observation_truth_contract_missing": 1}
    assert score["quarantined_independent_episode_count"] == 1
    assert score["excluded_legacy_observation_count"] == 0 and score["expectancy"] is None


def test_missing_cash_sizing_remains_excluded_without_returns_or_entry_readiness(radar_history, migrated_postgres_dsn):
    runtime, reference, _revisions, observe = radar_history
    sizing = observe("radar:sizing-only", reference - timedelta(days=2), retained=False)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("""UPDATE analysis.decision SET quality_status = 'sizing_blocked',
            sample_eligible = false, quarantine_reason = 'quality_status_sizing_blocked',
            blockers = ARRAY['missing_cash_context'] WHERE id = %s""", [sizing["decision_id"]])
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["status"] == "COLLECTING" and score["data_health_defects"] == {}
    assert score["independent_episode_count"] == 1 and score["resolved_independent_episode_count"] == 0
    assert score["expectancy"] is score["lower_95_expectancy"] is None
    with runtime.read() as connection:
        decision = connection.execute("SELECT sample_eligible, blockers FROM analysis.decision WHERE id = %s", [sizing["decision_id"]]).fetchone()
    assert decision["sample_eligible"] is False and decision["blockers"] == ["missing_cash_context"]


@pytest.mark.parametrize("retained", [False, True])
def test_null_episode_stays_visible_as_an_integrity_defect(radar_history, retained):
    runtime, reference, _revisions, observe = radar_history
    observe(None, reference - timedelta(days=2), retained=retained)
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["status"] == "INVALID" and score["missing_episode_key_count"] == 1
    assert score["data_health_defects"]["missing_episode_key"] == 1
    assert score["independent_episode_count"] == score["resolved_independent_episode_count"] == 0


def test_latest_id_breaks_equal_capture_time_ties_before_outcome_selection(radar_history, migrated_postgres_dsn):
    runtime, reference, _revisions, observe = radar_history
    at = reference - timedelta(days=2)
    first = observe("radar:tie", at, retained=False)
    # A separate input identity produces another immutable run at the same
    # market cutoff, as repeated production captures do.
    second = observe("radar:tie", at, retained=False, ticket={"state": "SETUP"})
    older = min(first["decision_id"], second["decision_id"])
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("UPDATE analysis.option_outcome SET sample_eligible = true, promotion_eligible = true WHERE decision_id = %s", [older])
    score = OpportunityScorecardRepository(runtime).scorecard(lane="radar", as_of=reference)
    assert score["independent_episode_count"] == 1 and score["resolved_independent_episode_count"] == 0
    assert score["status"] == "COLLECTING" and score["expectancy"] is None
