"""Bounded universe and row helpers for option-chain ingestion."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

from psycopg import sql
from psycopg.types.json import Jsonb

from investment_panel.database.ingestion_coerce import (
    aware_datetime,
    integer,
    number,
)
from investment_panel.database.instruments import canonical_symbol
from investment_panel.database.runtime import DatabaseRuntime


def option_universe(
    runtime: DatabaseRuntime,
    configured: Sequence[dict[str, Any]] = (),
    *,
    limit: int | None = None,
) -> list[str]:
    with runtime.read() as connection:
        rows = connection.execute(
            """
            WITH canonical_instruments AS (
                SELECT DISTINCT ON (canonical_symbol)
                       instrument.id, canonical_symbol AS symbol, instrument.asset_class
                FROM (
                    SELECT instrument.*,
                           regexp_replace(upper(instrument.symbol), '[.]+$', '') AS canonical_symbol
                    FROM catalog.instrument instrument
                ) instrument
                WHERE canonical_symbol <> ''
                ORDER BY canonical_symbol,
                         (upper(instrument.symbol) = canonical_symbol) DESC,
                         instrument.updated_at DESC, instrument.id
            ), source_signal AS (
                SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol,
                       count(DISTINCT CASE WHEN source.kind = 'news' THEN lower(source.name) ELSE source.id END) AS source_roots,
                       max(item.observed_at) AS latest_signal_at
                FROM raw.content_item_instrument link
                JOIN raw.content_item item ON item.id = link.content_item_id
                JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
                JOIN ingest.source source ON source.id = item.source_id
                WHERE source.enabled
                  AND item.observed_at >= now() - interval '30 days'
                  AND item.observed_at <= now()
                  AND COALESCE(item.published_at, item.observed_at) <= now()
                GROUP BY regexp_replace(upper(instrument.symbol), '[.]+$', '')
            ), upcoming_catalyst AS (
                SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol,
                       min(catalyst.starts_at) AS starts_at
                FROM app.catalyst catalyst
                JOIN catalog.instrument instrument ON instrument.id = catalyst.instrument_id
                WHERE catalyst.status = 'current'
                  AND catalyst.starts_at >= now() AND catalyst.starts_at < now() + interval '90 days'
                GROUP BY regexp_replace(upper(instrument.symbol), '[.]+$', '')
            ), recent_option_decision AS (
                SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol,
                       max(decision.as_of) AS latest_decision_at
                FROM analysis.decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE decision.kind = 'option' AND decision.as_of >= now() - interval '14 days'
                GROUP BY regexp_replace(upper(instrument.symbol), '[.]+$', '')
            )
            SELECT i.symbol, p.instrument_id IS NOT NULL AS is_owned, w.watch_state,
                   coalesce(source_signal.source_roots, 0) AS source_roots,
                   upcoming_catalyst.starts_at, recent_option_decision.latest_decision_at
            FROM canonical_instruments i
            LEFT JOIN app.portfolio_position p ON p.instrument_id = i.id
            LEFT JOIN app.watchlist_item w ON w.instrument_id = i.id
            LEFT JOIN source_signal ON source_signal.symbol = i.symbol
            LEFT JOIN upcoming_catalyst ON upcoming_catalyst.symbol = i.symbol
            LEFT JOIN recent_option_decision ON recent_option_decision.symbol = i.symbol
            WHERE p.instrument_id IS NOT NULL OR w.instrument_id IS NOT NULL
               OR (i.asset_class IN ('equity', 'etf') AND
                   (source_signal.symbol IS NOT NULL OR upcoming_catalyst.symbol IS NOT NULL
                    OR recent_option_decision.symbol IS NOT NULL))
            ORDER BY (p.instrument_id IS NOT NULL) DESC,
                     (w.watch_state IS NOT NULL AND w.watch_state <> 'excluded') DESC,
                     (recent_option_decision.symbol IS NOT NULL) DESC,
                     (upcoming_catalyst.starts_at IS NOT NULL) DESC,
                     coalesce(source_signal.source_roots, 0) DESC,
                     source_signal.latest_signal_at DESC NULLS LAST, i.symbol
            """
        ).fetchall()
    excluded = {
        str(row["symbol"])
        for row in rows
        if row["watch_state"] == "excluded" and not row["is_owned"]
    }
    owned = [str(row["symbol"]) for row in rows if row["is_owned"]]
    persisted_watchlist = [
        str(row["symbol"]) for row in rows
        if not row["is_owned"] and row["watch_state"] not in (None, "excluded")
    ]
    opportunistic = [
        str(row["symbol"]) for row in rows
        if not row["is_owned"] and row["watch_state"] is None
    ]
    configured_symbols = [
        str(item.get("symbol") or "").strip().upper()
        for item in configured
        if str(item.get("symbol") or "").strip().upper()
        and str(item.get("symbol") or "").strip().upper() not in excluded
    ]
    output = list(dict.fromkeys(owned))
    seen = set(output)
    buckets = [configured_symbols, persisted_watchlist]
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index >= len(bucket):
                continue
            symbol = bucket[index]
            if symbol not in seen:
                seen.add(symbol)
                output.append(symbol)
    for symbol in opportunistic:
        if symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output if limit is None else output[:max(0, int(limit))]


def stage_option_rows(connection: Any, rows: Sequence[dict[str, Any]]) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE option_quote_stage (
            underlying_symbol TEXT NOT NULL, expiration DATE NOT NULL, strike NUMERIC(20, 6) NOT NULL,
            option_type TEXT NOT NULL, multiplier INTEGER NOT NULL, provider_symbol TEXT,
            style TEXT, settlement TEXT, deliverable_key TEXT,
            standard_contract_verified BOOLEAN NOT NULL,
            resolved_deliverable_key TEXT,
            underlying_price DOUBLE PRECISION, bid DOUBLE PRECISION, ask DOUBLE PRECISION,
            mid DOUBLE PRECISION, last DOUBLE PRECISION, bid_size BIGINT, ask_size BIGINT,
            last_trade_at TIMESTAMPTZ, captured_at TIMESTAMPTZ, market_data_status TEXT,
            volume BIGINT, open_interest BIGINT,
            provider_iv DOUBLE PRECISION, provider_delta DOUBLE PRECISION,
            provider_gamma DOUBLE PRECISION, provider_theta DOUBLE PRECISION,
            provider_vega DOUBLE PRECISION, previous_close DOUBLE PRECISION,
            provider_rho DOUBLE PRECISION, chance_of_profit_long DOUBLE PRECISION,
            chance_of_profit_short DOUBLE PRECISION, provider_updated_at TIMESTAMPTZ,
            provider_payload JSONB, capture_group_key TEXT, group_started_at TIMESTAMPTZ,
            group_finished_at TIMESTAMPTZ, provider_observed_at TIMESTAMPTZ,
            available_at TIMESTAMPTZ, underlying_observed_at TIMESTAMPTZ,
            underlying_available_at TIMESTAMPTZ
        ) ON COMMIT DROP
        """
    )
    columns = tuple(rows[0].keys())
    with connection.cursor().copy(
        sql.SQL("COPY option_quote_stage ({}) FROM STDIN").format(
            sql.SQL(", ").join(map(sql.Identifier, columns))
        )
    ) as copy:
        for row in rows:
            copy.write_row([row[column] for column in columns])


