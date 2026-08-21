from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from investment_panel.analysis.history_v3 import MODEL_REVISION, analyze_group, static_arbitrage_findings
from investment_panel.core.option_underwriting import (
    conservative_entry,
    conservative_mark,
    historical_payoff_statistics,
    paper_state,
)
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.actions import _v3_paper_readiness
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_history_v3 import is_later_capture_cohort
from investment_panel.database.options_history_v3_materialization import (
    group_verified_contract_rows,
    surface_summary,
)
from investment_panel.database.options_history_v3_surface import surface_shape_metrics
from investment_panel.database.options_decision_system import OptionsDecisionSystemRepository
from investment_panel.database.runtime import DatabaseRuntime
from conftest import typed_config
from investment_panel.database.thesis import save_thesis


def _rows(option_type: str = "call") -> list[dict[str, object]]:
    now = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    return [
        {
            "contract_id": index + 1, "strike": 470 + index * 5, "option_type": option_type,
            "bid": max(0.1, 34 - index * 2.5) if option_type == "call" else 4 + index * 2.5,
            "ask": max(0.2, 34.2 - index * 2.5) if option_type == "call" else 4.2 + index * 2.5,
            "mid": max(0.15, 34.1 - index * 2.5) if option_type == "call" else 4.1 + index * 2.5,
            "open_interest": 500, "underlying_price": 500.0, "market_data_status": "open",
            "group_started_at": now, "group_finished_at": now + timedelta(seconds=4),
            "provider_observed_at": now + timedelta(seconds=3), "underlying_observed_at": now,
        }
        for index in range(12)
    ]


def test_surface_shape_metrics_materialize_skew_and_term_slope() -> None:
    grouped = {}
    spots = {}
    observed = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    for dte, expiration, atm_iv, delta_iv in (
        (10, "2026-08-01", 0.20, 0.23),
        (20, "2026-08-11", 0.22, 0.25),
        (30, "2026-08-21", 0.24, 0.27),
    ):
        key = (expiration, "call")
        grouped[key] = [
            {
                "strike": 500, "dte": dte, "provider_iv": atm_iv,
                "provider_delta": 0.50, "bid": 2.0, "ask": 2.1, "mid": 2.05,
                "open_interest": 500, "underlying_price": 500,
                "provider_observed_at": observed,
                "group_finished_at": observed + timedelta(seconds=2),
            },
            {
                "strike": 520, "dte": dte, "provider_iv": delta_iv,
                "provider_delta": 0.25, "bid": 1.0, "ask": 1.1, "mid": 1.05,
                "open_interest": 500, "underlying_price": 500,
                "provider_observed_at": observed,
                "group_finished_at": observed + timedelta(seconds=2),
            },
        ]
        spots[key] = 500.0
    metrics = surface_shape_metrics(grouped, spots, minimum_points=2)
    assert all(row["skew_25"] == pytest.approx(0.03) for row in metrics.values())
    assert all(row["term_slope"] == pytest.approx(0.002) for row in metrics.values())


def test_adjusted_rows_do_not_reject_the_verified_standard_surface() -> None:
    standard = {
        "expiration": "2026-08-21", "option_type": "call", "multiplier": 100,
        "style": "american", "settlement": "physical",
        "deliverable_key": "standard-chain", "standard_contract_verified": True,
    }
    adjusted = {
        **standard, "deliverable_key": "adjusted-chain",
        "standard_contract_verified": False,
    }

    grouped, ambiguous, excluded = group_verified_contract_rows(
        [{**standard, "contract_id": 1}, {**adjusted, "contract_id": 2}]
    )

    assert [row["contract_id"] for row in grouped[("2026-08-21", "call")]] == [1]
    assert ambiguous == 0
    assert excluded == 1


