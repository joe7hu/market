from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from investment_panel.core.robinhood_options import auth as robinhood_auth
from investment_panel.core.robinhood_options import (
    RobinhoodMcpClient,
    authorization_server_metadata,
    authorize_robinhood_mcp,
    collect_robinhood_equity_quotes,
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
    max_collection_seconds: int = 900
    max_response_bytes: int = 8 * 1024 * 1024
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
                        "cash_component": None,
                        "settle_on_open": False,
                        "trade_value_multiplier": "100.0000",
                        "underlying_instruments": [{"instrument": "https://provider.test/NVDA"}],
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


class _FailingRobinhoodClient:
    def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]:
        raise TimeoutError("Robinhood MCP request timed out after 30s")

    def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]:
        return {}

    def get_option_instruments(self, **_kwargs) -> dict[str, Any]:
        return {}

    def get_option_quotes(self, instrument_ids: list[str]) -> dict[str, Any]:
        return {}


class _PartiallyInvalidEquityQuoteClient:
    def get_equity_quotes(self, _symbols: list[str]) -> dict[str, Any]:
        return {
            "data": {
                "results": [
                    {"quote": {"symbol": "NVDA", "last_trade_price": "100", "venue_last_trade_time": "2026-08-03T19:59:00Z"}},
                    {"quote": {"symbol": "AMD", "last_trade_price": "100"}},
                    {"quote": {"symbol": "TSLA", "last_trade_price": "0", "venue_last_trade_time": "2026-08-03T19:59:00Z"}},
                ],
            },
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
        "bid_size": 133,
        "ask_size": 98,
        "venue_last_trade_time": "2026-06-12T19:59:59Z",
        "updated_at": "2026-06-12T19:59:59Z",
        "market_data_status": "live",
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
    assert row["bid_size"] == 133
    assert row["ask_size"] == 98
    assert row["last_trade_at"] == "2026-06-12T19:59:59Z"
    assert row["market_data_status"] == "live"
    assert row["contract_symbol"] == "deba9035-f70b-4257-917c-7bbc9ef06097"
    assert row["standard_contract_verified"] is False


def test_option_quote_row_verifies_provider_proven_standard_equity_contract() -> None:
    chain = {
        "id": "chain-nvda", "symbol": "NVDA", "cash_component": None,
        "settle_on_open": False, "trade_value_multiplier": "100.0000",
        "underlying_instruments": [{"instrument": "https://provider.test/NVDA"}],
    }
    instrument = {
        "id": "contract-1", "chain_id": "chain-nvda", "chain_symbol": "NVDA",
        "underlying_type": "equity", "trade_value_multiplier": "100.0000",
        "expiration_date": "2026-09-18", "strike_price": "200", "type": "call",
        "_chain_metadata": chain, "_requested_symbol": "NVDA",
    }

    row = option_quote_row(instrument, {"instrument_id": "contract-1", "bid_price": "2", "ask_price": "2.2"})

    assert row is not None
    assert row["style"] == "american"
    assert row["settlement"] == "physical"
    assert row["deliverable_key"] == "robinhood-chain:chain-nvda"
    assert row["standard_contract_verified"] is True


def test_option_quote_row_rejects_adjusted_or_unproven_deliverable_semantics() -> None:
    chain = {
        "id": "chain-nvda-adjusted", "symbol": "NVDA1", "cash_component": "12.50",
        "settle_on_open": False, "trade_value_multiplier": "100.0000",
        "underlying_instruments": [{"instrument": "https://provider.test/NVDA"}],
    }
    instrument = {
        "id": "adjusted-1", "chain_id": "chain-nvda-adjusted", "chain_symbol": "NVDA1",
        "underlying_type": "equity", "trade_value_multiplier": "100.0000",
        "expiration_date": "2026-09-18", "strike_price": "20", "type": "call",
        "_chain_metadata": chain, "_requested_symbol": "NVDA",
    }

    row = option_quote_row(instrument, {"instrument_id": "adjusted-1", "bid_price": "2", "ask_price": "2.2"})

    assert row is not None
    assert row["style"] is None
    assert row["settlement"] is None
    assert row["standard_contract_verified"] is False


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
    assert all(row["underlying_price"] == 205.14 for row in rows)
    assert result["observed_at"] == "2026-06-12T19:59:59+00:00"
    assert result["collected_at"] != result["observed_at"]


def test_equity_quote_collector_excludes_invalid_provider_rows_from_received_coverage() -> None:
    result = collect_robinhood_equity_quotes(
        _ProviderConfig(), ["NVDA", "AMD", "TSLA"], client=_PartiallyInvalidEquityQuoteClient(),
    )

    assert result["received_symbols"] == ["NVDA"]
    assert [row["symbol"] for row in result["rows"]] == ["NVDA"]
    assert {"AMD:provider_timestamp_missing", "TSLA:non_positive_quote"}.issubset(result["errors"])


def test_equity_quote_collector_can_require_regular_session_facts() -> None:
    result = collect_robinhood_equity_quotes(
        _ProviderConfig(), ["NVDA"], client=_FakeRobinhoodClient(), regular_session_only=True,
    )

    assert result["received_symbols"] == ["NVDA"]
    assert result["rows"][0]["price"] == 205.14
    assert result["rows"][0]["observed_at"] == datetime(2026, 6, 12, 19, 59, 59, tzinfo=UTC)


def test_equity_quote_collector_reports_an_expired_batch_deadline(monkeypatch) -> None:
    calls: list[list[str]] = []

    class ExpiredDeadlineClient:
        def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]:
            calls.append(symbols)
            return {"data": {"results": []}}

    monkeypatch.setattr("investment_panel.core.robinhood_options.collector.time.monotonic", lambda: 10.0)
    result = collect_robinhood_equity_quotes(
        _ProviderConfig(), ["NVDA"], client=ExpiredDeadlineClient(), deadline=9.0,
    )

    assert calls == []
    assert result["received_symbols"] == []
    assert "collector_deadline_exceeded" in result["errors"]


