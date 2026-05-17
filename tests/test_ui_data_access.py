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


def test_ticker_payload_includes_quote_and_signal_context_for_deep_links() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "decision_queue": [{"symbol": "NVDA", "score": 91, "action_grade": "research"}],
            "quotes": [{"symbol": "NVDA", "price": 135.25, "change_pct": 1.4}],
            "technicals": [{"symbol": "NVDA", "technical_score": 82}],
            "opportunity_sources": [{"symbol": "NVDA", "source_key": "technicals", "title": "Technical setup"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "nvda")

    assert payload["found"] is True
    assert payload["tables"]["decision_queue"][0]["score"] == 91
    assert payload["tables"]["quotes"][0]["price"] == 135.25
    assert payload["tables"]["technicals"][0]["technical_score"] == 82
    assert payload["tables"]["opportunity_sources"][0]["source_key"] == "technicals"


def test_settings_payload_exposes_config_and_integration_metadata() -> None:
    config = {
        "database": {"duckdb_path": "/tmp/market.duckdb"},
        "arco": {"raw_dir": "/Users/joehu/brain/raw/sources/arco"},
        "birdclaw": {"command": "birdclaw export"},
    }
    panel_data = data_access.PanelData(status=data_access.DataStatus(True, "ok", "test"), tables={})

    payload = data_access.settings_payload(config, panel_data)

    assert payload["status"]["ready"] is True
    assert payload["config"]["database"]["duckdb_path"] == "/tmp/market.duckdb"
    assert payload["integration"]["duckdb_path"] == "/tmp/market.duckdb"
    assert payload["integration"]["arco_raw_dir"] == "/Users/joehu/brain/raw/sources/arco"
    assert payload["integration"]["birdclaw_command"] == "birdclaw export"


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
