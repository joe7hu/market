"""Price-history technical computations."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import db, init_db, query_rows

from investment_panel.core.panel.coerce import _number_from_any, decode_fields
from investment_panel.core.panel.disclosures import _compact_empty_fields



def technicals(
    con: Any,
    symbols: list[str] | set[str] | tuple[str, ...] | None = None,
    price_history: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    normalized_symbols = sorted({str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    where_clause = ""
    params: list[Any] = []
    if normalized_symbols:
        where_clause = f"WHERE upper(symbol) IN ({', '.join(['?'] * len(normalized_symbols))})"
        params.extend(normalized_symbols)
    rows = query_rows(
        con,
        f"""
        SELECT symbol, date, features
        FROM technical_features
        {where_clause}
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        ORDER BY date DESC, symbol
        LIMIT 1000
        """,
        params,
    )
    decoded = [decode_fields(row, ("features",)) for row in rows]
    if price_history is None:
        price_history = technical_price_history(con, [str(row.get("symbol") or "").upper() for row in decoded], days=253)
    for row in decoded:
        symbol = str(row.get("symbol") or "").upper()
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        history = price_history.get(symbol) or []
        row["close"] = features.get("close")
        row["ma20"] = features.get("ma20")
        row["ma50"] = features.get("ma50")
        row["ma200"] = features.get("ma200")
        row["return_20d"] = features.get("return_20d")
        row["return_60d"] = features.get("return_60d")
        row["return_3m"] = features.get("return_3m") if features.get("return_3m") is not None else trailing_return(history, days=63)
        row["return_ytd"] = features.get("return_ytd") if features.get("return_ytd") is not None else period_return(history, "ytd")
        row["return_1y"] = features.get("return_1y") if features.get("return_1y") is not None else period_return(history, "1y")
        row["technical_score"] = features.get("technical_score")
        row["drawdown_from_high"] = features.get("drawdown_from_high")
        row["range_recovery"] = features.get("range_recovery")
        row["volume_ratio_20_60"] = features.get("volume_ratio_20_60")
        row["rel_volume_1m"] = features.get("rel_volume_1m") if features.get("rel_volume_1m") is not None else relative_volume(history, recent_days=22, baseline_days=63)
        row["volume_bars_1m"] = one_month_volume_bar_points(history)
        row["atr_pct_1m"] = features.get("atr_pct_1m") if features.get("atr_pct_1m") is not None else average_true_range_pct(history, days=22)
        row["atr_pct_1m_points"] = true_range_pct_points(history, days=22)
        row["chart_1y"] = sampled_price_points(history, max_points=253)
        row["rs_1m_bars"] = one_month_bar_points(history)
        row["rs_3m_bars"] = period_bar_points(history, days=63)
        row["source"] = features.get("source") or features.get("price_source")
    return [_compact_empty_fields(row) for row in decoded]




def technical_price_history(con: Any, symbols: list[str], days: int = 253) -> dict[str, list[dict[str, Any]]]:
    normalized = sorted({symbol for symbol in symbols if symbol})
    if not normalized:
        return {}
    placeholders = ", ".join(["?"] * len(normalized))
    history_rows = query_rows(
        con,
        f"""
        SELECT symbol, date, high, low, close, volume
        FROM (
            SELECT symbol, date, high, low, close, volume,
                   row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS recency_rank
            FROM prices_daily
            WHERE symbol IN ({placeholders})
        )
        WHERE recency_rank <= {int(days)}
        ORDER BY symbol, date
        """,
        normalized,
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in history_rows:
        symbol = str(row.get("symbol") or "").upper()
        close = row.get("close")
        if not symbol or close is None:
            continue
        by_symbol.setdefault(symbol, []).append(
            {
                "date": str(row.get("date") or ""),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": close,
                "volume": row.get("volume"),
            }
        )
    return by_symbol




def sampled_price_points(history: list[dict[str, Any]], max_points: int = 253) -> list[float] | None:
    closes = [_number_from_any(point.get("close")) for point in history]
    closes = [close for close in closes if close]
    if len(closes) < 2:
        return None
    if len(closes) <= max_points:
        return [round(close, 4) for close in closes]
    last_index = len(closes) - 1
    sampled: list[float] = []
    for index in range(max_points):
        source_index = round((index / (max_points - 1)) * last_index)
        sampled.append(round(closes[source_index], 4))
    return sampled




def one_month_bar_points(history: list[dict[str, Any]]) -> list[float] | None:
    return period_bar_points(history, days=22)




def period_bar_points(history: list[dict[str, Any]], days: int) -> list[float] | None:
    closes = [_number_from_any(point.get("close")) for point in history[-days:]]
    closes = [close for close in closes if close]
    if len(closes) < 2:
        return None
    low = min(closes)
    high = max(closes)
    spread = high - low or 1
    return [round(((close - low) / spread) * 100, 2) for close in closes]




def one_month_volume_bar_points(history: list[dict[str, Any]]) -> list[float] | None:
    volumes = [_number_from_any(point.get("volume")) for point in history[-22:]]
    volumes = [volume for volume in volumes if volume and volume > 0]
    if len(volumes) < 2:
        return None
    peak = max(volumes) or 1
    return [round((volume / peak) * 100, 2) for volume in volumes]




def trailing_return(history: list[dict[str, Any]], days: int) -> float | None:
    points = [point for point in history[-days:] if point.get("close") not in (None, 0)]
    if len(points) < 2:
        return None
    start = _number_from_any(points[0].get("close"))
    end = _number_from_any(points[-1].get("close"))
    if not start or not end:
        return None
    return (end / start) - 1




def relative_volume(history: list[dict[str, Any]], recent_days: int, baseline_days: int) -> float | None:
    volumes = [_number_from_any(point.get("volume")) for point in history if point.get("volume") not in (None, 0)]
    volumes = [volume for volume in volumes if volume and volume > 0]
    if len(volumes) < recent_days + 1:
        return None
    recent = volumes[-recent_days:]
    baseline = volumes[-(recent_days + baseline_days) : -recent_days] or volumes[:-recent_days]
    recent_avg = sum(recent) / len(recent)
    baseline_avg = sum(baseline) / len(baseline) if baseline else None
    if not baseline_avg:
        return None
    return recent_avg / baseline_avg




def true_range_pct_points(history: list[dict[str, Any]], days: int) -> list[float] | None:
    points = history[-(days + 1) :]
    values: list[float] = []
    previous_close: float | None = None
    for point in points:
        high = _number_from_any(point.get("high"))
        low = _number_from_any(point.get("low"))
        close = _number_from_any(point.get("close"))
        if not high or not low or not close:
            previous_close = close
            continue
        ranges = [high - low]
        if previous_close:
            ranges.extend([abs(high - previous_close), abs(low - previous_close)])
        true_range = max(ranges)
        values.append(true_range / close)
        previous_close = close
    values = values[-days:]
    return [round(value, 4) for value in values] if len(values) >= 2 else None




def average_true_range_pct(history: list[dict[str, Any]], days: int) -> float | None:
    values = true_range_pct_points(history, days)
    if not values:
        return None
    return sum(values) / len(values)




def period_return(history: list[dict[str, Any]], period: str) -> float | None:
    points = [point for point in history if point.get("close") not in (None, 0)]
    if len(points) < 2:
        return None
    last = points[-1]
    last_close = _number_from_any(last.get("close"))
    if not last_close:
        return None
    if period == "ytd":
        year = str(last.get("date") or "")[:4]
        start = next((point for point in points if str(point.get("date") or "").startswith(year)), points[0])
    else:
        start = points[0]
    start_close = _number_from_any(start.get("close"))
    if not start_close:
        return None
    return (last_close / start_close) - 1