def test_surface_shape_metrics_ignore_quality_rejected_nearest_quote() -> None:
    observed = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    common = {
        "dte": 30, "open_interest": 500, "underlying_price": 500,
        "group_finished_at": observed + timedelta(seconds=2),
    }
    rows = [
        {
            **common, "strike": 500, "provider_iv": 0.90, "provider_delta": 0.50,
            "bid": 2.0, "ask": 2.1, "mid": 2.05,
            "provider_observed_at": observed - timedelta(minutes=10),
        },
        {
            **common, "strike": 505, "provider_iv": 0.20, "provider_delta": 0.50,
            "bid": 2.0, "ask": 2.1, "mid": 2.05, "provider_observed_at": observed,
        },
        {
            **common, "strike": 520, "provider_iv": 0.23, "provider_delta": 0.25,
            "bid": 1.0, "ask": 1.1, "mid": 1.05, "provider_observed_at": observed,
        },
    ]

    metrics = surface_shape_metrics(
        {("2026-08-21", "call"): rows},
        {("2026-08-21", "call"): 500},
        minimum_points=2,
    )

    assert metrics[("2026-08-21", "call")]["skew_25"] == pytest.approx(0.03)


def test_surface_summary_atm_iv_ignores_quality_rejected_nearest_quote() -> None:
    observed = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    rows = []
    for index in range(12):
        strike = 490 + index * 2
        if strike == 500:
            strike = 501
        rows.append({
            "contract_id": index + 1, "strike": strike, "option_type": "call",
            "dte": 30, "provider_iv": 0.20, "provider_delta": 0.50,
            "bid": 2.0, "ask": 2.1, "mid": 2.05, "open_interest": 500,
            "underlying_price": 500, "provider_observed_at": observed,
            "group_started_at": observed,
            "group_finished_at": observed + timedelta(seconds=2),
        })
    rows.append({
        **rows[0], "contract_id": 99, "strike": 500, "provider_iv": 0.90,
        "provider_observed_at": observed - timedelta(minutes=10),
    })
    result = {
        "relative_values": [{"classification": "rejected"} for _row in rows],
        "blockers": [], "static_findings": [],
        "fit": SimpleNamespace(diagnostics={}), "row_metrics": {},
    }

    summary = surface_summary(rows, result, 500)

    assert summary["atm_iv"] == pytest.approx(0.20)


def test_price_shape_fit_is_deterministic_and_clean_chain_has_no_static_candidate() -> None:
    first = analyze_group(_rows(), spot=500.0, option_type="call")
    second = analyze_group(_rows(), spot=500.0, option_type="call")
    assert first["fit"].status == "succeeded"
    assert first["fit"].fitted == second["fit"].fitted
    assert not first["static_findings"]
    assert {row["classification"] for row in first["relative_values"]} <= {"rejected", "relative_cheap", "relative_rich"}


def test_live_status_and_mixed_bad_quotes_are_row_scoped() -> None:
    rows = _rows() + [
        {**_rows()[0], "contract_id": 100, "strike": 530.0, "market_data_status": "live"},
        {**_rows()[1], "contract_id": 101, "strike": 535.0, "market_data_status": "live"},
    ]
    rows[0]["market_data_status"] = "live"
    rows[1]["market_data_status"] = "delayed"
    rows[2]["provider_observed_at"] = rows[2]["group_finished_at"] - timedelta(minutes=4)
    result = analyze_group(rows, spot=500.0, option_type="call")
    assert result["fit"].status == "succeeded"
    assert result["eligible_count"] == 12
    assert result["row_metrics"] == {
        "total_rows": 14, "eligible_rows": 12, "stale_rows": 1,
        "invalid_status_rows": 1, "missing_underlying_rows": 0, "rejected_rows": 2,
    }
    by_id = {row["contract_id"]: row for row in result["relative_values"]}
    assert "invalid_market_status" in by_id[2]["blockers"]
    assert "quote_age_stale" in by_id[3]["blockers"]


def test_stale_and_missing_underlying_groups_retain_rejection_reasons() -> None:
    stale = _rows()
    for row in stale:
        row["provider_observed_at"] = row["group_finished_at"] - timedelta(minutes=4)
    stale_result = analyze_group(stale, spot=500.0, option_type="call")
    assert stale_result["fit"].status == "fit_failed"
    assert all("quote_age_stale" in row["blockers"] for row in stale_result["relative_values"])

    missing = _rows()
    missing[0]["underlying_price"] = None
    missing_result = analyze_group(missing, spot=None, option_type="call", group_blockers=["missing_aligned_underlying"])
    assert missing_result["fit"].status == "fit_failed"
    assert all("missing_aligned_underlying" in row["blockers"] for row in missing_result["relative_values"])


