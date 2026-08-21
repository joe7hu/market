from __future__ import annotations

from app.actions.options import OptionsActions
from app.actions.portfolio import PortfolioActions
from conftest import typed_config


def test_options_action_owns_thesis_response_assembly() -> None:
    class Agents:
        def submit(self, kind, payload):
            assert kind == "option_thesis"
            assert payload["strategy_version"] == "professional-v2"
            return "thesis-1"

    actions = OptionsActions.__new__(OptionsActions)
    actions.agents = Agents()

    assert actions.submit_thesis({"strategy_version": "professional-v2"}) == {
        "status": "accepted",
        "thesis_id": "thesis-1",
        "strategy_version": "professional-v2",
        "agent_thesis_validations": 1,
    }


def test_portfolio_action_owns_transaction_response_reload() -> None:
    stored = []

    def record(_config, transaction):
        stored.append(transaction)
        return {"id": "tx-1", **transaction}

    actions = PortfolioActions(
        typed_config(),
        portfolio_rows=lambda _config: [{"symbol": "NVDA"}],
        table_payload=lambda rows: {"rows": rows, "count": len(rows)},
        preview_transaction=lambda *_args: {},
        record_transaction=record,
        reverse_transaction=lambda *_args, **_kwargs: {},
        watchlist_rows=lambda _config: [],
        save_watchlist=lambda *_args: {},
        populate_watchlist=lambda *_args: {},
        delete_watchlist=lambda *_args: {},
    )

    result = actions.record_transaction({"symbol": "NVDA", "transaction_type": "buy"})

    assert stored == [{"symbol": "NVDA", "transaction_type": "buy"}]
    assert result["transaction"]["id"] == "tx-1"
    assert result["portfolio"] == {"rows": [{"symbol": "NVDA"}], "count": 1}
