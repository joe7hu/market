"""High-signal compaction for the daily research prompt."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def compact_symbol_rows(
    tables: dict[str, list[dict[str, Any]]],
    symbols: set[str],
    *,
    positions: list[dict[str, Any]],
    watchlist: list[dict[str, Any]],
    radar_symbols: set[str],
) -> list[dict[str, Any]]:
    """Return one decision-grade row per supplied symbol."""

    position_by_symbol = {_symbol(row): row for row in positions}
    watch_by_symbol = {_symbol(row): row for row in watchlist}
    latest = {
        name: _latest_by_symbol(tables.get(name, []))
        for name in (
            "quotes",
            "fundamentals",
            "technicals",
            "analyst_estimates",
            "options_ticker_signals",
            "thesis_monitor",
        )
    }
    output: list[dict[str, Any]] = []
    for symbol in sorted(symbols):
        position = position_by_symbol.get(symbol, {})
        watched = watch_by_symbol.get(symbol, {})
        roles = [
            role
            for role, active in (
                ("holding", bool(position)),
                ("watchlist", symbol in watch_by_symbol and not position),
                ("options_radar", symbol in radar_symbols),
            )
            if active
        ]
        row = {
            "symbol": symbol,
            "role": "+".join(roles),
            "position": _pick(
                position,
                (
                    "quantity",
                    "average_cost",
                    "market_value",
                    "portfolio_weight",
                    "unrealized_pnl_pct",
                    "price",
                    "quote_observed_at",
                    "quote_source",
                    "valuation_status",
                ),
                aliases={
                    "portfolio_weight": "weight",
                    "unrealized_pnl_pct": "pnl_pct",
                    "quote_observed_at": "quote_at",
                    "quote_source": "quote_src",
                    "valuation_status": "valuation",
                },
            ),
            "watch_note": _trim(watched.get("notes"), 100),
            "quote": _quote(latest["quotes"].get(symbol, {})),
            "fundamentals": _fundamentals(latest["fundamentals"].get(symbol, {})),
            "technicals": _technicals(latest["technicals"].get(symbol, {})),
            "next_estimate": _estimate(latest["analyst_estimates"].get(symbol, {})),
            "options": _options(latest["options_ticker_signals"].get(symbol, {})),
            "thesis": _thesis(latest["thesis_monitor"].get(symbol, {})),
        }
        output.append(_clean(row))
    return output


def _latest_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = _symbol(row)
        if not symbol:
            continue
        current = output.get(symbol)
        if current is None or _timestamp(row) > _timestamp(current):
            output[symbol] = row
    return output


def _quote(row: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        row,
        ("price", "change_pct", "observed_at", "source"),
        aliases={"change_pct": "day_pct", "observed_at": "at"},
    )


def _fundamentals(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    selected = _pick(
        values,
        (
            "sector",
            "revenue_growth",
            "profit_margin",
            "fcf_yield",
            "forward_pe",
            "return_on_invested_capital",
        ),
        aliases={
            "revenue_growth": "rev_growth",
            "profit_margin": "margin",
            "return_on_invested_capital": "roic",
        },
    )
    market_cap = values.get("market_cap")
    if isinstance(market_cap, (int, float)):
        selected["market_cap_b"] = round(market_cap / 1_000_000_000, 1)
    return selected


def _technicals(row: dict[str, Any]) -> dict[str, Any]:
    close = row.get("price", row.get("close"))
    return _clean(
        {
            "at": row.get("as_of") or row.get("observed_at"),
            "ret20": _number(row.get("return_20d")),
            "ret60": _number(row.get("return_60d")),
            "above20": _above(close, row.get("sma_20")),
            "above50": _above(close, row.get("sma_50")),
            "above200": _above(close, row.get("sma_200")),
        }
    )


def _estimate(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    targets = (
        values.get("analyst_price_targets")
        if isinstance(values.get("analyst_price_targets"), dict)
        else {}
    )
    earnings = _first_dict(values.get("earnings_estimate"))
    revenue = _first_dict(values.get("revenue_estimate"))
    revisions = _first_dict(values.get("eps_revisions"))
    return _clean(
        {
            "target_mean": targets.get("mean"),
            "target_range": _range(targets.get("low"), targets.get("high")),
            "eps_avg": earnings.get("avg"),
            "eps_growth": earnings.get("growth"),
            "revenue_growth": revenue.get("growth"),
            "eps_up_30d": revisions.get("upLast30days"),
        }
    )


def _options(row: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        row,
        (
            "nearest_expiry",
            "atm_iv",
            "expected_move_pct",
            "spread_quality",
            "liquidity_score",
            "as_of",
        ),
        aliases={
            "nearest_expiry": "expiry",
            "expected_move_pct": "expected_move_pct",
            "as_of": "at",
        },
    )


def _thesis(row: dict[str, Any]) -> dict[str, Any]:
    thesis = str(row.get("thesis") or row.get("why_owned_watched") or "").strip()
    invalidation = str(row.get("invalidation") or "").strip()
    missing: list[str] = []
    if thesis.lower().startswith("no structured thesis"):
        thesis = ""
        missing.append("thesis")
    if invalidation.lower().startswith("no invalidation"):
        invalidation = ""
        missing.append("invalidation")
    return _clean(
        {
            "status": row.get("status"),
            "thesis": _trim(thesis, 160),
            "invalidation": _trim(invalidation, 120),
            "missing": missing,
            "needs_review": row.get("needs_review"),
        }
    )


def _pick(
    row: dict[str, Any],
    keys: tuple[str, ...],
    *,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    aliases = aliases or {}
    return _clean({aliases.get(key, key): _number(row.get(key)) for key in keys})


def _first_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        return {}
    return next((item for item in value if isinstance(item, dict)), {})


def _clean(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return round(float(value), 4)
    return round(value, 4) if isinstance(value, float) else value


def _above(left: Any, right: Any) -> bool | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return left >= right


def _range(low: Any, high: Any) -> list[Any]:
    return [_number(low), _number(high)] if low is not None and high is not None else []


def _trim(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _symbol(row: dict[str, Any]) -> str:
    return (
        str(row.get("symbol") or row.get("ticker") or row.get("underlying") or "")
        .strip()
        .upper()
    )


def _timestamp(row: dict[str, Any]) -> datetime:
    for key in ("observed_at", "as_of", "updated_at", "generated_at", "created_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            continue
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    return datetime.min.replace(tzinfo=UTC)
