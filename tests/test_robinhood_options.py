from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.free_sources import store_options_chain
from investment_panel.core.options_radar import persist_option_snapshots
from investment_panel.core.robinhood_options import (
    _authorization_server_metadata,
    authorize_robinhood_mcp,
    collect_robinhood_option_chains,
    load_robinhood_access_token,
    option_quote_row,
    select_robinhood_expiries,
)
from investment_panel.jobs import update_robinhood_options


@dataclass
class _ProviderConfig:
    enabled: bool = True
    mcp_url: str = "https://example.invalid/mcp"
    token_path: str = "~/.config/market/robinhood-mcp-token.json"
    auth_token_env: str = "ROBINHOOD_MCP_TOKEN"
    prefer_codex_credentials: bool = True
    codex_credentials_path: str = "~/.codex/.credentials.json"
    codex_mcp_server_name: str = "robinhood-trading"
    timeout_seconds: int = 30
    readonly: bool = True
    max_symbols: int = 40
    max_expiries: int = 2
    strikes_around_spot: int = 12
    quote_batch_size: int = 20
    collect_puts: bool = False


class _FakeRobinhoodClient:
    def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]:
        return {
            "data": {
                "results": [
                    {
                        "quote": {
                            "symbol": symbol,
                            "last_trade_price": "205.140000",
                            "venue_last_trade_time": "2026-06-12T19:59:59Z",
                            "last_non_reg_trade_price": "205.420000",
                            "venue_last_non_reg_trade_time": "2026-06-12T23:59:59Z",
                            "adjusted_previous_close": "204.870000",
                            "previous_close": "204.870000",
                        }
                    }
                    for symbol in symbols
                ]
            }
        }

    def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]:
        return {
            "data": {
                "chains": [
                    {
                        "id": "chain-nvda",
                        "symbol": underlying_symbol,
                        "expiration_dates": ["2026-06-26", "2027-06-17", "2027-12-17"],
                    }
                ]
            }
        }

    def get_option_instruments(
        self,
        *,
        chain_id: str | None = None,
        chain_symbol: str | None = None,
        expiration_dates: str | None = None,
        option_type: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        assert chain_id == "chain-nvda"
        assert option_type == "call"
        rows = [
            {
                "id": f"nvda-{expiration_dates}-{strike}-c",
                "chain_id": chain_id,
                "chain_symbol": "NVDA",
                "underlying_type": "equity",
                "expiration_date": expiration_dates,
                "strike_price": f"{strike:.4f}",
                "type": "call",
                "state": "active",
                "tradability": "tradable",
            }
            for strike in (205.0, 210.0, 220.0, 240.0, 260.0, 300.0)
        ]
        return {"data": {"instruments": rows, "next": None}}

    def get_option_quotes(self, instrument_ids: list[str]) -> dict[str, Any]:
        return {
            "data": {
                "results": [
                    {
                        "quote": {
                            "instrument_id": instrument_id,
                            "ask_price": "6.150000",
                            "ask_size": 98,
                            "bid_price": "5.950000",
                            "bid_size": 133,
                            "adjusted_mark_price": "6.050000",
                            "mark_price": "6.050000",
                            "previous_close_price": "6.850000",
                            "previous_close_date": "2026-06-11",
                            "implied_volatility": "0.378862",
                            "delta": "0.525386",
                            "gamma": "0.027140",
                            "rho": "0.036225",
                            "theta": "-0.234907",
                            "vega": "0.154123",
                            "open_interest": 3652,
                            "volume": 2046,
                            "chance_of_profit_long": "0.339204",
                            "chance_of_profit_short": "0.660796",
                            "updated_at": "2026-06-12T19:59:59Z",
                        }
                    }
                    for instrument_id in instrument_ids
                ]
            }
        }


def test_option_quote_row_maps_robinhood_fields() -> None:
    instrument = {
        "id": "deba9035-f70b-4257-917c-7bbc9ef06097",
        "chain_id": "chain-nvda",
        "chain_symbol": "NVDA",
        "underlying_type": "equity",
        "expiration_date": "2026-06-26",
        "strike_price": "205.0000",
        "type": "call",
        "state": "active",
        "tradability": "tradable",
    }
    quote = {
        "instrument_id": "deba9035-f70b-4257-917c-7bbc9ef06097",
        "ask_price": "6.150000",
        "bid_price": "5.950000",
        "mark_price": "6.050000",
        "previous_close_price": "6.850000",
        "implied_volatility": "0.378862",
        "delta": "0.525386",
        "gamma": "0.027140",
        "theta": "-0.234907",
        "vega": "0.154123",
        "open_interest": 3652,
        "volume": 2046,
        "updated_at": "2026-06-12T19:59:59Z",
    }

    row = option_quote_row(instrument, quote)

    assert row is not None
    assert row["expiry"] == "2026-06-26"
    assert row["strike"] == 205.0
    assert row["type"] == "call"
    assert row["bid"] == 5.95
    assert row["ask"] == 6.15
    assert row["mid"] == 6.05
    assert row["iv"] == 0.378862
    assert row["open_interest"] == 3652
    assert row["volume"] == 2046
    assert row["contract_symbol"] == "deba9035-f70b-4257-917c-7bbc9ef06097"


def test_robinhood_chain_rows_flow_into_option_snapshots(tmp_path: Path) -> None:
    row = option_quote_row(
        {
            "id": "rh-contract-1",
            "chain_id": "chain-nvda",
            "chain_symbol": "NVDA",
            "underlying_type": "equity",
            "expiration_date": "2027-06-17",
            "strike_price": "210.0000",
            "type": "call",
            "state": "active",
            "tradability": "tradable",
        },
        {
            "instrument_id": "rh-contract-1",
            "bid_price": "40.00",
            "ask_price": "42.00",
            "mark_price": "41.00",
            "implied_volatility": "0.455",
            "delta": "0.622",
            "gamma": "0.012",
            "theta": "-0.1",
            "vega": "0.5",
            "open_interest": 7928,
            "volume": 3,
        },
    )
    assert row is not None

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"})
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('NVDA', '2026-06-12T20:00:00Z', 205.14, 0.13, 0.27, 'USD', 'robinhood', '{}')"
        )
        assert store_options_chain(con, "NVDA", "2026-06-12T20:00:00Z", [row], source="robinhood") == 1
        assert persist_option_snapshots(con, symbols=["NVDA"], source="robinhood") == 1
        rows = query_rows(con, "SELECT open_interest, volume, iv, delta, data_source, contract_id FROM option_snapshot")

    assert rows[0]["open_interest"] == 7928
    assert rows[0]["volume"] == 3
    assert round(float(rows[0]["delta"]), 3) == 0.622
    assert rows[0]["data_source"] == "robinhood"
    assert rows[0]["contract_id"] == "rh-contract-1"


