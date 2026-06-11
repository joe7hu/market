"""Per-contract option features and OI-flow features."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.options_radar.coerce import (_datetime, _integer, _iso, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (CALL_LIKE_OPTION_TYPES)
from investment_panel.core.options_radar.dbutil import (_source_filter, _symbol_filter)
from investment_panel.core.options_radar.indicators import (_convexity_score, _iv_history_by_ticker, _iv_rank, _liquidity_score, _percentile_rank, _required_move_pct, _zscore)

SETTLED_OBSERVATION_MIN_HOURS = 18.0


def refresh_option_features(con: Any, symbols: list[str] | None = None, *, source: str | None = None) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    iv_history = _iv_history_by_ticker(rows)
    count = 0
    for row in rows:
        feature = build_option_feature(row, iv_history.get(str(row.get("ticker") or "").upper(), []))
        if not feature:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO option_features
            (snapshot_time, contract_id, ticker, required_2x_price, required_5x_price,
             required_10x_price, required_move_10x_pct, breakeven, iv_percentile,
             iv_rank, liquidity_score, convexity_score, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                feature["snapshot_time"],
                feature["contract_id"],
                feature["ticker"],
                feature["required_2x_price"],
                feature["required_5x_price"],
                feature["required_10x_price"],
                feature["required_move_10x_pct"],
                feature["breakeven"],
                feature["iv_percentile"],
                feature["iv_rank"],
                feature["liquidity_score"],
                feature["convexity_score"],
                json_dumps(feature["raw"]),
            ],
        )
        count += 1
    return count


def build_option_feature(snapshot: dict[str, Any], iv_history: list[float]) -> dict[str, Any] | None:
    premium = _number(snapshot.get("mid")) or _number(snapshot.get("last"))
    strike = _number(snapshot.get("strike"))
    underlying = _number(snapshot.get("underlying_price"))
    option_type = str(snapshot.get("option_type") or "").lower()
    if premium is None or premium <= 0 or strike is None or option_type not in (CALL_LIKE_OPTION_TYPES | {"put"}):
        return None
    direction = 1 if option_type in CALL_LIKE_OPTION_TYPES else -1
    required_2x = max(0.0, strike + direction * premium * 2)
    required_5x = max(0.0, strike + direction * premium * 5)
    required_10x = max(0.0, strike + direction * premium * 10)
    breakeven = max(0.0, strike + direction * premium)
    required_move = _required_move_pct(option_type, underlying, required_10x)
    liquidity_score = _liquidity_score(
        _number(snapshot.get("spread_pct")),
        _number(snapshot.get("open_interest")),
        _number(snapshot.get("volume")),
    )
    convexity_score = _convexity_score(required_move, _number(snapshot.get("delta")), _integer(snapshot.get("dte")))
    iv = _number(snapshot.get("iv"))
    return {
        "snapshot_time": _iso(snapshot.get("snapshot_time")),
        "contract_id": str(snapshot.get("contract_id")),
        "ticker": _normalize_symbol(snapshot.get("ticker")),
        "required_2x_price": required_2x,
        "required_5x_price": required_5x,
        "required_10x_price": required_10x,
        "required_move_10x_pct": required_move,
        "breakeven": breakeven,
        "iv_percentile": _percentile_rank(iv, iv_history),
        "iv_rank": _iv_rank(iv, iv_history),
        "liquidity_score": liquidity_score,
        "convexity_score": convexity_score,
        "raw": {
            "premium_mid": premium,
            "option_type": option_type,
            "spread_pct": _number(snapshot.get("spread_pct")),
            "open_interest": _number(snapshot.get("open_interest")),
            "volume": _number(snapshot.get("volume")),
        },
    }


def _settled_oi_deltas(history: list[dict[str, Any]]) -> list[float]:
    """Open-interest changes between snapshots at least ~18h apart.

    OI settles overnight, so a >=18h gap keeps one delta per trading day and avoids
    double-counting intraday snapshots — OI deltas are the trustworthy free flow
    signal on delayed feeds (volume is best-effort there)."""

    deltas: list[float] = []
    prev_oi: float | None = None
    prev_t: datetime | None = None
    for snap in history:
        oi = _number(snap.get("open_interest"))
        t = _datetime(snap.get("snapshot_time"))
        if oi is None or t is None:
            continue
        if prev_oi is None:
            prev_oi, prev_t = oi, t
            continue
        if prev_t is not None and (t - prev_t).total_seconds() >= SETTLED_OBSERVATION_MIN_HOURS * 3600:
            deltas.append(oi - prev_oi)
            prev_oi, prev_t = oi, t
    return deltas


def _flow_score(oi_zscore: float | None, volume_oi_ratio: float | None, oi_change_1d: float | None) -> float | None:
    if oi_zscore is None and volume_oi_ratio is None:
        return None
    score = 0.0
    if oi_zscore is not None:
        score += max(0.0, min(60.0, oi_zscore * 20.0))  # +2 sigma OI expansion -> +40
    if volume_oi_ratio is not None and volume_oi_ratio >= 1.0 and (oi_change_1d or 0) > 0:
        score += min(40.0, volume_oi_ratio * 20.0)  # heavy volume into rising OI
    return round(max(0.0, min(100.0, score)), 2)


def build_option_flow_feature(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Flow-anomaly features for the latest snapshot of one contract.

    ``history`` is that contract's snapshots ascending by time. ``flow_score`` is the
    abstraction point a future paid flow feed plugs into without any scoring rewrite.
    """

    history = [snap for snap in history if snap]
    if not history:
        return None
    latest = history[-1]
    current_oi = _number(latest.get("open_interest"))
    current_vol = _number(latest.get("volume"))
    oi_deltas = _settled_oi_deltas(history)
    oi_change_1d = oi_deltas[-1] if oi_deltas else None
    oi_change_5d = sum(oi_deltas[-5:]) if oi_deltas else None
    oi_zscore_20d = _zscore(oi_change_1d, oi_deltas[:-1][-20:]) if oi_change_1d is not None else None
    volume_oi_ratio = (current_vol / current_oi) if current_vol is not None and current_oi and current_oi > 0 else None
    volume_history = [_number(snap.get("volume")) for snap in history[:-1]]
    volume_zscore_20d = _zscore(current_vol, volume_history[-20:]) if current_vol is not None else None
    flow_score = _flow_score(oi_zscore_20d, volume_oi_ratio, oi_change_1d)
    return {
        "snapshot_time": _iso(latest.get("snapshot_time")),
        "contract_id": str(latest.get("contract_id")),
        "ticker": _normalize_symbol(latest.get("ticker")),
        "oi_change_1d": oi_change_1d,
        "oi_change_5d": oi_change_5d,
        "oi_zscore_20d": oi_zscore_20d,
        "volume_oi_ratio": round(volume_oi_ratio, 4) if volume_oi_ratio is not None else None,
        "volume_zscore_20d": volume_zscore_20d,
        "option_type": str(latest.get("option_type") or "").lower(),
        "flow_score": flow_score,
        "raw": {"observations": len(history), "settled_deltas": len(oi_deltas)},
    }