def test_robinhood_mcp_client_rejects_oversized_response(monkeypatch) -> None:
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        request = httpx.Request("POST", "https://example.invalid/mcp")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield b"x" * 2048

    monkeypatch.setattr("investment_panel.core.robinhood_options.collector.httpx.stream", lambda *_args, **_kwargs: FakeStreamResponse())
    client = RobinhoodMcpClient("https://example.invalid/mcp", timeout_seconds=30, max_response_bytes=1024)

    try:
        client._post({"method": "tools/call"}, {})  # noqa: SLF001 - regression test for bounded transport
    except RuntimeError as exc:
        assert "exceeded 1024 bytes" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("oversized response was not rejected")


def test_load_robinhood_access_token_from_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    token_path = tmp_path / "token.json"
    token_path.write_text('{"access_token": "cached-token", "expires_at": 4102444800}', encoding="utf-8")

    token = load_robinhood_access_token(_ProviderConfig(token_path=str(token_path)))

    assert token == "cached-token"


def test_robinhood_token_write_atomically_replaces_private_json(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text('{"access_token": "old"}', encoding="utf-8")
    old_inode = token_path.stat().st_ino

    robinhood_auth._write_token_payload(token_path, {"access_token": "new"})

    assert token_path.stat().st_ino != old_inode
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert token_path.read_text(encoding="utf-8") == '{\n  "access_token": "new"\n}'


def test_robinhood_token_write_preserves_existing_json_when_replace_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    token_path = tmp_path / "token.json"
    original = b'{"access_token": "old"}'
    token_path.write_bytes(original)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(robinhood_auth.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        robinhood_auth._write_token_payload(token_path, {"access_token": "new"})

    assert token_path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [token_path]


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

    metadata = authorization_server_metadata("https://agent.robinhood.com/mcp/trading", timeout=30)

    assert metadata["authorization_endpoint"] == "https://robinhood.com/oauth"
    assert "https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading" in calls
    assert calls[-1] == "https://agent.robinhood.com/.well-known/oauth-authorization-server"


def test_update_robinhood_options_reports_auth_required(tmp_path: Path, monkeypatch, migrated_postgres_dsn: str) -> None:
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    monkeypatch.setattr(update_robinhood_options, "set_source_operational_state", lambda *_args, **_kwargs: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  url: "{migrated_postgres_dsn}"
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


def test_update_robinhood_options_reports_provider_error(tmp_path: Path, monkeypatch, migrated_postgres_dsn: str) -> None:
    monkeypatch.setattr(update_robinhood_options, "set_source_operational_state", lambda *_args, **_kwargs: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  url: "{migrated_postgres_dsn}"
nas:
  status_dir: {tmp_path / "status"}
data_sources:
  brokers:
    enabled: true
    robinhood:
      enabled: true
      readonly: true
""",
        encoding="utf-8",
    )

    result = update_robinhood_options.run(str(config_path), symbols=["NVDA"], client=_FailingRobinhoodClient())

    assert result["status"] == "error"
    assert result["provider"] == "robinhood"
    assert "timed out" in result["error"]
