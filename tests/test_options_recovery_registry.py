from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from investment_panel.core.options_recovery_paper import (
    RecoveryRiskContext,
    recovery_risk_policy,
    size_recovery_position,
)
from investment_panel.core.options_recovery import ExecutableLeg, QuoteCapture
from investment_panel.core.options_recovery_config import OptionsDecisionSystemConfig
from investment_panel.core.options_recovery_registry import (
    EventSpot,
    RecoveryContractQuote,
    RecoveryEventState,
    SHOCK_CONTINUATION_PUT_V1,
    SHOCK_REVERSAL_CALL_V1,
    contract_gate,
    signal_for,
    validate_mutation,
)
from investment_panel.core.options_recovery_ticket import (
    RECOVERY_TICKET_VERSION,
    build_recovery_ticket_v4,
    occ_symbol,
)
from investment_panel.database.options_recovery_execution import RecoveryExecutionRepository


NOW = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)


def _risk_policy(**overrides: object):
    values: dict[str, object] = {
        "options_risk_sleeve_capital": 25_000.0,
        "max_risk_per_trade_pct": 0.02,
        "max_open_risk_pct": 0.10,
        "max_symbol_risk_pct": 0.04,
        "daily_loss_halt_pct": 0.04,
        "max_recovery_open_positions": 5,
    }
    values.update(overrides)
    return recovery_risk_policy(OptionsDecisionSystemConfig(**values))


def _event(prices: list[float]) -> RecoveryEventState:
    return RecoveryEventState(
        event_id="event-1",
        symbol="NVDA",
        reference_price=120.0,
        event_low=100.0,
        started_at=NOW - timedelta(days=5),
        spots=tuple(
            EventSpot(NOW + timedelta(minutes=15 * index), NOW + timedelta(minutes=15 * index), price)
            for index, price in enumerate(prices)
        ),
    )


def _quote(*, option_type: str = "call", **overrides: object) -> RecoveryContractQuote:
    values: dict[str, object] = {
        "contract_id": 42,
        "occ_symbol": "NVDA260814C00100000",
        "option_type": option_type,
        "expiration": date(2026, 8, 14),
        "strike": 100.0,
        "bid": 2.0,
        "ask": 2.2,
        "bid_size": 10,
        "ask_size": 10,
        "open_interest": 100,
        "delta": 0.45 if option_type == "call" else -0.45,
        "observed_at": NOW,
        "available_at": NOW,
    }
    values.update(overrides)
    return RecoveryContractQuote(**values)  # type: ignore[arg-type]


def test_reversal_and_continuation_are_independent_typed_families() -> None:
    reversal = signal_for(_event([100, 100.5, 101, 101.9, 102.1, 103]), SHOCK_REVERSAL_CALL_V1)
    assert reversal.active
    assert "four_slot_breakout" in reversal.reasons

    continuation = signal_for(_event([102, 101, 100.5, 99.5, 99.0]), SHOCK_CONTINUATION_PUT_V1)
    assert continuation.active
    assert "fresh_event_low" in continuation.reasons


def test_contract_gates_fail_closed_for_stale_or_nonexecutable_contracts() -> None:
    result = contract_gate(
        _quote(bid_size=0, available_at=NOW - timedelta(days=1), open_interest=99),
        family=SHOCK_REVERSAL_CALL_V1,
        as_of=NOW,
    )
    assert not result.eligible
    assert set(result.blockers) >= {
        "displayed_size_required",
        "current_session_quote_required",
        "open_interest_below_100",
    }
    assert contract_gate(_quote(option_type="put"), family=SHOCK_REVERSAL_CALL_V1, as_of=NOW).eligible is False


def test_agent_mutation_cannot_introduce_unknown_registry_settings() -> None:
    assert validate_mutation(SHOCK_REVERSAL_CALL_V1, {"reversal_min_rebound_pct": 0.03})[
        "reversal_min_rebound_pct"
    ] == pytest.approx(0.03)
    with pytest.raises(ValueError, match="unsupported recovery strategy parameter"):
        validate_mutation(SHOCK_REVERSAL_CALL_V1, {"made_up_edge": 99})


def test_v4_ticket_has_exact_occ_contract_and_executable_risk() -> None:
    policy = _risk_policy()
    ticket = build_recovery_ticket_v4(
        decision_id="decision-1",
        event_id="event-1",
        symbol="NVDA",
        family=SHOCK_REVERSAL_CALL_V1,
        expiration=date(2026, 8, 14),
        quantity=2,
        invalidation="spot closes below the event low",
        created_at=NOW,
        risk_policy=policy,
        legs=[{
            "contract_id": 42,
            "option_type": "call",
            "strike": 100,
            "bid": 2.0,
            "ask": 2.2,
            "bid_size": 10,
            "ask_size": 10,
            "quote_time": NOW,
            "open_interest": 100,
            "volume": 30,
        }],
    )
    assert ticket["ticket_version"] == RECOVERY_TICKET_VERSION
    assert ticket["legs"][0]["occ_symbol"] == occ_symbol("NVDA", date(2026, 8, 14), "call", 100)
    assert ticket["entry"]["limit_price"] == pytest.approx(2.22)
    assert ticket["risk"]["total_risk"] > 400
    assert ticket["risk"]["per_trade_limit"] == 500
    assert ticket["exit_ladder"]["targets"][1]["multiple"] == 3.0