def refresh_option_flow_features(con: Any, symbols: list[str] | None = None, *, source: str | None = None) -> int:
    """Materialize ``option_flow_features`` (OI expansion + volume anomalies) for the
    latest snapshot of every contract, plus per-ticker call-OI aggregates."""

    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT s.snapshot_time, s.ticker, s.contract_id, s.option_type,
               s.open_interest, s.volume, s.mid
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.contract_id, s.snapshot_time
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    by_contract: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_contract[str(row.get("contract_id"))].append(row)

    features = [feature for history in by_contract.values() if (feature := build_option_flow_feature(history))]

    # Per-ticker call-side aggregates: total 1d call-OI expansion and the dollar
    # premium that traded into calls today (a coarse free directional-flow read).
    ticker_call_oi: dict[str, float] = defaultdict(float)
    ticker_call_premium: dict[str, float] = defaultdict(float)
    latest_premium = {str(row.get("contract_id")): _number(row.get("mid")) for row in rows}
    latest_volume = {str(row.get("contract_id")): _number(row.get("volume")) for row in rows}
    for feature in features:
        if feature["option_type"] != "call":
            continue
        if feature["oi_change_1d"]:
            ticker_call_oi[feature["ticker"]] += feature["oi_change_1d"]
        mid = latest_premium.get(feature["contract_id"])
        vol = latest_volume.get(feature["contract_id"])
        if mid and vol:
            ticker_call_premium[feature["ticker"]] += mid * vol * 100.0

    count = 0
    for feature in features:
        con.execute(
            """
            INSERT OR REPLACE INTO option_flow_features
            (snapshot_time, contract_id, ticker, oi_change_1d, oi_change_5d,
             oi_zscore_20d, volume_oi_ratio, volume_zscore_20d,
             ticker_call_oi_delta_1d, ticker_call_volume_premium_usd, flow_score, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                feature["snapshot_time"],
                feature["contract_id"],
                feature["ticker"],
                feature["oi_change_1d"],
                feature["oi_change_5d"],
                feature["oi_zscore_20d"],
                feature["volume_oi_ratio"],
                feature["volume_zscore_20d"],
                ticker_call_oi.get(feature["ticker"]),
                ticker_call_premium.get(feature["ticker"]),
                feature["flow_score"],
                json_dumps(feature["raw"]),
            ],
        )
        count += 1
    return count
