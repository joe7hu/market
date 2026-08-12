from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from investment_panel.jobs import options_paper_execution
from investment_panel.database.options_paper_execution import (
    _available_quantity,
    _exit_reason,
    _net_pnl,
)
from investment_panel.database.options_paper_quotes import package_price


NOW = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)


def test_paper_fill_prices_are_never_better_than_the_displayed_market_side() -> None:
    debit = [{"side": "buy", "bid": 1.0, "ask": 1.2, "bid_size": 4, "ask_size": 2}]
    credit = [{"side": "sell", "bid": 1.0, "ask": 1.2, "bid_size": 2, "ask_size": 4}]
    assert package_price(debit, phase="entry") == 1.2
    assert package_price(debit, phase="exit") == 1.0
    assert package_price(credit, phase="entry") == 1.0
    assert package_price(credit, phase="exit") == 1.2
    assert _available_quantity(debit, phase="entry", requested=5) == 2
    assert _available_quantity(debit, phase="exit", requested=5) == 4


def test_paper_exit_uses_profit_stop_time_and_liquidity_gates() -> None:
    ticket = {"expiration": "2026-09-30", "expires_at": "2026-08-12T20:00:00+00:00"}
    exits = {"profit_price": 2.0, "loss_price": 0.5, "time_exit_dte": 7}
    assert _exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=2.05, execution_blockers=[], now=NOW,
    ) == "profit_target"
    assert _exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=0.45, execution_blockers=[], now=NOW,
    ) == "stop_loss"
    assert _exit_reason(
        ticket=ticket, exits=exits, credit=True, entry_price=1.0,
        exit_price=0.45, execution_blockers=[], now=NOW,
    ) == "profit_target"
    assert _exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=1.0, execution_blockers=["long_leg_open_interest_below_100"], now=NOW,
    ) == "liquidity_exit"


def test_paper_net_pnl_includes_both_sides_of_conservative_fees() -> None:
    # One long contract bought at 1.20 and sold at 2.00: 80 gross less 1.30 fees.
    assert _net_pnl(credit=False, entry_price=1.2, exit_price=2.0, quantity=1, leg_count=1) == 78.7


def test_kill_switch_blocks_entries_but_keeps_the_safe_exit_manager_running(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _Repository:
        def __init__(self, _runtime) -> None:
            pass

        def manage_orders(self, **kwargs):
            calls["manage"] = kwargs
            return [{"paper_order_id": "existing", "status": "closed"}]

        def process(self, **_kwargs):  # pragma: no cover - this must not run.
            raise AssertionError("disabled lanes must not stage entries")

    settings = SimpleNamespace(
        options_paper_actions_enabled=False,
        radar_paper_actions_enabled=False,
        qqq_paper_actions_enabled=False,
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

    assert result["entry_staging"] == "disabled"
    assert result["managed"][0]["paper_order_id"] == "existing"
    assert calls["manage"]["lanes"] == ("radar", "qqq")
