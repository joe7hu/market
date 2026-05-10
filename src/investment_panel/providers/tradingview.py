"""TradingView provider backed by the local OpenCLI plugin."""

from __future__ import annotations

from typing import Any, Protocol

from investment_panel.providers.opencli import ensure_list


class JsonRunner(Protocol):
    def read_json(self, args: list[str]) -> Any: ...


class TradingViewProvider:
    """Read-only TradingView data adapter.

    The adapter intentionally exposes domain operations rather than arbitrary
    OpenCLI command construction. That keeps provider details local and makes
    tests target the Market provider interface.
    """

    def __init__(self, runner: JsonRunner):
        self.runner = runner

    def status(self) -> list[dict[str, Any]]:
        return ensure_list(self.runner.read_json(["tradingview", "status"]))

    def quote(self, symbol: str) -> dict[str, Any] | None:
        ticker, exchange = split_tradingview_symbol(symbol)
        args = ["tradingview", "quote", "--ticker", ticker]
        if exchange:
            args.extend(["--exchange", exchange])
        rows = ensure_list(self.runner.read_json(args))
        return rows[0] if rows else None

    def options_expiries(self, symbol: str) -> list[dict[str, Any]]:
        ticker, exchange = split_tradingview_symbol(symbol)
        args = ["tradingview", "options-expiries", "--ticker", ticker]
        if exchange:
            args.extend(["--exchange", exchange])
        return ensure_list(self.runner.read_json(args))

    def options_chain(self, symbol: str, expiry: str | None = None, strikes_around_spot: int = 6) -> list[dict[str, Any]]:
        ticker, exchange = split_tradingview_symbol(symbol)
        args = ["tradingview", "options-chain", "--ticker", ticker, "--strikes-around-spot", str(strikes_around_spot)]
        if exchange:
            args.extend(["--exchange", exchange])
        if expiry:
            args.extend(["--expiry", expiry])
        return ensure_list(self.runner.read_json(args))

    def screener(self, market: str = "america", limit: int = 50) -> list[dict[str, Any]]:
        return ensure_list(
            self.runner.read_json(
                [
                    "tradingview",
                    "screener",
                    "--market",
                    market,
                    "--columns",
                    "name,close,change,volume,market_cap_basic,sector",
                    "--limit",
                    str(limit),
                ]
            )
        )

    def news(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        args = ["tradingview", "news", "--limit", str(limit)]
        if symbol:
            args.extend(["--symbol", symbol])
        return ensure_list(self.runner.read_json(args))


def split_tradingview_symbol(symbol: str) -> tuple[str, str | None]:
    value = symbol.upper()
    if ":" not in value:
        return value, None
    exchange, ticker = value.split(":", 1)
    return ticker, exchange
