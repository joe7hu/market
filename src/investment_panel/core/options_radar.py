"""Deterministic 10x options radar data flywheel."""

from __future__ import annotations

import json
from datetime import date, datetime
from statistics import mean
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id


DEFAULT_STRATEGY_VERSION = "leap_10x_reversal_v1"

DEFAULT_STRATEGY_PARAMETERS: dict[str, Any] = {
    "strategy_name": "leap_10x_reversal",
    "version": 1,
    "option_type": "call",
    "delta_min": 0.20,
    "delta_max": 0.45,
    "dte_min": 365,
    "dte_max": 900,
    "max_spread_pct": 0.25,
    "reject_spread_pct": 0.40,
    "min_open_interest": 100,
    "min_volume": 1,
    "max_required_move_pct": 3.50,
    "max_iv_percentile": 70.0,
    "reject_iv_percentile": 85.0,
    "require_price_above_ma50": True,
    "require_rs_improving": True,
    "fill_slippage_pct": 0.03,
}


def refresh_options_radar(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str = "tradingview",
    snapshot_time: str | None = None,
) -> dict[str, int]:
    """Refresh the deterministic radar tables from already-ingested market data."""

    register_default_strategy(con, strategy_version)
    snapshot_rows = persist_option_snapshots(con, symbols=symbols, source=source, snapshot_time=snapshot_time)
    feature_rows = refresh_option_features(con, symbols=symbols, source=source)
    stock_rows = refresh_stock_features_for_option_snapshots(con, symbols=symbols, source=source)
    candidate_rows = generate_candidate_events(con, symbols=symbols, strategy_version=strategy_version, source=source)
    from investment_panel.core.option_agent_thesis import refresh_option_agent_work

    agent_work = refresh_option_agent_work(con, strategy_version=strategy_version)
    shadow_rows = create_shadow_trades(con, strategy_version=strategy_version)
    marked_rows = mark_shadow_trades(con)
    attribution_rows = refresh_option_attributions(con, strategy_version=strategy_version)
    missed_rows = detect_missed_winners(con, symbols=symbols, strategy_version=strategy_version, source=source)
    proposal_rows = generate_strategy_mutation_proposals(con, strategy_version=strategy_version)
    return {
        "option_snapshots": snapshot_rows,
        "option_features": feature_rows,
        "stock_features": stock_rows,
        "candidate_events": candidate_rows,
        **agent_work,
        "shadow_trades": shadow_rows,
        "shadow_trades_marked": marked_rows,
        "option_attributions": attribution_rows,
        "missed_winners": missed_rows,
        "strategy_mutation_proposals": proposal_rows,
    }


