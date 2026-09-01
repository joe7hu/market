"""PostgreSQL authority for watchlist analytics and option-chain composition."""

from __future__ import annotations

from typing import Any

from investment_panel.core.options_intelligence import build_expiry_signal, build_ticker_signal


TECHNICALS_QUERY = """
    WITH daily AS (
        SELECT DISTINCT ON (bar.instrument_id, bar.trading_date)
               bar.instrument_id, instrument.symbol, bar.trading_date,
               bar.observed_at, bar.available_at, bar.high, bar.low, bar.close, bar.volume
        FROM raw.confirmed_price_bar bar
        JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
        WHERE bar.interval = '1d' AND bar.close > 0
        ORDER BY bar.instrument_id, bar.trading_date,
                 (bar.source_id = 'daily-market-prices') DESC,
                 bar.observed_at DESC, bar.available_at DESC
    ), sequenced AS (
        SELECT daily.*,
               row_number() OVER (
                   PARTITION BY instrument_id ORDER BY trading_date DESC
               ) AS rn,
               lag(close) OVER (
                   PARTITION BY instrument_id ORDER BY trading_date
               ) AS prior_close
        FROM daily
    ), aggregated AS (
        SELECT instrument_id, symbol, max(observed_at) AS as_of,
               max(available_at) AS available_at,
               max(close) FILTER (WHERE rn = 1) AS price,
               avg(close) FILTER (WHERE rn <= 20) AS sma_20,
               avg(close) FILTER (WHERE rn <= 50) AS sma_50,
               avg(close) FILTER (WHERE rn <= 200) AS sma_200,
               avg(volume) FILTER (WHERE rn <= 20) AS average_volume_20d,
               max(close) FILTER (WHERE rn = 1)
                   / NULLIF(max(close) FILTER (WHERE rn = 21), 0) - 1 AS return_20d,
               max(close) FILTER (WHERE rn = 1)
                   / NULLIF(max(close) FILTER (WHERE rn = 61), 0) - 1 AS return_60d,
               max(close) FILTER (WHERE rn = 1)
                   / NULLIF(max(close) FILTER (WHERE rn = 252), 0) - 1 AS return_1y,
               max(close) FILTER (WHERE rn = 1)
                   / NULLIF((array_agg(close ORDER BY trading_date)
                       FILTER (WHERE trading_date >= date_trunc('year', current_date)))[1], 0) - 1 AS return_ytd,
               max(close) FILTER (WHERE rn = 1)
                   / NULLIF(max(high) FILTER (WHERE rn <= 252), 0) - 1 AS drawdown_from_high,
               avg(volume) FILTER (WHERE rn <= 22)
                   / NULLIF(avg(volume) FILTER (WHERE rn BETWEEN 23 AND 85), 0) AS relative_volume_1m,
               avg(greatest(
                   high - low,
                   abs(high - prior_close),
                   abs(low - prior_close)
               )) FILTER (WHERE rn <= 22)
                   / NULLIF(max(close) FILTER (WHERE rn = 1), 0) AS atr_pct_1m,
               jsonb_agg(jsonb_build_object(
                   'date', trading_date, 'close', close
               ) ORDER BY trading_date) FILTER (WHERE rn <= 252) AS chart_1y,
               jsonb_agg(jsonb_build_object(
                   'date', trading_date, 'value', volume
               ) ORDER BY trading_date) FILTER (WHERE rn <= 22) AS volume_1m_bars,
               jsonb_agg(jsonb_build_object(
                   'date', trading_date,
                   'value', greatest(high - low, abs(high - prior_close), abs(low - prior_close))
                       / NULLIF(close, 0)
               ) ORDER BY trading_date) FILTER (WHERE rn <= 22) AS atr_pct_1m_points
        FROM sequenced
        WHERE rn <= 252
        GROUP BY instrument_id, symbol
    ), price_ranks AS (
        SELECT sequenced.instrument_id,
               100.0 * (count(*) FILTER (
                   WHERE sequenced.rn <= 252 AND sequenced.close <= aggregated.price
               ) - 1) / NULLIF(count(*) FILTER (WHERE sequenced.rn <= 252) - 1, 0)
                   AS price_percentile_1y
        FROM sequenced
        JOIN aggregated USING (instrument_id)
        GROUP BY sequenced.instrument_id
    )
    SELECT aggregated.*,
           aggregated.price / NULLIF(aggregated.sma_50, 0) - 1 AS distance_from_sma_50,
           price_ranks.price_percentile_1y,
           price_ranks.price_percentile_1y AS valuation_percentile,
           ((aggregated.price >= aggregated.sma_20)::int
             + (aggregated.price >= aggregated.sma_50)::int
             + (aggregated.price >= aggregated.sma_200)::int
             + (aggregated.return_20d > 0)::int
             + (aggregated.return_60d > 0)::int) * 20 AS technical_score
    FROM aggregated
    JOIN price_ranks USING (instrument_id)
    ORDER BY aggregated.symbol
"""