def normalize_option_row(row: dict[str, Any]) -> dict[str, Any]:
    symbol = canonical_symbol(row.get("underlying_symbol") or row.get("symbol") or row.get("ticker"))
    option_type = str(row.get("option_type") or row.get("type") or "").strip().lower()
    if option_type not in {"call", "put"}:
        raise ValueError("option row option_type must be call or put")
    try:
        expiration = row["expiration"] if "expiration" in row else row["expiry"]
        strike = float(row["strike"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("option row requires expiration and numeric strike") from exc
    if isinstance(expiration, str):
        expiration = date.fromisoformat(expiration[:10])
    style = str(row.get("style") or row.get("exercise_style") or "").strip().lower() or None
    settlement = str(row.get("settlement") or row.get("settlement_type") or "").strip().lower() or None
    deliverable_key = str(row.get("deliverable_key") or row.get("chain_id") or "").strip() or None
    standard_contract_verified = (
        row.get("standard_contract_verified") is True
        and style == "american"
        and settlement == "physical"
        and deliverable_key is not None
    )
    return {
        "underlying_symbol": symbol, "expiration": expiration, "strike": strike,
        "option_type": option_type, "multiplier": int(row.get("multiplier") or 100),
        "provider_symbol": str(row.get("provider_symbol") or row.get("contract_symbol") or "").strip() or None,
        "style": style, "settlement": settlement, "deliverable_key": deliverable_key,
        "standard_contract_verified": standard_contract_verified,
        "underlying_price": number(row.get("underlying_price")), "bid": number(row.get("bid")),
        "ask": number(row.get("ask")), "mid": number(row.get("mid")), "last": number(row.get("last")),
        "bid_size": integer(row.get("bid_size")), "ask_size": integer(row.get("ask_size")),
        "last_trade_at": aware_datetime(row.get("last_trade_at")),
        "captured_at": aware_datetime(row.get("captured_at")),
        "market_data_status": str(row.get("market_data_status") or row.get("market_data") or "").lower() or None,
        "volume": integer(row.get("volume")), "open_interest": integer(row.get("open_interest")),
        "provider_iv": number(row.get("provider_iv") if "provider_iv" in row else row.get("iv")),
        "provider_delta": number(row.get("provider_delta") if "provider_delta" in row else row.get("delta")),
        "provider_gamma": number(row.get("provider_gamma") if "provider_gamma" in row else row.get("gamma")),
        "provider_theta": number(row.get("provider_theta") if "provider_theta" in row else row.get("theta")),
        "provider_vega": number(row.get("provider_vega") if "provider_vega" in row else row.get("vega")),
        "previous_close": number(row.get("previous_close") if "previous_close" in row else row.get("close")),
        "provider_rho": number(row.get("provider_rho") if "provider_rho" in row else row.get("rho")),
        "chance_of_profit_long": number(row.get("chance_of_profit_long")),
        "chance_of_profit_short": number(row.get("chance_of_profit_short")),
        "provider_updated_at": aware_datetime(row.get("provider_updated_at") or row.get("updated_at")),
        "provider_payload": Jsonb(dict(row.get("provider_payload") or {})),
        "capture_group_key": str(row.get("capture_group_key") or f"{expiration}:{option_type}"),
        "group_started_at": aware_datetime(row.get("group_started_at")),
        "group_finished_at": aware_datetime(row.get("group_finished_at")),
        "provider_observed_at": aware_datetime(row.get("provider_observed_at") or row.get("provider_updated_at") or row.get("updated_at")),
        "available_at": aware_datetime(row.get("available_at") or row.get("captured_at")),
        "underlying_observed_at": aware_datetime(row.get("underlying_observed_at")),
        "underlying_available_at": aware_datetime(row.get("underlying_available_at")),
    }
