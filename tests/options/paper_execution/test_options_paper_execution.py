from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from investment_panel.jobs import options_paper_execution
from investment_panel.core.decision import ExpressionKind
from investment_panel.database import options_paper_execution as paper_execution_database
from investment_panel.database import ticker_execution as ticker_execution_database
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.options_paper_execution import GENERIC_LANES, OptionsPaperExecutionRepository
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository
from investment_panel.database.options_paper_ledger import active_paper_exposure
from investment_panel.database.options_paper_execution import (
    available_quantity,
    exit_reason,
    net_pnl,
)
from investment_panel.database.options_paper_quotes import package_price


NOW = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)


class _Result:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def execute(self, statement: str, parameters=None) -> _Result:
        self.statements.append((statement, parameters))
        return _Result()


def _open_order(*, filled_quantity: float = 1, exited_quantity: float = 0) -> dict[str, object]:
    return {
        "id": "paper-order-1",
        "instrument_id": 1,
        "lane": "radar",
        "status": "entered",
        "quantity": filled_quantity,
        "filled_quantity": filled_quantity,
        "exited_quantity": exited_quantity,
        "actual_fill_price": 1.0,
        "structure": "long_option",
    }


def _executable_long_quote(*, quote_time: datetime, bid_size: int = 1) -> dict[str, object]:
    return {
        "contract_id": "1",
        "side": "buy",
        "bid": 2.0,
        "ask": 2.1,
        "bid_size": bid_size,
        "ask_size": bid_size,
        "open_interest": 100,
        "quote_time": quote_time,
    }


def test_paper_fill_prices_are_never_better_than_the_displayed_market_side() -> None:
    debit = [{"side": "buy", "bid": 1.0, "ask": 1.2, "bid_size": 4, "ask_size": 2}]
    credit = [{"side": "sell", "bid": 1.0, "ask": 1.2, "bid_size": 2, "ask_size": 4}]
    assert package_price(debit, phase="entry") == 1.2
    assert package_price(debit, phase="exit") == 1.0
    assert package_price(credit, phase="entry") == 1.0
    assert package_price(credit, phase="exit") == 1.2
    assert available_quantity(debit, phase="entry", requested=5) == 2
    assert available_quantity(debit, phase="exit", requested=5) == 4


def test_paper_exit_uses_profit_stop_time_and_liquidity_gates() -> None:
    ticket = {"expiration": "2026-09-30", "expires_at": "2026-08-12T20:00:00+00:00"}
    exits = {"profit_price": 2.0, "loss_price": 0.5, "time_exit_dte": 7}
    assert exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=2.05, execution_blockers=[], now=NOW,
    ) == "profit_target"
    assert exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=0.45, execution_blockers=[], now=NOW,
    ) == "stop_loss"
    assert exit_reason(
        ticket=ticket, exits=exits, credit=True, entry_price=1.0,
        exit_price=0.45, execution_blockers=[], now=NOW,
    ) == "profit_target"
    assert exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=1.0, execution_blockers=["long_leg_open_interest_below_100"], now=NOW,
    ) == "liquidity_exit"


def test_paper_net_pnl_includes_both_sides_of_conservative_fees() -> None:
    # One long contract bought at 1.20 and sold at 2.00: 80 gross less 1.30 fees.
    assert net_pnl(credit=False, entry_price=1.2, exit_price=2.0, quantity=1, leg_count=1) == 78.7


