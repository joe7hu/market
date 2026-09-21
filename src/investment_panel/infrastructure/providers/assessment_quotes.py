"""Public reference-price adapters. These are never execution quotes."""
from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any

import httpx

from investment_panel.core.prices import YAHOO_SYMBOL_ALIASES
from investment_panel.domain.decision import assessment_quote, assessment_timestamp as timestamp


def fetch_assessment_quote(symbol: str, asset_class: str) -> dict[str, Any]:
    continuous = asset_class == "crypto"
    errors: list[str] = []
    with httpx.Client(timeout=12.0, headers={"User-Agent": "joehu-market-panel/0.1"}) as client:
        if continuous:
            try:
                product = YAHOO_SYMBOL_ALIASES.get(symbol, symbol)
                response = client.get(f"https://api.exchange.coinbase.com/products/{product}/ticker")
                response.raise_for_status()
                payload = response.json()
                return validated_quote(symbol, asset_class, payload.get("price"), payload.get("time"), "coinbase-exchange-ticker")
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                errors.append(f"Coinbase: {type(exc).__name__}: {exc}")
        try:
            provider_symbol = YAHOO_SYMBOL_ALIASES.get(symbol, symbol)
            response = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}",
                                  params={"range": "1d", "interval": "1m"})
            response.raise_for_status()
            result = (response.json().get("chart", {}).get("result") or [None])[0]
            if not result:
                raise ValueError("No chart metadata")
            metadata = result.get("meta") or {}
            observed = datetime.fromtimestamp(float(metadata["regularMarketTime"]), UTC)
            return validated_quote(symbol, asset_class, metadata.get("regularMarketPrice"), observed, "yahoo-chart-reference")
        except (httpx.HTTPError, KeyError, ValueError, TypeError, OverflowError) as exc:
            errors.append(f"Yahoo: {type(exc).__name__}: {exc}")
    raise ValueError("; ".join(errors))


def validated_quote(symbol: str, asset_class: str, price: Any, observed: Any, provider: str,
                    *, now: datetime | None = None) -> dict[str, Any]:
    checked = now or datetime.now(UTC)
    if isinstance(price, bool):
        raise ValueError("Boolean provider price")
    value = float(price)
    if not isfinite(value) or value <= 0:
        raise ValueError("Invalid provider price")
    row = {"symbol": symbol, "asset_class": asset_class, "price": value,
           "observed_at": timestamp(observed), "available_at": checked,
           "provider": provider, "execution_eligible": False}
    quality = assessment_quote(row, now=checked, continuous=asset_class == "crypto")
    if not quality.usable:
        raise ValueError(quality.reason)
    return row


__all__ = ["fetch_assessment_quote", "validated_quote"]
