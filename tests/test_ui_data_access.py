from app import data_access


def test_empty_database_returns_duckdb_status(tmp_path) -> None:
    panel_data = data_access.load_panel_data({"database": {"duckdb_path": str(tmp_path / "missing.duckdb")}})

    assert panel_data.status.ready is False
    assert panel_data.status.source == "duckdb"
    assert "Database is initialized" in panel_data.status.message
    assert panel_data.rows("candidates") == []


def test_table_payload_normalizes_rows() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={"candidates": [{"symbol": "ABC"}]},
    )

    payload = data_access.table_payload(panel_data, "candidates")

    assert payload["count"] == 1
    assert payload["rows"][0]["symbol"] == "ABC"


def test_ticker_payload_matches_symbol() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={"candidates": [{"symbol": "ABC"}], "portfolio": []},
    )

    payload = data_access.ticker_payload(panel_data, "abc")

    assert payload["found"] is True
    assert payload["tables"]["candidates"][0]["symbol"] == "ABC"


def test_save_and_delete_portfolio_position(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "portfolio.duckdb")}}

    saved = data_access.save_portfolio_position(
        config,
        {"symbol": "nvda", "quantity": 3, "avg_cost": 125.5, "purchase_date": "2024-01-15", "notes": "core"},
    )
    panel_data = data_access.load_panel_data(config)

    assert saved["symbol"] == "NVDA"
    assert saved["purchase_date"] == "2024-01-15"
    assert panel_data.rows("portfolio")[0]["symbol"] == "NVDA"
    assert panel_data.rows("portfolio")[0]["quantity"] == 3
    assert panel_data.rows("portfolio")[0]["purchase_date"] == "2024-01-15"
    assert panel_data.rows("portfolio")[0]["tax_lot_term"] == "long_term"

    deleted = data_access.delete_portfolio_position(config, "NVDA")
    panel_data = data_access.load_panel_data(config)

    assert deleted == {"symbol": "NVDA", "deleted": True}
    assert panel_data.rows("portfolio") == []
