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

    def market_metrics(self, symbol: str) -> dict[str, Any]:
        ticker = self.ticker(symbol)
        info = dict(ticker.info or {})
        try:
            roic = return_on_invested_capital(ticker.income_stmt, ticker.balance_sheet)
        except Exception:
            roic = None
        if roic is not None:
            info["returnOnInvestedCapital"] = roic
        return info

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

    def sec_filings(self, symbol: str) -> Any:
        getter = getattr(self.ticker(symbol), "get_sec_filings", None)
        return getter() if callable(getter) else {}

    def options_expiries(self, symbol: str) -> list[dict[str, Any]]:
        ticker = self.ticker(symbol)
        expiries = getattr(ticker, "options", None) or []
        return [{"expiry": str(expiry)} for expiry in expiries if expiry]

    def options_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        chain = self.ticker(symbol).option_chain(expiry)
        rows: list[dict[str, Any]] = []
        for option_type, frame in (("call", getattr(chain, "calls", None)), ("put", getattr(chain, "puts", None))):
            for row in frame_to_records(frame):
                bid = number_or_none(row.get("bid"))
                ask = number_or_none(row.get("ask"))
                last = number_or_none(row.get("lastPrice"))
                mid = ((bid + ask) / 2) if bid is not None and ask is not None and ask >= bid else last
                contract_symbol = row.get("contractSymbol")
                rows.append(
                    {
                        "expiry": expiry,
                        "type": option_type,
                        "strike": row.get("strike"),
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "last": last,
                        "iv": row.get("impliedVolatility"),
                        "volume": row.get("volume"),
                        "open_interest": row.get("openInterest"),
                        "openInterest": row.get("openInterest"),
                        "symbol": contract_symbol,
                        "contract_symbol": contract_symbol,
                        "raw": row,
                    }
                )
        return rows

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


def number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def return_on_invested_capital(income_statement: Any, balance_sheet: Any) -> float | None:
    operating_income = latest_statement_value(income_statement, "Operating Income")
    invested_capital = statement_values(balance_sheet, "Invested Capital")[:2]
    if operating_income is None or not invested_capital:
        return None
    denominator = sum(invested_capital) / len(invested_capital)
    if denominator <= 0:
        return None
    tax_rate = latest_statement_value(income_statement, "Tax Rate For Calcs")
    if tax_rate is None:
        tax_provision = latest_statement_value(income_statement, "Tax Provision")
        pretax_income = latest_statement_value(income_statement, "Pretax Income")
        tax_rate = tax_provision / pretax_income if tax_provision is not None and pretax_income not in (None, 0) else 0.21
    bounded_tax_rate = min(0.5, max(0.0, tax_rate))
    return operating_income * (1.0 - bounded_tax_rate) / denominator


def latest_statement_value(statement: Any, row_name: str) -> float | None:
    values = statement_values(statement, row_name)
    return values[0] if values else None


def statement_values(statement: Any, row_name: str) -> list[float]:
    if statement is None or not hasattr(statement, "index") or row_name not in statement.index:
        return []
    series = statement.loc[row_name]
    raw_values = list(series.values) if hasattr(series, "values") else [series]
    return [value for item in raw_values if (value := number_or_none(item)) is not None]


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
