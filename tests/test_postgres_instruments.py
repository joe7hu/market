from __future__ import annotations

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.instruments import canonical_symbol, instrument_identity, reconcile_instrument


def test_instrument_identity_normalizes_aliases_and_classification() -> None:
    assert canonical_symbol("$btc") == "BTC-USD"
    assert instrument_identity("SPY", asset_class="equity")["asset_class"] == "etf"
    assert instrument_identity("BTC", asset_class="equity")["market_timezone"] == "UTC"


def test_reconcile_instrument_improves_placeholders_without_downgrading(migrated_postgres_dsn: str) -> None:
    runtime = runtime_for_config({"database": {"url": migrated_postgres_dsn}})
    with runtime.transaction() as connection:
        first_id = reconcile_instrument(
            connection,
            "SPY",
            name="SPY",
            asset_class="unknown",
            category="option-discovery",
        )
        second_id = reconcile_instrument(
            connection,
            "$spy",
            name="SPDR S&P 500 ETF",
            asset_class="equity",
            category="market_data",
        )
        row = connection.execute(
            "SELECT symbol, name, asset_class, category FROM catalog.instrument WHERE id = %s",
            [first_id],
        ).fetchone()
    assert second_id == first_id
    assert dict(row) == {
        "symbol": "SPY",
        "name": "SPDR S&P 500 ETF",
        "asset_class": "etf",
        "category": "market_data",
    }