def technical_rows(connection: Any, *, symbols: set[str] | None = None) -> list[dict[str, Any]]:
    """Return technical rows, constraining the history scan before windows run."""

    normalized = sorted({str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()})
    if symbols is not None and not normalized:
        return []
    if not normalized:
        result = connection.execute(TECHNICALS_QUERY)
    else:
        query = TECHNICALS_QUERY.replace(
            "WITH daily AS (",
            """WITH candidate_instruments AS MATERIALIZED (
                SELECT id, symbol FROM catalog.instrument WHERE symbol = ANY(%s)
            ), facts AS MATERIALIZED (
                SELECT fact.id, fact.instrument_id, fact.source_id, fact.interval,
                       fact.trading_date, fact.observed_at, fact.available_at,
                       fact.high, fact.low, fact.close, fact.volume
                FROM raw.price_bar fact
                JOIN candidate_instruments candidate ON candidate.id = fact.instrument_id
                WHERE fact.interval = '1d' AND fact.close > 0
                UNION ALL
                SELECT fact.id, fact.instrument_id, fact.source_id, fact.interval,
                       fact.trading_date, fact.observed_at, fact.available_at,
                       fact.high, fact.low, fact.close, fact.volume
                FROM raw.price_bar_history fact
                JOIN candidate_instruments candidate ON candidate.id = fact.instrument_id
                WHERE fact.interval = '1d' AND fact.close > 0
            ), confirmed_price_bars AS MATERIALIZED (
                SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at)
                       fact.*
                FROM facts fact
                JOIN LATERAL (
                    SELECT availability.ingest_run_id
                    FROM raw.price_bar_fact_availability availability
                    WHERE availability.fact_id = fact.id
                      AND availability.fact_available_at = fact.available_at
                    LIMIT 1
                ) availability ON true
                JOIN ingest.run price_run
                  ON price_run.id = availability.ingest_run_id
                 AND price_run.status IN ('succeeded', 'partial')
                 AND price_run.finished_at IS NOT NULL
                ORDER BY fact.instrument_id, fact.source_id, fact.interval,
                         fact.observed_at, fact.available_at DESC
            ), daily AS (""",
            1,
        ).replace(
            "FROM raw.confirmed_price_bar bar",
            "FROM confirmed_price_bars bar",
            1,
        )
        result = connection.execute(query, [normalized])
    return [dict(row) for row in result.fetchall()]


def options_ticker_signal_rows(connection: Any, *, symbols: set[str] | None = None) -> list[dict[str, Any]]:
    """Compose current per-ticker option context from PostgreSQL chain facts."""
    if symbols is not None and not symbols:
        return []
    symbol_filter = sorted(symbols or set())
    outer_symbol_clause = " AND instrument.symbol = ANY(%s)" if symbol_filter else ""
    parameters = [symbol_filter] if symbol_filter else []
    query = f"""
            WITH latest_snapshot AS (
                SELECT id
                FROM raw.option_snapshot
                WHERE collection_profile = 'radar' AND contract_count > 0
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
            )
            SELECT instrument.symbol, snapshot.source_id AS source,
                   contract.expiration::text AS expiry,
                   contract.expiration - current_date AS dte,
                   contract.strike::double precision AS strike,
                   contract.option_type, quote.bid, quote.ask, quote.mid, quote.last,
                   quote.provider_iv AS iv, quote.provider_delta AS delta,
                   quote.underlying_price AS spot, quote.observed_at,
                   quote.available_at
            FROM raw.option_quote quote
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN latest_snapshot latest ON latest.id = quote.snapshot_id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            WHERE contract.expiration >= current_date{outer_symbol_clause}
            ORDER BY instrument.symbol, snapshot.source_id, contract.expiration, contract.strike,
                     contract.option_type
            """
    chain_rows = [
        dict(row)
        for row in (connection.execute(query, parameters) if parameters else connection.execute(query)).fetchall()
    ]
    by_expiry: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in chain_rows:
        key = (str(row["symbol"]), str(row["source"]), str(row["expiry"]))
        by_expiry.setdefault(key, []).append(row)

    by_ticker: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (symbol, source, expiry), rows in by_expiry.items():
        signal = build_expiry_signal(
            symbol,
            expiry,
            source,
            rows,
            {"dte": rows[0].get("dte"), "contracts_count": len(rows)},
            {"price": rows[0].get("spot")},
        )
        if signal:
            by_ticker.setdefault((symbol, source), []).append(signal)

    composed: list[dict[str, Any]] = []
    for (symbol, source), signals in by_ticker.items():
        if not signals:
            continue
        row = {"ticker": symbol, **build_ticker_signal(symbol, source, signals)}
        if row.get("put_call_iv_skew") is None:
            row["skew_signal"] = "N/A"
        composed.append(row)
    newest_by_symbol: dict[str, dict[str, Any]] = {}
    for row in composed:
        symbol = str(row["symbol"])
        existing = newest_by_symbol.get(symbol)
        if existing is None or str(row.get("as_of") or "") > str(existing.get("as_of") or ""):
            newest_by_symbol[symbol] = row
    return sorted(newest_by_symbol.values(), key=lambda row: str(row["symbol"]))