def test_select_robinhood_expiries_filters_to_radar_window() -> None:
    out = select_robinhood_expiries(
        ["2026-06-26", "2027-06-17", "2027-12-17", "2029-01-19", "bad"],
        today=date(2026, 6, 13),
        min_dte=365,
        max_dte=900,
        max_per_symbol=2,
    )
    assert out == ["2027-06-17", "2027-12-17"]


def test_collect_robinhood_option_chains_with_fake_client() -> None:
    result = collect_robinhood_option_chains(
        _ProviderConfig(max_expiries=1, strikes_around_spot=3),
        ["NVDA"],
        client=_FakeRobinhoodClient(),
        min_dte=0,
        max_dte=900,
        max_expiries=1,
        strikes_around_spot=3,
    )

    assert result["errors"] == []
    assert result["quotes"][0]["symbol"] == "NVDA"
    rows = result["rows"]["NVDA"]
    assert rows
    assert {row["market_data"] for row in rows} == {"robinhood"}
    assert all(row["open_interest"] == 3652 for row in rows)


def test_load_robinhood_access_token_from_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    token_path = tmp_path / "token.json"
    token_path.write_text('{"access_token": "cached-token", "expires_at": 4102444800}', encoding="utf-8")

    token = load_robinhood_access_token(_ProviderConfig(token_path=str(token_path)))

    assert token == "cached-token"


def test_load_robinhood_access_token_from_codex_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    token_path = tmp_path / "missing-market-token.json"
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        """
{
  "robinhood-trading|abc": {
    "server_name": "robinhood-trading",
    "server_url": "https://agent.robinhood.com/mcp/trading",
    "client_id": "client",
    "access_token": "codex-token",
    "expires_at": 4102444800000,
    "refresh_token": "refresh",
    "scopes": ["internal"]
  }
}
""",
        encoding="utf-8",
    )

    token = load_robinhood_access_token(
        _ProviderConfig(token_path=str(token_path), codex_credentials_path=str(credentials_path))
    )

    assert token == "codex-token"


def test_authorize_robinhood_mcp_uses_existing_codex_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        """
{
  "robinhood-trading|abc": {
    "server_name": "robinhood-trading",
    "server_url": "https://agent.robinhood.com/mcp/trading",
    "client_id": "client",
    "access_token": "codex-token",
    "expires_at": 4102444800000,
    "refresh_token": "refresh",
    "scopes": ["internal"]
  }
}
""",
        encoding="utf-8",
    )

    result = authorize_robinhood_mcp(_ProviderConfig(codex_credentials_path=str(credentials_path)))

    assert result["status"] == "ok"
    assert result["auth_provider"] == "codex_mcp"
    assert result["server_name"] == "robinhood-trading"


def test_authorization_server_metadata_falls_back_to_origin_well_known(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get_json(url: str, *, timeout: int) -> dict[str, Any]:
        calls.append(url)
        if url == "https://agent.robinhood.com/.well-known/oauth-authorization-server":
            return {
                "authorization_endpoint": "https://robinhood.com/oauth",
                "token_endpoint": "https://api.robinhood.com/oauth2/token/",
            }
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr("investment_panel.core.robinhood_options.auth._get_json", fake_get_json)

    metadata = _authorization_server_metadata("https://agent.robinhood.com/mcp/trading", timeout=30)

    assert metadata["authorization_endpoint"] == "https://robinhood.com/oauth"
    assert "https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading" in calls
    assert calls[-1] == "https://agent.robinhood.com/.well-known/oauth-authorization-server"


def test_update_robinhood_options_reports_auth_required(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  status_dir: {tmp_path / "status"}
data_sources:
  brokers:
    enabled: true
    robinhood:
      enabled: true
      auth_token_env: ROBINHOOD_MCP_TOKEN
      prefer_codex_credentials: false
""",
        encoding="utf-8",
    )

    result = update_robinhood_options.run(str(config_path), symbols=["NVDA"])

    assert result["status"] == "auth_required"
    assert result["provider"] == "robinhood"
    assert result["auth_command"] == "market-update-robinhood-options --auth"