def register_default_strategy(con: Any, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> None:
    now = datetime.utcnow().isoformat()
    con.execute(
        """
        INSERT OR IGNORE INTO option_strategy_versions
        (strategy_version, strategy_name, version, created_at, status, parameters, promoted_at, supersedes, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            strategy_version,
            DEFAULT_STRATEGY_PARAMETERS["strategy_name"],
            DEFAULT_STRATEGY_PARAMETERS["version"],
            now,
            "shadow",
            json_dumps(DEFAULT_STRATEGY_PARAMETERS),
            None,
            None,
            "Deterministic 10x LEAP reversal baseline. Agents may propose changes, but code/backtests promote versions.",
        ],
    )


def persist_option_snapshots(
    con: Any,
    symbols: list[str] | None = None,
    *,
    source: str = "tradingview",
    snapshot_time: str | None = None,
) -> int:
    """Copy raw chain rows into the event-sourced radar snapshot table."""

    symbol_filter = _symbol_filter(symbols, table_alias="oc")
    observed_filter = "AND oc.observed_at = TRY_CAST(? AS TIMESTAMP)" if snapshot_time else ""
    params: list[Any] = [source, *symbol_filter["params"]]
    if snapshot_time:
        params.append(snapshot_time)
    rows = query_rows(
        con,
        f"""
        SELECT
            oc.symbol,
            oc.expiry,
            oc.strike,
            oc.option_type,
            oc.bid,
            oc.ask,
            oc.mid,
            oc.iv,
            oc.delta,
            oc.gamma,
            oc.theta,
            oc.vega,
            oc.contract_symbol,
            oc.observed_at,
            oc.source,
            oc.raw,
            COALESCE(
                (
                    SELECT q.price
                    FROM quotes_intraday q
                    WHERE q.symbol = oc.symbol AND q.observed_at <= oc.observed_at
                    ORDER BY q.observed_at DESC
                    LIMIT 1
                ),
                (
                    SELECT q.price
                    FROM quotes_intraday q
                    WHERE q.symbol = oc.symbol
                    ORDER BY q.observed_at DESC
                    LIMIT 1
                )
            ) AS underlying_price
        FROM options_chain oc
        WHERE oc.source = ? {symbol_filter["sql"]} {observed_filter}
        ORDER BY oc.observed_at, oc.symbol, oc.expiry, oc.strike, oc.option_type
        """,
        params,
    )
    count = 0
    for row in rows:
        raw = _json(row.get("raw"))
        ticker = _normalize_symbol(row.get("symbol"))
        snapshot_at = _iso(row.get("observed_at"))
        expiration = row.get("expiry")
        strike = _number(row.get("strike"))
        option_type = str(row.get("option_type") or raw.get("type") or "").lower()
        mid = _premium_mid(row, raw)
        bid = _number(row.get("bid"))
        ask = _number(row.get("ask"))
        contract_id = _contract_id(ticker, expiration, strike, option_type, row.get("contract_symbol") or raw.get("symbol"))
        con.execute(
            """
            INSERT OR REPLACE INTO option_snapshot
            (snapshot_time, ticker, underlying_price, expiration, strike, option_type, bid, ask, mid,
             last, volume, open_interest, iv, delta, gamma, theta, vega, dte, spread_pct,
             data_source, contract_id, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_at,
                ticker,
                _number(row.get("underlying_price")),
                expiration,
                strike,
                option_type,
                bid,
                ask,
                mid,
                _coalesce_number(raw, "last", "last_price", "close"),
                _coalesce_number(raw, "volume", "vol"),
                _coalesce_number(raw, "open_interest", "openInterest", "oi"),
                _number(row.get("iv")),
                _number(row.get("delta")),
                _number(row.get("gamma")),
                _number(row.get("theta")),
                _number(row.get("vega")),
                _integer(raw.get("dte")) or _days_to_expiration(expiration, snapshot_at),
                _spread_pct(bid, ask, mid),
                source,
                contract_id,
                json_dumps(raw),
            ],
        )
        count += 1
    return count


def refresh_option_features(con: Any, symbols: list[str] | None = None, *, source: str = "tradingview") -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE s.data_source = ? {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
        [source, *symbol_filter["params"]],
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
    if premium is None or premium <= 0 or strike is None or option_type not in {"call", "put"}:
        return None
    direction = 1 if option_type == "call" else -1
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


def refresh_stock_features_for_option_snapshots(con: Any, symbols: list[str] | None = None, *, source: str = "tradingview") -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    rows = query_rows(
        con,
        f"""
        SELECT DISTINCT s.ticker, s.snapshot_time
        FROM option_snapshot s
        WHERE s.data_source = ? {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker
        """,
        [source, *symbol_filter["params"]],
    )
    count = 0
    for row in rows:
        if compute_stock_feature(con, str(row["ticker"]), _iso(row["snapshot_time"])):
            count += 1
    return count


def compute_stock_feature(con: Any, ticker: str, snapshot_time: str) -> dict[str, Any] | None:
    ticker = _normalize_symbol(ticker)
    prices = query_rows(
        con,
        """
        SELECT date, open, high, low, close, volume
        FROM prices_daily
        WHERE symbol = ? AND date <= TRY_CAST(? AS DATE)
        ORDER BY date
        """,
        [ticker, snapshot_time],
    )
    if not prices:
        return None
    qqq_prices = query_rows(
        con,
        """
        SELECT date, close
        FROM prices_daily
        WHERE symbol = 'QQQ' AND date <= TRY_CAST(? AS DATE)
        ORDER BY date
        """,
        [snapshot_time],
    )
    closes = [_number(row.get("close")) for row in prices]
    highs = [_number(row.get("high")) for row in prices]
    lows = [_number(row.get("low")) for row in prices]
    volumes = [_number(row.get("volume")) for row in prices]
    close_values = [value for value in closes if value is not None]
    if not close_values:
        return None
    price = close_values[-1]
    high_values = [value for value in highs if value is not None]
    high_252 = max(high_values[-252:]) if high_values else price
    feature = {
        "snapshot_time": snapshot_time,
        "ticker": ticker,
        "price": price,
        "ma_20": _average(close_values[-20:]) if len(close_values) >= 20 else None,
        "ma_50": _average(close_values[-50:]) if len(close_values) >= 50 else None,
        "ma_200": _average(close_values[-200:]) if len(close_values) >= 200 else None,
        "rs_vs_qqq_20d": _relative_strength(close_values, [_number(row.get("close")) for row in qqq_prices], 20),
        "rs_vs_qqq_60d": _relative_strength(close_values, [_number(row.get("close")) for row in qqq_prices], 60),
        "atr_pct": _atr_pct(prices),
        "volume_ratio": _volume_ratio([value for value in volumes if value is not None]),
        "distance_from_52w_high": (price / high_252 - 1) if high_252 else None,
        "base_length_days": _base_length_days(close_values, high_252),
        "breakout_level": max(high_values[-60:-1]) if len(high_values) > 1 else high_252,
        "raw": {
            "price_rows": len(prices),
            "qqq_rows": len(qqq_prices),
            "source": "prices_daily",
        },
    }
    con.execute(
        """
        INSERT OR REPLACE INTO stock_features
        (snapshot_time, ticker, price, ma_20, ma_50, ma_200, rs_vs_qqq_20d,
         rs_vs_qqq_60d, atr_pct, volume_ratio, distance_from_52w_high,
         base_length_days, breakout_level, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            feature["snapshot_time"],
            feature["ticker"],
            feature["price"],
            feature["ma_20"],
            feature["ma_50"],
            feature["ma_200"],
            feature["rs_vs_qqq_20d"],
            feature["rs_vs_qqq_60d"],
            feature["atr_pct"],
            feature["volume_ratio"],
            feature["distance_from_52w_high"],
            feature["base_length_days"],
            feature["breakout_level"],
            json_dumps(feature["raw"]),
        ],
    )
    return feature


def generate_candidate_events(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str = "tradingview",
) -> int:
    strategy = _strategy_parameters(con, strategy_version)
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    rows = query_rows(
        con,
        f"""
        SELECT
            s.*,
            f.required_2x_price,
            f.required_5x_price,
            f.required_10x_price,
            f.required_move_10x_pct,
            f.breakeven,
            f.iv_percentile,
            f.iv_rank,
            f.liquidity_score,
            f.convexity_score,
            sf.price,
            sf.ma_50,
            sf.rs_vs_qqq_20d,
            sf.base_length_days,
            sf.breakout_level,
            t.thesis_id
        FROM option_snapshot s
        JOIN option_features f ON f.contract_id = s.contract_id AND f.snapshot_time = s.snapshot_time
        LEFT JOIN stock_features sf ON sf.ticker = s.ticker AND sf.snapshot_time = s.snapshot_time
        LEFT JOIN (
            SELECT ticker, thesis_id
            FROM agent_thesis
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        ) t ON t.ticker = s.ticker
        WHERE s.data_source = ? {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
        [source, *symbol_filter["params"]],
    )
    count = 0
    for row in rows:
        event = build_candidate_event(row, strategy_version, strategy)
        if not event:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO candidate_event
            (event_id, snapshot_time, ticker, contract_id, strategy_version, state, premium_mid,
             premium_fill_assumption, required_10x_price, required_move_pct, buy_under,
             trigger_reason, thesis_id, score, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event["event_id"],
                event["snapshot_time"],
                event["ticker"],
                event["contract_id"],
                event["strategy_version"],
                event["state"],
                event["premium_mid"],
                event["premium_fill_assumption"],
                event["required_10x_price"],
                event["required_move_pct"],
                event["buy_under"],
                event["trigger_reason"],
                event["thesis_id"],
                event["score"],
                json_dumps(event["raw"]),
            ],
        )
        count += 1
    return count


def build_candidate_event(row: dict[str, Any], strategy_version: str, strategy: dict[str, Any]) -> dict[str, Any] | None:
    premium = _number(row.get("mid"))
    underlying = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    required_move = _number(row.get("required_move_10x_pct"))
    if premium is None or premium <= 0 or underlying is None or underlying <= 0 or strike is None or required_move is None:
        return None
    option_type = str(row.get("option_type") or "").lower()
    hard_rejects: list[str] = []
    blockers: list[str] = []
    positives: list[str] = []

    if option_type != strategy["option_type"]:
        hard_rejects.append("strategy_only_tracks_calls")
    dte = _integer(row.get("dte"))
    if dte is None:
        blockers.append("missing_dte")
    elif dte < int(strategy["dte_min"]) or dte > int(strategy["dte_max"]):
        hard_rejects.append("dte_outside_strategy_range")
    delta = abs(_number(row.get("delta")) or 0)
    if delta:
        if delta < float(strategy["delta_min"]) or delta > float(strategy["delta_max"]):
            hard_rejects.append("delta_outside_strategy_range")
        else:
            positives.append("delta_in_range")
    else:
        blockers.append("missing_delta")
    if required_move > float(strategy["max_required_move_pct"]):
        hard_rejects.append("required_move_too_high")
    else:
        positives.append("10x_math_inside_cap")
    spread_pct = _number(row.get("spread_pct"))
    if spread_pct is None:
        blockers.append("missing_spread")
    elif spread_pct > float(strategy["reject_spread_pct"]):
        hard_rejects.append("spread_reject")
    elif spread_pct > float(strategy["max_spread_pct"]):
        blockers.append("spread_above_fire_threshold")
    else:
        positives.append("spread_usable")
    open_interest = _number(row.get("open_interest"))
    if open_interest is None:
        blockers.append("missing_open_interest")
    elif open_interest < float(strategy["min_open_interest"]):
        blockers.append("open_interest_below_threshold")
    else:
        positives.append("open_interest_supported")
    volume = _number(row.get("volume"))
    if volume is None:
        blockers.append("missing_volume")
    elif volume < float(strategy["min_volume"]):
        blockers.append("volume_below_threshold")
    else:
        positives.append("volume_seen")
    iv_percentile = _number(row.get("iv_percentile"))
    if iv_percentile is None:
        blockers.append("missing_iv_percentile")
    elif iv_percentile > float(strategy["reject_iv_percentile"]):
        hard_rejects.append("iv_percentile_reject")
    elif iv_percentile > float(strategy["max_iv_percentile"]):
        blockers.append("iv_percentile_above_fire_threshold")
    else:
        positives.append("iv_not_overpriced")
    price = _number(row.get("price"))
    ma50 = _number(row.get("ma_50"))
    if strategy.get("require_price_above_ma50"):
        if price is None or ma50 is None:
            blockers.append("missing_50d_context")
        elif price < ma50:
            blockers.append("stock_below_50d")
        else:
            positives.append("stock_above_50d")
    rs20 = _number(row.get("rs_vs_qqq_20d"))
    if strategy.get("require_rs_improving"):
        if rs20 is None:
            blockers.append("missing_rs_vs_qqq")
        elif rs20 < 0:
            blockers.append("rs_vs_qqq_20d_negative")
        else:
            positives.append("rs_vs_qqq_improving")

    buy_under = _buy_under(row, strategy)
    fill = premium * (1 + float(strategy["fill_slippage_pct"]))
    if buy_under is None:
        blockers.append("buy_under_unavailable")
    elif premium > buy_under:
        blockers.append("premium_above_buy_under")
    else:
        positives.append("premium_inside_buy_under")

    state = "REJECT" if hard_rejects else "WATCH" if _has_missing_data(blockers) else "SETUP" if blockers else "FIRE"
    reasons = [*hard_rejects, *blockers, *positives]
    snapshot_time = _iso(row.get("snapshot_time"))
    contract_id = str(row.get("contract_id"))
    event_id = stable_id("candidate_event", strategy_version, snapshot_time, contract_id)
    return {
        "event_id": event_id,
        "snapshot_time": snapshot_time,
        "ticker": _normalize_symbol(row.get("ticker")),
        "contract_id": contract_id,
        "strategy_version": strategy_version,
        "state": state,
        "premium_mid": premium,
        "premium_fill_assumption": fill,
        "required_10x_price": _number(row.get("required_10x_price")),
        "required_move_pct": required_move,
        "buy_under": buy_under,
        "trigger_reason": ", ".join(reasons),
        "thesis_id": row.get("thesis_id"),
        "score": _candidate_score(row, state),
        "raw": {
            "hard_rejects": hard_rejects,
            "blockers": blockers,
            "positives": positives,
            "strategy_parameters": strategy,
            "expiration": str(row.get("expiration")),
            "strike": strike,
            "option_type": option_type,
        },
    }


def create_shadow_trades(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        SELECT event_id, snapshot_time, premium_fill_assumption
        FROM candidate_event
        WHERE strategy_version = ? AND state = 'FIRE'
        ORDER BY snapshot_time
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        trade_id = stable_id("shadow_trade", row["event_id"])
        before = query_rows(con, "SELECT count(*) AS count FROM shadow_trade WHERE trade_id = ?", [trade_id])[0]["count"]
        con.execute(
            """
            INSERT OR IGNORE INTO shadow_trade
            (trade_id, event_id, entry_time, entry_price_assumption, exit_time, exit_price,
             status, max_return_seen, max_drawdown_seen, time_to_2x, time_to_5x, time_to_10x,
             exit_reason, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade_id,
                row["event_id"],
                row["snapshot_time"],
                _number(row["premium_fill_assumption"]),
                None,
                None,
                "open",
                0.0,
                0.0,
                None,
                None,
                None,
                None,
                json_dumps({"authority": "shadow_only", "created_from": "candidate_event"}),
            ],
        )
        after = query_rows(con, "SELECT count(*) AS count FROM shadow_trade WHERE trade_id = ?", [trade_id])[0]["count"]
        count += int(after) - int(before)
    return count


def mark_shadow_trades(con: Any) -> int:
    trades = query_rows(
        con,
        """
        SELECT
            st.*,
            ce.contract_id
        FROM candidate_event ce
        JOIN shadow_trade st ON st.event_id = ce.event_id
        WHERE st.status = 'open'
        """,
    )
    count = 0
    for trade in trades:
        latest = query_rows(
            con,
            """
            SELECT snapshot_time, mid
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        if not latest:
            continue
        current_mid = _number(latest[0].get("mid"))
        entry_price = _number(trade.get("entry_price_assumption"))
        if current_mid is None or entry_price is None or entry_price <= 0:
            continue
        current_return = current_mid / entry_price - 1
        max_return = max(_number(trade.get("max_return_seen")) or 0.0, current_return)
        max_drawdown = min(_number(trade.get("max_drawdown_seen")) or 0.0, current_return)
        time_to_2x = trade.get("time_to_2x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 1.0 else None)
        time_to_5x = trade.get("time_to_5x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 4.0 else None)
        time_to_10x = trade.get("time_to_10x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 9.0 else None)
        con.execute(
            """
            UPDATE shadow_trade
            SET max_return_seen = ?, max_drawdown_seen = ?, time_to_2x = ?,
                time_to_5x = ?, time_to_10x = ?, raw = ?
            WHERE trade_id = ?
            """,
            [
                max_return,
                max_drawdown,
                time_to_2x,
                time_to_5x,
                time_to_10x,
                json_dumps({"last_mark": latest[0]["snapshot_time"], "current_mid": current_mid, "current_return": current_return}),
                trade["trade_id"],
            ],
        )
        count += 1
    return count


def refresh_option_attributions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    trades = query_rows(
        con,
        """
        SELECT st.trade_id, st.event_id, st.entry_time, ce.contract_id, ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
        """,
        [strategy_version],
    )
    count = 0
    for trade in trades:
        latest_rows = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        if not latest_rows:
            continue
        latest = latest_rows[0]
        prior_rows = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time < TRY_CAST(? AS TIMESTAMP)
                  AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], latest["snapshot_time"], trade["entry_time"]],
        )
        if not prior_rows:
            prior_rows = query_rows(
                con,
                """
                SELECT *
                FROM option_snapshot
                WHERE contract_id = ? AND snapshot_time = TRY_CAST(? AS TIMESTAMP)
                LIMIT 1
                """,
                [trade["contract_id"], trade["entry_time"]],
            )
        if not prior_rows or prior_rows[0]["snapshot_time"] == latest["snapshot_time"]:
            continue
        attribution = build_option_attribution(trade, prior_rows[0], latest)
        if not attribution:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO option_attribution
            (attribution_id, trade_id, event_id, contract_id, snapshot_time,
             prior_snapshot_time, option_return, underlying_return, iv_change,
             theta_decay, spread_change, stock_move_effect, iv_effect,
             theta_effect, spread_effect, unexplained_effect, label, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                attribution["attribution_id"],
                attribution["trade_id"],
                attribution["event_id"],
                attribution["contract_id"],
                attribution["snapshot_time"],
                attribution["prior_snapshot_time"],
                attribution["option_return"],
                attribution["underlying_return"],
                attribution["iv_change"],
                attribution["theta_decay"],
                attribution["spread_change"],
                attribution["stock_move_effect"],
                attribution["iv_effect"],
                attribution["theta_effect"],
                attribution["spread_effect"],
                attribution["unexplained_effect"],
                attribution["label"],
                json_dumps(attribution["raw"]),
            ],
        )
        count += 1
    return count


def build_option_attribution(trade: dict[str, Any], prior: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any] | None:
    prior_mid = _number(prior.get("mid"))
    latest_mid = _number(latest.get("mid"))
    if prior_mid is None or latest_mid is None or prior_mid <= 0:
        return None
    prior_underlying = _number(prior.get("underlying_price"))
    latest_underlying = _number(latest.get("underlying_price"))
    underlying_change = None if prior_underlying is None or latest_underlying is None else latest_underlying - prior_underlying
    underlying_return = None if prior_underlying is None or prior_underlying <= 0 or latest_underlying is None else latest_underlying / prior_underlying - 1
    iv_change = _diff(_number(latest.get("iv")), _number(prior.get("iv")))
    days = max(1, _elapsed_days(prior.get("snapshot_time"), latest.get("snapshot_time")) or 1)
    theta_decay = (_number(prior.get("theta")) or 0.0) * days
    stock_move_effect = ((_number(prior.get("delta")) or 0.0) * (underlying_change or 0.0)) / prior_mid
    iv_effect = ((_number(prior.get("vega")) or 0.0) * (iv_change or 0.0)) / prior_mid
    theta_effect = theta_decay / prior_mid
    spread_change = _diff(_number(latest.get("spread_pct")), _number(prior.get("spread_pct")))
    spread_effect = -(spread_change or 0.0)
    option_return = latest_mid / prior_mid - 1
    explained = stock_move_effect + iv_effect + theta_effect + spread_effect
    unexplained = option_return - explained
    label = _attribution_label(option_return, underlying_return, iv_change, theta_effect, spread_change)
    return {
        "attribution_id": stable_id("option_attribution", trade["trade_id"], latest["snapshot_time"]),
        "trade_id": trade["trade_id"],
        "event_id": trade["event_id"],
        "contract_id": trade["contract_id"],
        "snapshot_time": _iso(latest.get("snapshot_time")),
        "prior_snapshot_time": _iso(prior.get("snapshot_time")),
        "option_return": option_return,
        "underlying_return": underlying_return,
        "iv_change": iv_change,
        "theta_decay": theta_decay,
        "spread_change": spread_change,
        "stock_move_effect": stock_move_effect,
        "iv_effect": iv_effect,
        "theta_effect": theta_effect,
        "spread_effect": spread_effect,
        "unexplained_effect": unexplained,
        "label": label,
        "raw": {
            "days": days,
            "prior_mid": prior_mid,
            "latest_mid": latest_mid,
            "prior_underlying": prior_underlying,
            "latest_underlying": latest_underlying,
            "method": "delta_vega_theta_spread_approximation",
        },
    }


def detect_missed_winners(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str = "tradingview",
) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE s.data_source = ? {symbol_filter["sql"]}
        ORDER BY s.contract_id, s.snapshot_time
        """,
        [source, *symbol_filter["params"]],
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("contract_id")), []).append(row)

    count = 0
    for contract_id, snapshots in grouped.items():
        winner = build_missed_winner(con, contract_id, snapshots, strategy_version)
        if not winner:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO missed_winner_event
            (missed_id, detected_at, ticker, contract_id, strategy_version,
             first_snapshot_time, winner_snapshot_time, entry_price_assumption,
             winner_price, max_return_seen, winner_threshold, filter_reason,
             proposed_strategy_family, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                winner["missed_id"],
                winner["detected_at"],
                winner["ticker"],
                winner["contract_id"],
                winner["strategy_version"],
                winner["first_snapshot_time"],
                winner["winner_snapshot_time"],
                winner["entry_price_assumption"],
                winner["winner_price"],
                winner["max_return_seen"],
                winner["winner_threshold"],
                winner["filter_reason"],
                winner["proposed_strategy_family"],
                json_dumps(winner["raw"]),
            ],
        )
        count += 1
    return count


def build_missed_winner(con: Any, contract_id: str, snapshots: list[dict[str, Any]], strategy_version: str) -> dict[str, Any] | None:
    usable = [row for row in snapshots if (_number(row.get("mid")) or 0) > 0]
    if len(usable) < 2:
        return None
    entry = usable[0]
    entry_mid = _number(entry.get("mid"))
    if entry_mid is None or entry_mid <= 0:
        return None
    winner = max(usable[1:], key=lambda row: _number(row.get("mid")) or 0)
    winner_mid = _number(winner.get("mid"))
    if winner_mid is None:
        return None
    max_return = winner_mid / entry_mid - 1
    if max_return < 4.0:
        return None
    fire_rows = query_rows(
        con,
        """
        SELECT event_id
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ? AND state = 'FIRE'
              AND snapshot_time <= TRY_CAST(? AS TIMESTAMP)
        LIMIT 1
        """,
        [contract_id, strategy_version, winner["snapshot_time"]],
    )
    if fire_rows:
        return None
    candidate_rows = query_rows(
        con,
        """
        SELECT state, trigger_reason, raw
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ?
        ORDER BY snapshot_time
        LIMIT 1
        """,
        [contract_id, strategy_version],
    )
    filter_reason = _missed_filter_reason(candidate_rows[0] if candidate_rows else None)
    threshold = "10x" if max_return >= 9.0 else "5x"
    proposed_family = _proposed_family(filter_reason)
    return {
        "missed_id": stable_id("missed_winner", strategy_version, contract_id, entry["snapshot_time"], winner["snapshot_time"], threshold),
        "detected_at": datetime.utcnow().isoformat(),
        "ticker": _normalize_symbol(entry.get("ticker")),
        "contract_id": contract_id,
        "strategy_version": strategy_version,
        "first_snapshot_time": _iso(entry.get("snapshot_time")),
        "winner_snapshot_time": _iso(winner.get("snapshot_time")),
        "entry_price_assumption": entry_mid,
        "winner_price": winner_mid,
        "max_return_seen": max_return,
        "winner_threshold": threshold,
        "filter_reason": filter_reason,
        "proposed_strategy_family": proposed_family,
        "raw": {
            "candidate_state": candidate_rows[0].get("state") if candidate_rows else "missing_candidate",
            "first_snapshot": _compact_snapshot(entry),
            "winner_snapshot": _compact_snapshot(winner),
        },
    }


def generate_strategy_mutation_proposals(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        SELECT filter_reason, proposed_strategy_family, count(*) AS missed_count,
               max(max_return_seen) AS best_return,
               list(missed_id) AS missed_ids
        FROM missed_winner_event
        WHERE strategy_version = ?
        GROUP BY filter_reason, proposed_strategy_family
        ORDER BY missed_count DESC, best_return DESC
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        proposal = build_strategy_mutation_proposal(row, strategy_version)
        if not proposal:
            continue
        before = query_rows(con, "SELECT count(*) AS count FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal["proposal_id"]])[0]["count"]
        con.execute(
            """
            INSERT OR REPLACE INTO strategy_mutation_proposal
            (proposal_id, created_at, source_type, strategy_version, proposed_strategy_version,
             proposed_parameter_changes, rationale, expected_effect, risk, status,
             requires_backtest, requires_forward_test, human_approval_status,
             evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                proposal["proposal_id"],
                proposal["created_at"],
                proposal["source_type"],
                proposal["strategy_version"],
                proposal["proposed_strategy_version"],
                json_dumps(proposal["proposed_parameter_changes"]),
                proposal["rationale"],
                proposal["expected_effect"],
                proposal["risk"],
                proposal["status"],
                proposal["requires_backtest"],
                proposal["requires_forward_test"],
                proposal["human_approval_status"],
                json_dumps(proposal["evidence_refs"]),
                json_dumps(proposal["raw"]),
            ],
        )
        after = query_rows(con, "SELECT count(*) AS count FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal["proposal_id"]])[0]["count"]
        count += int(after) - int(before)
    return count


def build_strategy_mutation_proposal(row: dict[str, Any], strategy_version: str) -> dict[str, Any] | None:
    filter_reason = str(row.get("filter_reason") or "unknown")
    changes = _proposal_parameter_changes(filter_reason)
    if not changes:
        return None
    family = str(row.get("proposed_strategy_family") or "leap_10x_variant")
    proposed_version = f"{family}_proposed_v1"
    missed_count = int(row.get("missed_count") or 0)
    best_return = _number(row.get("best_return")) or 0.0
    missed_ids = _list_value(row.get("missed_ids"))
    return {
        "proposal_id": stable_id("strategy_mutation_proposal", strategy_version, filter_reason, family),
        "created_at": datetime.utcnow().isoformat(),
        "source_type": "deterministic_missed_winner_analysis",
        "strategy_version": strategy_version,
        "proposed_strategy_version": proposed_version,
        "proposed_parameter_changes": changes,
        "rationale": f"{missed_count} missed winner(s) were filtered by {filter_reason}; best observed return was {best_return + 1:.2f}x.",
        "expected_effect": "Increase recall for similar 5x/10x contracts in shadow mode.",
        "risk": "May increase false positives or earlier entries; must pass deterministic backtest and forward shadow comparison before promotion.",
        "status": "proposed",
        "requires_backtest": True,
        "requires_forward_test": True,
        "human_approval_status": "required",
        "evidence_refs": [{"type": "missed_winner_event", "id": missed_id} for missed_id in missed_ids],
        "raw": {
            "filter_reason": filter_reason,
            "missed_count": missed_count,
            "best_return": best_return,
            "promotion_policy": "no_auto_promotion",
        },
    }


def _attribution_label(
    option_return: float,
    underlying_return: float | None,
    iv_change: float | None,
    theta_effect: float,
    spread_change: float | None,
) -> str:
    if spread_change is not None and spread_change > 0.10:
        return "liquidity_risk"
    if underlying_return is not None and underlying_return > 0.02 and option_return > 0.10:
        return "good_convexity"
    if underlying_return is not None and underlying_return > 0.02 and option_return <= 0.0:
        return "iv_crush_or_bad_strike"
    if underlying_return is not None and abs(underlying_return) <= 0.01 and option_return < 0.0:
        return "theta_iv_bleed"
    if iv_change is not None and iv_change < -0.05 and option_return < 0.0:
        return "iv_crush"
    if theta_effect < -0.05 and option_return < 0.0:
        return "theta_decay"
    return "mixed"


def _missed_filter_reason(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return "no_candidate_event"
    raw = _json(candidate.get("raw"))
    hard_rejects = raw.get("hard_rejects") if isinstance(raw.get("hard_rejects"), list) else []
    blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
    reasons = [str(item) for item in [*hard_rejects, *blockers] if item]
    return reasons[0] if reasons else str(candidate.get("state") or "unknown_filter")


def _proposed_family(filter_reason: str) -> str:
    if "delta" in filter_reason:
        return "leap_10x_momentum_lottery"
    if "iv" in filter_reason:
        return "leap_10x_high_iv_catalyst"
    if "open_interest" in filter_reason or "volume" in filter_reason or "spread" in filter_reason:
        return "leap_10x_liquidity_watch"
    if "50d" in filter_reason or "rs_vs_qqq" in filter_reason:
        return "leap_10x_early_reversal"
    return "leap_10x_variant"


def _proposal_parameter_changes(filter_reason: str) -> dict[str, Any]:
    if "delta_outside_strategy_range" in filter_reason:
        return {"delta_min": 0.10, "delta_max": 0.45, "candidate_note": "test lower-delta lottery sleeve separately"}
    if "iv_percentile" in filter_reason:
        return {"max_iv_percentile": 85.0, "candidate_note": "test high-IV catalyst sleeve separately"}
    if "open_interest" in filter_reason:
        return {"min_open_interest": 25, "candidate_note": "test low-OI contracts only with stronger spread and volume gates"}
    if "volume" in filter_reason:
        return {"min_volume": 0, "candidate_note": "test no-volume LEAP snapshots with stricter OI and spread gates"}
    if "spread" in filter_reason:
        return {"max_spread_pct": 0.35, "candidate_note": "test wider spreads only in shadow mode"}
    if "50d" in filter_reason:
        return {"require_price_above_ma50": False, "candidate_note": "test pre-50D early reversal sleeve"}
    if "rs_vs_qqq" in filter_reason:
        return {"require_rs_improving": False, "candidate_note": "test pre-RS recovery sleeve"}
    if "required_move" in filter_reason:
        return {"max_required_move_pct": 5.0, "candidate_note": "test larger required moves as a separate lottery strategy"}
    return {}


def _compact_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_time": _iso(row.get("snapshot_time")),
        "ticker": _normalize_symbol(row.get("ticker")),
        "contract_id": row.get("contract_id"),
        "underlying_price": _number(row.get("underlying_price")),
        "expiration": str(row.get("expiration")),
        "strike": _number(row.get("strike")),
        "option_type": row.get("option_type"),
        "mid": _number(row.get("mid")),
        "iv": _number(row.get("iv")),
        "delta": _number(row.get("delta")),
        "spread_pct": _number(row.get("spread_pct")),
        "volume": _number(row.get("volume")),
        "open_interest": _number(row.get("open_interest")),
    }


def _strategy_parameters(con: Any, strategy_version: str) -> dict[str, Any]:
    rows = query_rows(con, "SELECT parameters FROM option_strategy_versions WHERE strategy_version = ?", [strategy_version])
    if not rows:
        register_default_strategy(con, strategy_version)
        return dict(DEFAULT_STRATEGY_PARAMETERS)
    return {**DEFAULT_STRATEGY_PARAMETERS, **_json(rows[0].get("parameters"))}


def _symbol_filter(symbols: list[str] | None, *, table_alias: str, column: str = "symbol") -> dict[str, Any]:
    clean = [_normalize_symbol(symbol) for symbol in symbols or [] if symbol]
    if not clean:
        return {"sql": "", "params": []}
    placeholders = ", ".join(["?"] * len(clean))
    return {"sql": f"AND {table_alias}.{column} IN ({placeholders})", "params": clean}


def _contract_id(ticker: str, expiration: Any, strike: float | None, option_type: str, provider_symbol: Any) -> str:
    if provider_symbol:
        return str(provider_symbol)
    return f"{ticker}:{expiration}:{strike:g}:{option_type}" if strike is not None else stable_id(ticker, expiration, option_type)


def _premium_mid(row: dict[str, Any], raw: dict[str, Any]) -> float | None:
    mid = _number(row.get("mid")) or _coalesce_number(raw, "mid", "mark")
    if mid is not None:
        return mid
    bid = _number(row.get("bid")) or _coalesce_number(raw, "bid")
    ask = _number(row.get("ask")) or _coalesce_number(raw, "ask")
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or mid <= 0:
        return None
    return max(0.0, (ask - bid) / mid)


def _required_move_pct(option_type: str, underlying: float | None, required_price: float) -> float | None:
    if underlying is None or underlying <= 0:
        return None
    if option_type == "put":
        return max(0.0, (underlying - required_price) / underlying)
    return max(0.0, (required_price - underlying) / underlying)


def _diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _liquidity_score(spread_pct: float | None, open_interest: float | None, volume: float | None) -> float | None:
    components: list[float] = []
    weights: list[float] = []
    if spread_pct is not None:
        components.append(max(0.0, min(100.0, 100.0 - spread_pct * 300.0)))
        weights.append(0.60)
    if open_interest is not None:
        components.append(max(0.0, min(100.0, open_interest / 500.0 * 100.0)))
        weights.append(0.25)
    if volume is not None:
        components.append(max(0.0, min(100.0, volume / 100.0 * 100.0)))
        weights.append(0.15)
    if not components:
        return None
    score = sum(component * weight for component, weight in zip(components, weights, strict=False)) / sum(weights)
    if open_interest is None or volume is None:
        score = min(score, 70.0)
    return round(score, 2)


def _convexity_score(required_move_pct: float | None, delta: float | None, dte: int | None) -> float | None:
    if required_move_pct is None:
        return None
    move_score = max(0.0, min(100.0, 100.0 - required_move_pct * 25.0))
    delta_score = 100.0 - min(100.0, abs((abs(delta or 0.30) - 0.30) * 180.0))
    dte_score = 100.0 if dte is None else max(0.0, min(100.0, (dte - 180) / 720 * 100.0))
    return round(move_score * 0.60 + delta_score * 0.25 + dte_score * 0.15, 2)


def _iv_history_by_ticker(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    history: dict[str, list[float]] = {}
    for row in rows:
        iv = _number(row.get("iv"))
        if iv is None:
            continue
        history.setdefault(_normalize_symbol(row.get("ticker")), []).append(iv)
    return history


def _percentile_rank(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    return round(sum(1 for item in history if item <= value) / len(history) * 100, 2)


def _iv_rank(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    low = min(history)
    high = max(history)
    if high == low:
        return 50.0
    return round((value - low) / (high - low) * 100, 2)


def _relative_strength(values: list[float | None], benchmark: list[float | None], period: int) -> float | None:
    clean = [value for value in values if value is not None]
    bench = [value for value in benchmark if value is not None]
    if len(clean) <= period or len(bench) <= period:
        return None
    stock_return = clean[-1] / clean[-period - 1] - 1
    benchmark_return = bench[-1] / bench[-period - 1] - 1
    return stock_return - benchmark_return


def _atr_pct(rows: list[dict[str, Any]], period: int = 14) -> float | None:
    if not rows:
        return None
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in rows[-period:]:
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        if high is None or low is None:
            continue
        values = [high - low]
        if previous_close is not None:
            values.extend([abs(high - previous_close), abs(low - previous_close)])
        true_ranges.append(max(values))
        previous_close = close
    close = _number(rows[-1].get("close"))
    if not true_ranges or close is None or close <= 0:
        return None
    return mean(true_ranges) / close


def _volume_ratio(volumes: list[float]) -> float | None:
    if len(volumes) < 20:
        return None
    recent = _average(volumes[-20:])
    baseline = _average(volumes[-60:]) if len(volumes) >= 60 else _average(volumes)
    if recent is None or baseline is None or baseline <= 0:
        return None
    return recent / baseline


def _base_length_days(closes: list[float], high_252: float) -> int | None:
    if not closes or high_252 <= 0:
        return None
    floor = high_252 * 0.75
    count = 0
    for close in reversed(closes):
        if close < floor:
            break
        count += 1
    return count


def _buy_under(row: dict[str, Any], strategy: dict[str, Any]) -> float | None:
    underlying = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    if underlying is None or strike is None:
        return None
    max_move = float(strategy["max_required_move_pct"])
    option_type = str(row.get("option_type") or "").lower()
    if option_type == "put":
        return max(0.0, (strike - underlying * (1 - max_move)) / 10)
    return max(0.0, (underlying * (1 + max_move) - strike) / 10)


def _candidate_score(row: dict[str, Any], state: str) -> float:
    if state == "REJECT":
        return 0.0
    required_move = _number(row.get("required_move_10x_pct")) or 10
    liquidity = _number(row.get("liquidity_score")) or 0
    convexity = _number(row.get("convexity_score")) or 0
    rs = _number(row.get("rs_vs_qqq_20d")) or 0
    technical = 100.0 if (_number(row.get("price")) or 0) >= (_number(row.get("ma_50")) or 10**9) else 45.0
    score = (max(0.0, 100.0 - required_move * 20.0) * 0.35) + (liquidity * 0.20) + (convexity * 0.30) + (technical * 0.10) + (max(-20.0, min(20.0, rs * 100)) + 20) * 0.05
    if state == "WATCH":
        score *= 0.70
    if state == "SETUP":
        score *= 0.88
    return round(max(0.0, min(100.0, score)), 2)


def _has_missing_data(blockers: list[str]) -> bool:
    return any(blocker.startswith("missing_") for blocker in blockers)


def _days_to_expiration(expiration: Any, snapshot_time: str) -> int | None:
    expiry_date = _date(expiration)
    snapshot_date = _date(snapshot_time)
    if expiry_date is None or snapshot_date is None:
        return None
    return (expiry_date - snapshot_date).days


def _elapsed_days(start: Any, end: Any) -> int | None:
    start_date = _date(start)
    end_date = _date(end)
    if start_date is None or end_date is None:
        return None
    return (end_date - start_date).days


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, tuple):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return [value] if value else []
        if isinstance(decoded, list):
            return [str(item) for item in decoded if item]
    return []


def _coalesce_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _average(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return mean(clean)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().split(":")[-1]