def test_phase4_option_execution_math_and_coercion_are_conservative() -> None:
    legs = [{"side": "sell", "bid": 1.0, "ask": 1.2}, {"side": "buy", "bid": 0.2, "ask": 0.4}]
    assert paper_execution_database._midpoint_package(legs) == 0.8
    assert paper_execution_database._entry_slippage(legs, 0.7, True) == 0.1
    assert paper_execution_database._entry_slippage(legs, 1.0, False) == 0.2
    assert paper_execution_database._exit_slippage(legs, 1.0, True) == 0.2
    assert paper_execution_database._exit_slippage(legs, 0.7, False) == 0.1
    assert paper_execution_database._midpoint_package([{"bid": 0, "ask": 1}]) is None
    assert paper_execution_database._entry_slippage([{"bid": 0, "ask": 1}], 1, True) is None
    assert paper_execution_database._exit_slippage([{"bid": 0, "ask": 1}], 1, False) is None
    assert paper_execution_database._fees(2, 3) == 3.9
    assert paper_execution_database._net_pnl(credit=False, entry_price=1.0, exit_price=2.0, quantity=2, leg_count=1) == 197.4
    assert paper_execution_database._timestamp(NOW.isoformat()) == NOW
    assert paper_execution_database._timestamp(NOW.replace(tzinfo=None)) == NOW
    assert paper_execution_database._timestamp("bad") is None
    assert paper_execution_database._utc(None).tzinfo is UTC
    assert paper_execution_database._date(NOW) == NOW.date()
    assert paper_execution_database._date(NOW.date()) == NOW.date()
    assert paper_execution_database._date("2026-08-12") == NOW.date()
    assert paper_execution_database._date("bad") is None
    assert paper_execution_database._number(None) is None
    assert paper_execution_database._number("1.5") == 1.5
    assert paper_execution_database._number("") is None
    assert paper_execution_database._number("bad") is None
    assert paper_execution_database._integer("2") == 2
    assert paper_execution_database._integer(None) is None
    assert paper_execution_database._integer("") is None
    assert paper_execution_database._integer("bad") is None
    assert paper_execution_database._quantity("2.5") == 2.5
    assert paper_execution_database._quantity(None) == 0
    assert paper_execution_database._quantity("bad") == 0
    assert str(paper_execution_database._uuid("00000000-0000-0000-0000-000000000001")) == "00000000-0000-0000-0000-000000000001"


def test_phase4_ticker_option_guards_validate_sizes_quotes_and_dates() -> None:
    leg = {
        "contract_id": "contract:1", "option_type": "put", "side": "sell", "strike": 100,
        "bid": 2.0, "ask": 2.2, "bid_size": 3, "ask_size": 4, "quote_time": NOW,
        "expiration": date(2026, 9, 18),
    }
    assert ticker_execution_database._complete_option_legs([leg])
    assert not ticker_execution_database._complete_option_legs([{**leg, "contract_id": None}])
    assert ticker_execution_database._option_available_quantity([leg], 5, phase="entry") == 3
    assert ticker_execution_database._option_available_quantity([leg], 5, phase="exit") == 4
    assert ticker_execution_database._option_available_quantity([{**leg, "bid_size": 0}], 5, phase="entry") == 0
    assert ticker_execution_database._option_available_quantity([{**leg, "ask_size": 0}], 5, phase="exit") == 0
    assert ticker_execution_database._option_available_quantity([{**leg, "ask_size": None}], 5, phase="exit") == 0
    assert ticker_execution_database._option_available_quantity([], 5, phase="entry") == 0
    assert ticker_execution_database._option_midpoint([leg]) == 2.1
    assert ticker_execution_database._option_midpoint([]) is None
    assert ticker_execution_database._option_midpoint([{**leg, "bid": -1}]) is None
    assert ticker_execution_database._option_midpoint([{**leg, "ask": 1.0}]) is None
    assert ticker_execution_database._option_midpoint([{**leg, "ask": 1.0}]) is None
    assert ticker_execution_database._option_expiration({}, [leg]) == date(2026, 9, 18)
    assert ticker_execution_database._option_expiration({"expiration": "2026-09-19"}, []) == date(2026, 9, 19)
    assert ticker_execution_database._option_expiration({"legs": [{"expiration": "2026-09-20"}]}, []) == date(2026, 9, 20)
    assert ticker_execution_database._option_expiration({"expiration": NOW}, []) == NOW.date()
    assert ticker_execution_database._option_expiration({"expiration": "bad"}, []) is None
    assert ticker_execution_database._option_expiration({}, []) is None
    assert not ticker_execution_database._complete_option_legs([{**leg, "quote_time": "bad"}])
    assert ticker_execution_database._limit_reached("buy", 99, 100)
    assert ticker_execution_database._limit_reached("sell", 101, 100)
    assert not ticker_execution_database._limit_reached("buy", 101, 100)
    assert not ticker_execution_database._limit_reached("sell", 99, 100)
    assert ticker_execution_database._option_structure(ExpressionKind.CALL) == "long_call"
    assert ticker_execution_database._option_structure(ExpressionKind.PUT) == "long_put"
    assert ticker_execution_database._option_structure(ExpressionKind.DEBIT_SPREAD) == "debit_spread"
    assert ticker_execution_database._option_structure(ExpressionKind.CASH_SECURED_PUT) == "cash_secured_put"
    assert ticker_execution_database._utc(NOW.replace(tzinfo=None)) == NOW
    assert ticker_execution_database._utc(None).tzinfo is UTC
    assert ticker_execution_database._timestamp(NOW) == NOW
    assert ticker_execution_database._number("bad") is None
    assert ticker_execution_database._number(float("inf")) is None
    assert ticker_execution_database._quantity(-2) == 0
    assert ticker_execution_database._timestamp("bad") is None