def test_recovery_risk_limits_and_qualified_session_gate_fail_closed() -> None:
    policy = _risk_policy()
    decision = size_recovery_position(100.0, RecoveryRiskContext(), policy)
    assert decision.quantity == 5
    assert decision.total_risk == 500
    halted = size_recovery_position(
        100.0,
        RecoveryRiskContext(daily_realized_unrealized_pnl=-policy.daily_loss_halt),
        policy,
    )
    assert halted.quantity == 0
    assert "daily_loss_halt" in halted.blockers
    missing = size_recovery_position(100.0, RecoveryRiskContext(), None)
    assert missing.quantity == 0
    assert "recovery_risk_policy_required" in missing.blockers


def test_typed_policy_changes_ticket_limits_and_rejects_incoherent_settings() -> None:
    conservative = _risk_policy(
        options_risk_sleeve_capital=10_000.0,
        max_risk_per_trade_pct=0.01,
        max_open_risk_pct=0.05,
    )
    assert conservative.valid
    assert conservative.per_trade_limit == 100
    assert size_recovery_position(60.0, RecoveryRiskContext(), conservative).quantity == 1

    incoherent = _risk_policy(max_risk_per_trade_pct=0.05, max_symbol_risk_pct=0.04)
    decision = size_recovery_position(60.0, RecoveryRiskContext(), incoherent)
    assert not incoherent.valid
    assert decision.quantity == 0
    assert "per_trade_risk_cannot_exceed_per_symbol_risk" in decision.blockers


class _StageResult:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class _StageConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> "_StageConnection":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, _: object = None) -> _StageResult:
        self.statements.append(statement)
        self.calls.append((statement, _))
        if "SELECT id, status FROM app.paper_order" in statement:
            return _StageResult()
        if "FROM analysis.option_event_signal signal" in statement:
            return _StageResult({"stageable": True})
        if "INSERT INTO app.paper_order" in statement:
            return _StageResult({"id": "paper-order-1"})
        return _StageResult()


class _StageRuntime:
    def __init__(self, connection: _StageConnection) -> None:
        self.connection = connection

    def transaction(self, *_: object) -> _StageConnection:
        return self.connection


class _GreenProgram:
    def as_dict(self) -> dict[str, object]:
        return {"eligible": True}


def test_staging_writes_a_journal_record_after_refactor() -> None:
    connection = _StageConnection()
    repository = RecoveryExecutionRepository(_StageRuntime(connection), risk_policy=_risk_policy())
    repository._risk_context = lambda **_: RecoveryRiskContext()  # type: ignore[method-assign]
    signal = {
        "id": "signal-1",
        "event_id": "event-1",
        "instrument_id": 1,
        "strategy_key": SHOCK_REVERSAL_CALL_V1,
        "decision_id": "decision-1",
        "cohort_id": "cohort-1",
        "symbol": "NVDA",
        "event_contract_id": 7,
        "reference_price": 120.0,
        "event_low": 100.0,
        "started_at": NOW - timedelta(days=4),
        "contract_id": 42,
        "expiration": date(2026, 8, 14),
        "strike": 100.0,
        "option_type": "call",
        "provider_symbols": {"occ": "NVDA260814C00100000"},
        "bid": 0.9,
        "ask": 1.0,
        "bid_size": 10,
        "ask_size": 10,
        "open_interest": 100,
        "provider_delta": 0.45,
        "volume": 30,
        "observed_at": NOW,
        "available_at": NOW,
        "lower_confidence_expectancy": 0.1,
    }

    staged = repository._stage_order(signal, now=NOW, program=_GreenProgram())

    assert staged["status"] == "staged"
    assert any("INSERT INTO app.trade_journal" in statement for statement in connection.statements)


def test_paper_entry_extends_the_event_tape_from_the_actual_fill() -> None:
    connection = _StageConnection()
    repository = RecoveryExecutionRepository(_StageRuntime(connection), risk_policy=_risk_policy())
    repository._order_captures = lambda _order, _now: [  # type: ignore[method-assign]
        QuoteCapture(
            observed_at=NOW,
            legs=(ExecutableLeg("contract", "buy", 0.99, 1.0, 10, 10),),
            session_number=9,
            dte=20,
        )
    ]
    order = {
        "id": "paper-order-1",
        "event_id": "event-1",
        "signal_id": "signal-1",
        "decision_id": "decision-1",
        "instrument_id": 1,
        "strategy_key": SHOCK_REVERSAL_CALL_V1,
        "cohort_id": "cohort-1",
        "created_at": NOW - timedelta(minutes=1),
        "quantity": 1,
        "status": "staged",
        "filled_at": None,
        "ticket_snapshot": {"entry": {"limit_price": 1.01}, "legs": [{"contract_id": 42}]},
    }

    managed = repository._manage_order(order, NOW)

    assert managed["status"] == "entered"
    extension = next(
        (statement, parameters)
        for statement, parameters in connection.calls
        if "UPDATE app.option_history_policy" in statement
    )
    assert extension[1] == [
        datetime(2026, 8, 17, 20, 15, tzinfo=UTC),
        datetime(2026, 8, 17, 20, 15, tzinfo=UTC),
        "event-1",
    ]
