"""Optional yfinance enrichment provider."""

from __future__ import annotations

from typing import Any


class YFinanceUnavailable(RuntimeError):
    """Raised when yfinance is not installed or cannot be imported."""


class YFinanceProvider:
    def __init__(self) -> None:
        try:
            import yfinance as yf
        except ModuleNotFoundError as exc:
            raise YFinanceUnavailable("Install the `market-data` extra or add yfinance to use this provider.") from exc
        self.yf = yf

    def ticker(self, symbol: str) -> Any:
        return self.yf.Ticker(symbol)

    def info(self, symbol: str) -> dict[str, Any]:
        return dict(self.ticker(symbol).info or {})

    def estimates(self, symbol: str) -> dict[str, Any]:
        ticker = self.ticker(symbol)
        return {
            "earnings_estimate": frame_to_records(getattr(ticker, "earnings_estimate", None)),
            "revenue_estimate": frame_to_records(getattr(ticker, "revenue_estimate", None)),
            "eps_trend": frame_to_records(getattr(ticker, "eps_trend", None)),
            "eps_revisions": frame_to_records(getattr(ticker, "eps_revisions", None)),
            "growth_estimates": frame_to_records(getattr(ticker, "growth_estimates", None)),
            "analyst_price_targets": scalar_or_records(getattr(ticker, "analyst_price_targets", None)),
        }

    def earnings_events(self, symbol: str) -> dict[str, Any]:
        ticker = self.ticker(symbol)
        return {
            "calendar": scalar_or_records(getattr(ticker, "calendar", None)),
            "earnings_history": frame_to_records(getattr(ticker, "earnings_history", None)),
        }

    def options_chain_liquidity(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        chain = self.ticker(symbol).option_chain(expiry)
        rows: list[dict[str, Any]] = []
        for option_type, frame in (("call", getattr(chain, "calls", None)), ("put", getattr(chain, "puts", None))):
            for row in frame_to_records(frame):
                rows.append(
                    {
                        "expiry": expiry,
                        "type": option_type,
                        "strike": row.get("strike"),
                        "volume": row.get("volume"),
                        "open_interest": row.get("openInterest"),
                        "openInterest": row.get("openInterest"),
                        "last": row.get("lastPrice"),
                        "contract_symbol": row.get("contractSymbol"),
                        "raw": row,
                    }
                )
        return rows


def frame_to_records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "empty") and value.empty:
        return []
    if hasattr(value, "reset_index"):
        frame = value.reset_index()
        frame.columns = [str(column) for column in frame.columns]
        return [
            {key: json_safe(item) for key, item in row.items()}
            for row in frame.to_dict(orient="records")
        ]
    return []


def scalar_or_records(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if hasattr(value, "to_dict"):
        try:
            return {str(key): json_safe(item) for key, item in value.to_dict().items()}
        except Exception:
            pass
    return json_safe(value)


def json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value