def test_phase4_ticker_paper_switches_fail_closed() -> None:
    repo = object.__new__(TickerPaperExecutionRepository)
    settings = SimpleNamespace(
        mode="paper", ticker_paper_actions_enabled=True,
        stock_paper_actions_enabled=True, options_paper_actions_enabled=True,
    )
    repo.config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings))
    repo._check_switches(ExpressionKind.STOCK)
    repo._check_switches(ExpressionKind.CALL)
    for field, kind in (
        ("mode", ExpressionKind.STOCK), ("ticker_paper_actions_enabled", ExpressionKind.STOCK),
        ("stock_paper_actions_enabled", ExpressionKind.STOCK), ("options_paper_actions_enabled", ExpressionKind.CALL),
    ):
        original = getattr(settings, field)
        setattr(settings, field, "live" if field == "mode" else False)
        with pytest.raises(ValueError):
            repo._check_switches(kind)
        setattr(settings, field, original)


@pytest.mark.parametrize(
    ("global_enabled", "radar_enabled", "qqq_enabled", "expected_lanes"),
    [
        (False, False, False, []),
        (True, True, False, ["radar"]),
        (True, False, True, ["qqq"]),
        (True, True, True, ["radar", "qqq"]),
    ],
)
def test_lane_switches_only_control_entry_staging(
    monkeypatch,
    global_enabled: bool,
    radar_enabled: bool,
    qqq_enabled: bool,
    expected_lanes: list[str],
) -> None:
    calls: dict[str, object] = {}

    class _Repository:
        def __init__(self, _runtime) -> None:
            pass

        def process(self, **kwargs):
            calls["process"] = kwargs
            return {
                "status": "ok",
                "entry_staging": "enabled" if kwargs["enabled_lanes"] else "disabled",
                "staged": [],
                "managed": [{"paper_order_id": "existing", "status": "closed"}],
            }

    settings = SimpleNamespace(
        options_paper_actions_enabled=global_enabled,
        radar_paper_actions_enabled=radar_enabled,
        qqq_paper_actions_enabled=qqq_enabled,
        options_risk_sleeve_capital=25_000,
        daily_loss_halt_pct=0.02,
        max_recovery_open_positions=2,
        decision_inbox_enabled=True,
    )
    monkeypatch.setattr(
        options_paper_execution,
        "load_config",
        lambda _path: SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings)),
    )
    monkeypatch.setattr(options_paper_execution, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(options_paper_execution, "OptionsPaperExecutionRepository", _Repository)

    result = options_paper_execution.run("config.yaml")

    assert result["paper_only"] is True
    assert calls["process"]["enabled_lanes"] == expected_lanes


def test_process_stages_enabled_lanes_but_manages_all_existing_generic_positions(monkeypatch) -> None:
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    calls: dict[str, object] = {}

    def stage_current_ready(**kwargs):
        calls["stage"] = kwargs
        return [{"paper_order_id": "new-radar-order"}]

    def manage_orders(**kwargs):
        calls["manage"] = kwargs
        return [{"paper_order_id": "existing-qqq-order"}]

    monkeypatch.setattr(repository, "stage_current_ready", stage_current_ready)
    monkeypatch.setattr(repository, "manage_orders", manage_orders)

    result = repository.process(
        enabled_lanes=("radar",),
        sleeve_capital=25_000,
        daily_loss_halt_pct=0.02,
        max_open_positions=2,
        decision_inbox_enabled=True,
        now=NOW,
    )

    assert calls["stage"]["enabled_lanes"] == ("radar",)
    assert calls["manage"]["lanes"] == GENERIC_LANES
    assert result["managed"] == [{"paper_order_id": "existing-qqq-order"}]


@pytest.mark.parametrize(
    ("market_open", "quote_time", "expected_blocker"),
    [
        (True, NOW - timedelta(seconds=121), "quote_age_over_120_seconds"),
        (False, NOW - timedelta(seconds=1), "regular_market_session_required"),
    ],
)
def test_unexecutable_exit_quote_never_books_exit_or_pnl(
    monkeypatch,
    market_open: bool,
    quote_time: datetime,
    expected_blocker: str,
) -> None:
    connection = _RecordingConnection()
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    quote = _executable_long_quote(quote_time=quote_time)
    monkeypatch.setattr(paper_execution_database, "latest_option_legs", lambda *_args, **_kwargs: [quote])
    monkeypatch.setattr(paper_execution_database, "is_market_open", lambda _now: market_open)

    result = repository._manage_open(
        connection,
        _open_order(),
        {
            "expiration": "2026-09-30",
            "exits": {"profit_price": 1.5, "loss_price": 0.5, "time_exit_dte": 7},
        },
        [{"contract_id": "1"}],
        NOW,
    )

    assert result["reason"] == "profit_target_pending_executable_quote"
    assert expected_blocker in result["blockers"]
    assert any("unfilled_reason" in statement for statement, _ in connection.statements)
    assert not any("exit_price" in statement for statement, _ in connection.statements)
    assert not any("app.trade_journal" in statement for statement, _ in connection.statements)


def test_partial_exit_keeps_residual_position_open_until_all_filled_contracts_exit(monkeypatch) -> None:
    connection = _RecordingConnection()
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    quote = _executable_long_quote(quote_time=NOW - timedelta(seconds=1), bid_size=1)
    monkeypatch.setattr(paper_execution_database, "latest_option_legs", lambda *_args, **_kwargs: [quote])
    monkeypatch.setattr(paper_execution_database, "is_market_open", lambda _now: True)

    result = repository._manage_open(
        connection,
        _open_order(filled_quantity=3, exited_quantity=1),
        {
            "expiration": "2026-09-30",
            "exits": {"profit_price": 1.5, "loss_price": 0.5, "time_exit_dte": 7},
        },
        [{"contract_id": "1"}],
        NOW,
    )

    assert result["status"] == "filled"
    assert result["exit_quantity"] == 1
    update_parameters = next(
        parameters
        for statement, parameters in connection.statements
        if "SET status = %s, exited_quantity" in statement
    )
    assert update_parameters[0] == "partial_exited"
    assert update_parameters[1] == 2


def test_partial_exit_residual_is_aggregated_for_risk_and_cash_collateral(migrated_postgres_dsn: str) -> None:
    from investment_panel.database.runtime import DatabaseRuntime

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "PARTIAL", category="test")
            connection.execute(
                """
                INSERT INTO app.paper_order
                    (instrument_id, side, quantity, limit_price, status, policy_result,
                     lane, structure, reserved_collateral, ticket_snapshot,
                     filled_quantity, exited_quantity)
                VALUES
                    (%s, 'buy', 4, 10, 'partial_exited', '{}'::jsonb,
                     'radar', 'call_debit_spread', NULL, %s, 4, 3),
                    (%s, 'sell', 4, 10, 'partial_exited', '{}'::jsonb,
                     'qqq', 'cash_secured_put', 4000, '{}'::jsonb, 4, 3)
                """,
                [instrument_id, Jsonb({"risk": {"total_risk": 400}}), instrument_id],
            )
            exposure = active_paper_exposure(
                connection,
                symbol="PARTIAL",
                instrument_id=instrument_id,
            )
    finally:
        runtime.close()

    assert float(exposure["symbol_risk"]) == 100
    assert float(exposure["total_risk"]) == 100
    assert float(exposure["symbol_csp_collateral"]) == 1000
    assert float(exposure["total_csp_collateral"]) == 1000
    assert float(exposure["total_committed"]) == 1100
    assert exposure["unvalued_commitments"] == 0