def test_static_arbitrage_uses_executable_worst_side_and_detects_bounds() -> None:
    rows = _rows()
    rows[0]["ask"] = 1.0  # substantially below the call intrinsic value of 30
    findings = static_arbitrage_findings(rows, spot=500.0, option_type="call")
    assert any(item["kind"] == "intrinsic_lower_bound" for item in findings)
    audited_wing = [
        {"contract_id": 800, "strike": 800.0, "bid": 1.00, "ask": 1.10, "mid": 1.05},
        {"contract_id": 805, "strike": 805.0, "bid": 0.95, "ask": 1.05, "mid": 1.00},
    ]
    assert not static_arbitrage_findings(audited_wing, spot=700.0, option_type="call")


def test_append_only_retry_advances_pointer_without_mixing_quotes(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(runtime)
    ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
    slot = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    rows = [
        {"underlying_symbol": "QQQ", "expiry": "2026-08-21", "strike": 480 + index * 5,
         "type": "call", "underlying_price": 500, "bid": 5 - index * 0.1,
         "ask": 5.2 - index * 0.1, "mid": 5.1 - index * 0.1, "open_interest": 200,
         "style": "american", "settlement": "physical", "deliverable_key": "qqq-standard",
         "standard_contract_verified": True}
        for index in range(12)
    ]
    first_run = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=first_run)
    partial = history.store_capture(run_id=first_run, source_id="robinhood", symbol="QQQ", slot_at=slot, captured={"rows": rows[:2], "expected_contract_count": len(rows), "received_contract_count": 2, "capture_started_at": slot, "capture_finished_at": slot})
    ingestion.finish_run(first_run, "partial", summary=partial)
    second_run = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=second_run)
    complete = history.store_capture(run_id=second_run, source_id="robinhood", symbol="QQQ", slot_at=slot, captured={"rows": rows, "expected_contract_count": len(rows), "received_contract_count": len(rows), "capture_started_at": slot, "capture_finished_at": slot + timedelta(seconds=1)})
    ingestion.finish_run(second_run, "succeeded", summary=complete)
    assert history.chain(symbol="QQQ", snapshot=complete["snapshot_id"])["count"] == len(rows)
    groups = history.surface_groups(symbol="QQQ", snapshot=complete["snapshot_id"])
    assert groups["rows"] == [{"expiration": datetime(2026, 8, 21).date(), "option_type": "call", "dte": 32, "contract_count": len(rows)}]
    with runtime.read() as connection:
        generations = connection.execute("SELECT capture_state, received_contract_count FROM raw.option_capture_generation ORDER BY generation").fetchall()
        pointer = connection.execute("SELECT latest_complete_generation_id FROM raw.option_snapshot WHERE id = %s", [complete["snapshot_id"]]).fetchone()
    assert [row["capture_state"] for row in generations] == ["partial", "complete"]
    assert [row["received_contract_count"] for row in generations] == [2, len(rows)]
    assert pointer["latest_complete_generation_id"] == complete["capture_generation_id"]
    first_replay = history.v3.materialize(
        snapshot_id=complete["snapshot_id"], capture_generation_id=complete["capture_generation_id"], code_version="test-replay"
    )
    second_replay = history.v3.materialize(
        snapshot_id=complete["snapshot_id"], capture_generation_id=complete["capture_generation_id"], code_version="test-replay"
    )
    assert first_replay["deterministic_hash"] == second_replay["deterministic_hash"]
    assert first_replay["analysis_run_id"] == second_replay["analysis_run_id"]
    assert second_replay["idempotent_replay"] is True
    assert first_replay["mode"] == "historical_evidence"
    with runtime.read() as connection:
        assert connection.execute(
            "SELECT count(*) FROM analysis.shadow_trade WHERE source_kind = 'options_history_v3'"
        ).fetchone()["count"] == 0
    with runtime.transaction() as connection:
        candidate = connection.execute(
            "SELECT id FROM analysis.option_relative_value WHERE analysis_run_id = %s LIMIT 1",
            [second_replay["analysis_run_id"]],
        ).fetchone()
        connection.execute(
            "UPDATE analysis.option_relative_value SET classification = 'historical_static_arbitrage_candidate' WHERE id = %s",
            [candidate["id"]],
        )
        connection.execute(
            "INSERT INTO analysis.option_relative_value_verification (relative_value_id, status, blockers, evidence) VALUES (%s, 'verified', ARRAY[]::text[], '{}'::jsonb)",
            [candidate["id"]],
        )
    verified = OptionsDecisionSystemRepository(runtime).relative_values(
        symbol="QQQ", snapshot=complete["snapshot_id"], classification="verified_static_arbitrage_candidate"
    )
    assert [row["id"] for row in verified["rows"]] == [candidate["id"]]
    assert verified["rows"][0]["verification_status"] == "verified"
    runtime.close()


def test_candidate_capture_persists_json_safe_leg_observation_times(migrated_postgres_dsn: str) -> None:
    """A candidate-producing live capture must not fail while writing its evidence legs."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        history = OptionHistoryRepository(runtime)
        ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
        slot = datetime.now(UTC) + timedelta(minutes=1)
        expiration = (slot.date() + timedelta(days=30)).isoformat()
        save_thesis(
            typed_config(migrated_postgres_dsn),
            "QQQ",
            {
                "thesis": "QQQ remains bullish while the current trend and breadth regime hold.",
                "why": "Core QQQ options-underwriting benchmark.",
                "direction": "long",
                "horizon_date": expiration,
                "invalidation_rules": [
                    {"type": "price", "operator": "<=", "price": 480, "text": "QQQ closes below 480"},
                ],
            },
        )
        finished_at = slot + timedelta(seconds=5)
        rows = [
            {
                "underlying_symbol": "QQQ",
                "expiry": expiration,
                "strike": 470 + index * 5,
                "type": "call",
                "underlying_price": 500,
                    # The first two quotes are below intrinsic value and intentionally
                    # produce independent research observations for cohort isolation.
                    "bid": 29.8 - index * 5 if index < 2 else max(0.4, 30 - index * 4 - 0.1),
                    "ask": 29.9 - index * 5 if index < 2 else max(0.5, 30 - index * 4),
                    "mid": 29.85 - index * 5 if index < 2 else max(0.45, 30 - index * 4 - 0.05),
                "open_interest": 500,
                "provider_delta": 0.5,
                "market_data_status": "open",
                "style": "american",
                "settlement": "physical",
                "deliverable_key": "qqq-standard",
                "standard_contract_verified": True,
            }
            for index in range(12)
        ]
        run_id = ingestion.start_run("robinhood", "option_history_full")
        assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id)

        captured = history.store_capture(
            run_id=run_id,
            source_id="robinhood",
            symbol="QQQ",
            slot_at=slot,
            captured={
                "rows": rows,
                "expected_contract_count": len(rows),
                "received_contract_count": len(rows),
                "capture_started_at": slot,
                "capture_finished_at": finished_at,
                "quote_diagnostics": {
                    "groups": {
                        f"{expiration}:call": {
                            "started_at": slot,
                            "finished_at": finished_at,
                            "underlying_observed_at": slot,
                        }
                    }
                },
            },
        )
        assert captured["decision_candidates"] >= 1
        diagnostic = captured["quote_diagnostics"]["groups"][f"{expiration}:call"]
        assert datetime.fromisoformat(diagnostic["finished_at"]) == finished_at
        ingestion.finish_run(run_id, "succeeded", summary=captured)

        with runtime.read() as connection:
            evidence = connection.execute(
                """
                SELECT option_decision.synthetic_legs -> 0 ->> 'observed_at' AS observed_at
                FROM analysis.option_decision option_decision
                JOIN analysis.decision decision ON decision.id = option_decision.decision_id
                WHERE decision.run_id = %s
                LIMIT 1
                """,
                [captured["analysis_run_id"]],
            ).fetchone()
        assert evidence is not None
        assert datetime.fromisoformat(evidence["observed_at"]) == finished_at
        candidate_payload = OptionsDecisionSystemRepository(runtime).candidates(symbol="QQQ")
        assert candidate_payload["rows"]
        assert candidate_payload["rows"][0]["legs"]
        assert candidate_payload["rows"][0]["conservative_entry"]["fill_basis"]
        assert "expected" in candidate_payload["rows"][0]["expected_value_interval"]
        brief = OptionsDecisionSystemRepository(runtime).decision_brief(symbol="QQQ", lane="thesis")
        assert brief["readiness"]["analysis"]["eligible_groups"] >= 1
        assert brief["readiness"]["thesis"]["eligible"] is True
        assert brief["readiness"]["thesis"]["invalidation"] == "QQQ closes below 480"
        assert brief["strongest_candidate"] is not None
        repository = OptionsDecisionSystemRepository(runtime)
        with runtime.transaction() as connection:
            decision_id = connection.execute(
                "SELECT id FROM analysis.decision WHERE run_id = %s ORDER BY created_at LIMIT 1",
                [captured["analysis_run_id"]],
            ).fetchone()["id"]
            connection.execute("UPDATE analysis.decision SET state = 'WATCH' WHERE id = %s", [decision_id])
            connection.execute(
                """
                UPDATE analysis.option_decision
                SET paper_state = 'WATCH', max_loss = 100, expected_value = 25,
                    probability_profit = 0.6, structure = 'long_call',
                    market_regime = 'above_200d:normal'
                WHERE decision_id = %s
                """,
                [decision_id],
            )
            connection.execute(
                """
                INSERT INTO analysis.shadow_trade
                    (decision_id, status, structure, market_regime, source_kind, metrics)
                VALUES (%s, 'pending', 'long_call', 'above_200d:normal', 'options_history_v3', '{}'::jsonb)
                """,
                [decision_id],
            )
        assert repository.paper_journal(symbol="QQQ")["count"] == 0
        shadow = repository.shadow_observations(symbol="QQQ")
        assert shadow["count"] >= 1
        initial_counts = repository.workspace(symbol="QQQ")["tab_counts"]
        assert initial_counts["journal"] == 0
        assert initial_counts["shadow_observations"] >= 1
        observation = shadow["rows"][0]
        assert observation["record_kind"] == "shadow_observation"
        assert observation["admission"]["decision_state"] in {"COLLECTING", "WATCH"}
        assert observation["admission"]["paper_state"] in {"COLLECTING", "WATCH"}
        assert observation["contract"]["expiration"] == expiration
        assert observation["contract"]["legs"][0]["strike"] is not None
        assert observation["thesis"]["revision"] == 1
        assert observation["thesis"]["direction"] == "long"
        assert observation["thesis"]["invalidation"] == "QQQ closes below 480"
        assert observation["forecast"]["max_loss"] is not None
        assert "expected_value" in observation["forecast"]
        assert observation["execution"]["entry_cohort_id"] is None
        assert observation["outcome"]["current_return"] is None
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.decision SET blockers = ARRAY['thesis_upgrade_required'] WHERE id = %s",
                [decision_id],
            )
        assert repository.shadow_observations(symbol="QQQ")["count"] == 0
        assert repository.shadow_observations(symbol="QQQ", include_legacy=True)["count"] >= 1
        legacy_counts = repository.workspace(symbol="QQQ")["tab_counts"]
        assert legacy_counts["shadow_observations"] == 0
        assert legacy_counts["legacy_shadow_observations"] >= 1
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.decision SET blockers = ARRAY[]::text[] WHERE id = %s", [decision_id])
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.decision SET state = 'READY' WHERE id = %s", [decision_id])
            connection.execute(
                "UPDATE analysis.option_decision SET paper_state = 'PAPER_READY' WHERE decision_id = %s",
                [decision_id],
            )
            instrument_id = connection.execute(
                "SELECT instrument_id FROM analysis.decision WHERE id = %s", [decision_id]
            ).fetchone()["instrument_id"]
            connection.execute(
                """
                INSERT INTO app.paper_order
                    (decision_id, instrument_id, side, quantity, limit_price, status, structure, idempotency_key)
                VALUES (%s, %s, 'buy', 1, 29.9, 'staged', 'long_call', 'journal-contract-test')
                """,
                [decision_id, instrument_id],
            )
        assert repository.shadow_observations(symbol="QQQ")["count"] == 0
        paper = repository.paper_journal(symbol="QQQ")
        assert paper["count"] == 1
        promoted_counts = repository.workspace(symbol="QQQ")["tab_counts"]
        assert promoted_counts["journal"] == 1
        assert promoted_counts["shadow_observations"] == 0
        assert paper["rows"][0]["record_kind"] == "paper_trade"
        assert paper["rows"][0]["paper_order_id"]
        assert paper["rows"][0]["admission"]["paper_state"] == "PAPER_READY"
        with runtime.transaction() as connection:
            paper_shadow_id = connection.execute(
                "SELECT id FROM analysis.shadow_trade WHERE decision_id = %s", [decision_id]
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO analysis.option_outcome
                    (decision_id, maturity_state, observed_through, current_return, outcome_source, shadow_trade_id)
                VALUES (%s, 'mature', now(), 0.2, 'options_history_v3', %s)
                """,
                [decision_id, paper_shadow_id],
            )
            other = connection.execute(
                """
                SELECT decision.id
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                WHERE decision.run_id = %s AND decision.id <> %s
                ORDER BY decision.created_at LIMIT 1
                """,
                [captured["analysis_run_id"], decision_id],
            ).fetchone()["id"]
            connection.execute("UPDATE analysis.decision SET state = 'WATCH' WHERE id = %s", [other])
            connection.execute(
                """
                UPDATE analysis.option_decision
                SET paper_state = 'WATCH', structure = 'long_call',
                    market_regime = 'above_200d:normal', probability_profit = 0.6
                WHERE decision_id = %s
                """,
                [other],
            )
            other_shadow_id = connection.execute(
                """
                INSERT INTO analysis.shadow_trade
                    (decision_id, status, structure, market_regime, source_kind, metrics)
                VALUES (%s, 'entered', 'long_call', 'above_200d:normal', 'options_history_v3', '{}'::jsonb)
                RETURNING id
                """,
                [other],
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO analysis.option_outcome
                    (decision_id, maturity_state, observed_through, current_return, outcome_source, shadow_trade_id)
                VALUES (%s, 'mature', now(), -0.5, 'options_history_v3', %s)
                """,
                [other, other_shadow_id],
            )
        learning = repository.learning_progress(symbol="QQQ")
        long_call = next(row for row in learning["rows"] if row["structure"] == "long_call")
        assert long_call["mature_outcomes"] == 1
        save_thesis(
            typed_config(migrated_postgres_dsn),
            "QQQ",
            {
                "thesis": "QQQ is range-bound pending a decisive macro or breadth signal.",
                "why": "Core QQQ options-underwriting benchmark.",
                "direction": "neutral",
                "horizon_date": "2026-12-31",
                "invalidation_rules": [
                    {"type": "time", "text": "Reassess when independent directional evidence improves."},
                ],
            },
        )
        neutral_brief = OptionsDecisionSystemRepository(runtime).decision_brief(symbol="QQQ", lane="thesis")
        assert neutral_brief["readiness"]["thesis"]["eligible"] is False
        assert neutral_brief["readiness"]["thesis"]["present"] is True
        assert neutral_brief["readiness"]["thesis"]["direction"] == "neutral"
        assert neutral_brief["readiness"]["thesis"]["blocker"] == "thesis_direction_required"
        assert neutral_brief["readiness"]["next_required_action"] == "wait_for_directional_qqq_thesis"
    finally:
        runtime.close()


def test_health_counts_observed_dates_and_qualified_sessions_not_snapshots(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_panel.database import options_history_canary

    monkeypatch.setattr(options_history_canary, "SCHEDULED_REGULAR_SLOTS", 2)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        history = OptionHistoryRepository(runtime)
        ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
        with runtime.transaction() as connection:
            connection.execute("DELETE FROM analysis.option_history_canary")
            connection.execute(
                "INSERT INTO analysis.option_history_canary (model_revision, started_at) VALUES (%s, %s)",
                [MODEL_REVISION, datetime(2026, 7, 19, tzinfo=UTC)],
            )
        for day in (20, 21):
            for minute in (30, 45):
                slot = datetime(2026, 7, day, 14, minute, tzinfo=UTC)
                rows = [
                    {"underlying_symbol": "QQQ", "expiry": "2026-08-21", "strike": 470 + index * 5,
                     "type": "call", "underlying_price": 500, "bid": 30 - index * 2,
                     "ask": 30.1 - index * 2, "mid": 30.05 - index * 2, "open_interest": 500,
                     "market_data_status": "live", "style": "american",
                     "settlement": "physical", "deliverable_key": "qqq-standard",
                     "standard_contract_verified": True}
                    for index in range(12)
                ]
                run_id = ingestion.start_run("robinhood", "option_history_full")
                assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id)
                stored = history.store_capture(
                    run_id=run_id, source_id="robinhood", symbol="QQQ", slot_at=slot,
                    captured={"rows": rows, "expected_contract_count": 12, "received_contract_count": 12,
                              "capture_started_at": slot, "capture_finished_at": slot + timedelta(seconds=2)},
                )
                ingestion.finish_run(run_id, "succeeded", summary=stored)
        health = history.health()
        assert health["complete_captures"] == 4
        assert health["observed_regular_session_dates"] == 2
        assert health["qualified_regular_sessions"] == 2
        assert health["canary_revision"] == MODEL_REVISION
    finally:
        runtime.close()


def test_underwriting_never_uses_midpoint_for_debit_fill_or_mark() -> None:
    now = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    legs = [
        {"side": "long", "option_type": "call", "strike": 500, "bid": 2.0, "ask": 2.2, "observed_at": now, "size_available": True},
        {"side": "short", "option_type": "call", "strike": 505, "bid": 0.8, "ask": 1.0, "observed_at": now + timedelta(seconds=2), "size_available": True},
    ]
    assert conservative_entry(legs, "call_debit_spread") == (1.4000000000000001, [])
    assert conservative_mark(legs, "call_debit_spread") == (1.0, [])
    result = paper_state(structure="put_debit_spread", lane="anomaly", thesis=None, fit_status="succeeded")
    assert result["paper_state"] == "WATCH"
    assert result["blockers"] == ["thesis_upgrade_required"]


def test_shadow_entry_requires_a_strictly_later_capture_cohort() -> None:
    earlier = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    later = earlier + timedelta(minutes=15)

    assert is_later_capture_cohort(earlier, later)
    assert not is_later_capture_cohort(earlier, earlier)
    assert not is_later_capture_cohort(later, earlier)


def test_paper_state_requires_exact_gates_and_all_structures_can_watch() -> None:
    thesis = {
        "schema_version": 2, "direction": "bullish", "horizon_date": "2026-08-20",
        "invalidation": "QQQ closes below 480", "max_loss": 500,
    }
    assert paper_state(structure="long_call", lane="thesis", thesis=thesis, fit_status="fit_failed")["paper_state"] == "REJECT"
    assert paper_state(structure="call_debit_spread", lane="thesis", thesis=thesis, fit_status="succeeded")["paper_state"] == "COLLECTING"
    bearish = {**thesis, "direction": "bearish"}
    for structure, option_thesis in (("long_put", bearish), ("put_debit_spread", bearish), ("long_call", thesis), ("call_debit_spread", thesis)):
        state = paper_state(
            structure=structure, lane="thesis", thesis=option_thesis, fit_status="succeeded", scenario_count=20,
            expected_value=1, lower_95_expected_value=0.1, max_loss=100, data_confidence=0.9, execution_confidence=0.8,
        )
        assert state["paper_state"] == "WATCH"
    ready = paper_state(
        structure="long_call", lane="thesis", thesis=thesis, fit_status="succeeded", scenario_count=20,
        expected_value=1, lower_95_expected_value=0.1, max_loss=100, data_confidence=0.9, execution_confidence=0.8,
        calibration={"sample_size": 30, "prediction_sample_size": 30, "lower_95_expectancy": 0.01, "brier_score": 0.2, "other_regime_monitoring_count": 5},
    )
    assert ready["paper_state"] == "PAPER_READY"


def test_paper_state_accepts_canonical_v3_thesis_with_deterministic_risk_cap() -> None:
    thesis = {
        "schema_version": 3,
        "direction": "long",
        "horizon_date": "2026-08-20",
        "lifecycle_status": "active",
        "invalidation_rules": [
            {"type": "price", "operator": "<=", "price": 480, "text": "QQQ closes below 480"},
        ],
    }

    watch = paper_state(
        structure="long_call",
        lane="thesis",
        thesis=thesis,
        fit_status="succeeded",
        scenario_count=20,
        expected_value=1,
        lower_95_expected_value=0.1,
        max_loss=100,
        data_confidence=0.9,
        execution_confidence=0.8,
    )
    rejected = paper_state(
        structure="long_call",
        lane="thesis",
        thesis=thesis,
        fit_status="succeeded",
        scenario_count=20,
        expected_value=1,
        lower_95_expected_value=0.1,
        max_loss=600,
        data_confidence=0.9,
        execution_confidence=0.8,
    )

    assert watch["paper_state"] == "WATCH"
    assert watch["reasons"] == ["exact_structure_regime_calibration_collecting"]
    assert rejected["paper_state"] == "REJECT"
    assert rejected["blockers"] == ["thesis_max_loss_exceeded"]


def test_historical_payoff_statistics_is_seeded_and_never_uses_midpoint_entry() -> None:
    legs = [
        {"option_type": "call", "side": "long", "strike": 100.0, "bid": 4.8, "ask": 5.0, "observed_at": datetime(2026, 7, 20, 14, 30, tzinfo=UTC), "size_available": True},
        {"option_type": "call", "side": "short", "strike": 105.0, "bid": 2.0, "ask": 2.2, "observed_at": datetime(2026, 7, 20, 14, 30, tzinfo=UTC), "size_available": True},
    ]
    returns = (-0.10, -0.02, 0.01, 0.04, 0.08) * 4

    first = historical_payoff_statistics(spot=100.0, legs=legs, terminal_returns=returns, seed=7)
    second = historical_payoff_statistics(spot=100.0, legs=legs, terminal_returns=returns, seed=7)

    assert first == second
    assert first["scenario_count"] == 20
    assert first["entry_price"] == pytest.approx(3.0)  # long ask minus short bid
    assert first["max_loss"] == pytest.approx(300.0)
    assert first["lower_95_expected_value"] is not None


def test_v3_paper_readiness_requires_a_fresh_coherent_leg_package() -> None:
    now = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    payload = {
        "quote_observed_at": now.isoformat(),
        "leg_quotes": [{"bid": 2.0, "ask": 2.2, "size_available": True, "observed_at": now.isoformat(), "available_at": now.isoformat()}],
    }

    assert _v3_paper_readiness(payload, now) == "A"
    assert _v3_paper_readiness(payload, now + timedelta(minutes=6)) == "C"
    assert _v3_paper_readiness({**payload, "leg_quotes": [{"bid": 2.2, "ask": 2.0}]}, now) == "C"
    assert _v3_paper_readiness({**payload, "leg_quotes": [{"bid": 2.0, "ask": 2.2}]}, now) == "C"
    assert conservative_entry([{**payload["leg_quotes"][0], "side": "long", "option_type": "call", "strike": 500, "size_available": None}], "long_call")[0] is None


def test_claimed_generation_is_terminal_after_collector_failure(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(runtime)
    ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
    slot = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
    run_id = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id)
    history.fail_capture(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id, error=TimeoutError("provider timeout"))
    with runtime.read() as connection:
        row = connection.execute(
            "SELECT capture_state FROM raw.option_capture_generation WHERE ingest_run_id = %s", [run_id]
        ).fetchone()
    assert row["capture_state"] == "failed"
    runtime.close()
