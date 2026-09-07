"""Versioned daily price-bar ingestion and quote materialization."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Sequence
from uuid import UUID

from investment_panel.core.decision import is_us_market_day, market_session_bounds
from investment_panel.core.market_time import current_market_date, market_timezone_for_symbol
from investment_panel.database.ingestion_coerce import calendar_date, number
from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.price_fact_versions import confirm_price_fact, lock_price_fact
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def store_price_bars(
    runtime: DatabaseRuntime,
    run_id: UUID,
    source_id: str,
    rows: Sequence[dict[str, Any]],
    asset_classes: dict[str, str] | None = None,
) -> int:
    normalized_asset_classes = {
        canonical_symbol(symbol): str(asset_class or "equity")
        for symbol, asset_class in (asset_classes or {}).items()
    }
    prepared: list[tuple[dict[str, Any], str, date, float]] = []
    examples: dict[str, dict[str, Any]] = {}
    for source in rows:
        try:
            symbol = canonical_symbol(source.get("symbol"))
        except ValueError:
            continue
        trading_date = calendar_date(source.get("date") or source.get("trading_date"))
        close = number(source.get("close"))
        if symbol and trading_date is not None and close is not None:
            prepared.append((source, symbol, trading_date, close))
            examples.setdefault(symbol, source)
    latest: dict[str, dict[str, Any]] = {}
    stored = 0
    stored_at = datetime.now(UTC)
    with runtime.transaction(JOB_PROFILE) as connection:
        instruments: dict[str, int] = {}
        for symbol, source in examples.items():
            asset_class = normalized_asset_classes.get(symbol, str(source.get("asset_class") or "equity"))
            instruments[symbol] = reconcile_instrument(
                connection,
                symbol,
                name=source.get("name") or symbol,
                asset_class=asset_class,
                category="market_data",
            )
        for source, symbol, trading_date, close in prepared:
            market_date = current_market_date(symbol, stored_at)
            if trading_date > market_date:
                continue
            if trading_date == market_date and source.get("is_complete") is not True:
                continue
            legacy_observed_at = datetime.combine(trading_date, time(20), tzinfo=UTC)
            asset_class = normalized_asset_classes.get(symbol, str(source.get("asset_class") or "equity"))
            session_close = (
                market_session_bounds(trading_date)[1].astimezone(UTC)
                if asset_class in {"equity", "etf"} and market_timezone_for_symbol(symbol) == "America/New_York"
                and is_us_market_day(trading_date) else None
            )
            observed_at = session_close or legacy_observed_at
            open_price = number(source.get("open"))
            high = number(source.get("high"))
            low = number(source.get("low"))
            volume = number(source.get("volume"))
            currency = str(source.get("currency") or "USD")
            # Keep the existing date-stable lock while repairing the old
            # nominal clock through the same fact/version owner.
            lock_price_fact(connection, "price_bar", instruments[symbol], source_id, "1d", legacy_observed_at)
            latest_bar = connection.execute(
                """
                SELECT bar.id, bar.available_at, bar.observed_at, bar.ingest_run_id, bar.open, bar.high, bar.low, bar.close,
                       bar.volume, bar.currency, price_run.status AS run_status
                FROM raw.price_bar bar
                JOIN ingest.run price_run ON price_run.id = bar.ingest_run_id
                WHERE bar.instrument_id = %s AND bar.source_id = %s
                  AND bar.interval = '1d' AND bar.trading_date = %s
                  AND bar.observed_at = ANY(%s)
                ORDER BY (bar.observed_at = %s) DESC, bar.available_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                [instruments[symbol], source_id, trading_date, [observed_at, legacy_observed_at], observed_at],
            ).fetchone()
            current_bar = (open_price, high, low, close, volume, currency)
            # The availability projection retains the first confirmation of
            # a fact version. Preserve a distinct completed-session version
            # when an earlier, unchanged daily value was known before close.
            needs_close_version = (
                latest_bar is not None and session_close is not None
                and latest_bar["available_at"] < session_close <= stored_at
            )
            if latest_bar is None:
                bar_fact = connection.execute(
                    """
                    INSERT INTO raw.price_bar (
                        instrument_id, source_id, ingest_run_id, interval,
                        trading_date, observed_at, open, high, low, close, volume, currency
                    )
                    VALUES (%s, %s, %s, '1d', %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, available_at
                    """,
                    [instruments[symbol], source_id, run_id, trading_date, observed_at,
                     open_price, high, low, close, volume, currency],
                ).fetchone()
            elif (
                tuple(latest_bar[key] for key in ("open", "high", "low", "close", "volume", "currency"))
                != current_bar or needs_close_version or latest_bar["observed_at"] != observed_at
            ):
                connection.execute(
                    "INSERT INTO raw.price_bar_history SELECT * FROM raw.price_bar WHERE id = %s",
                    [latest_bar["id"]],
                )
                bar_fact = connection.execute(
                    """
                    UPDATE raw.price_bar SET ingest_run_id = %s, trading_date = %s, observed_at = %s,
                        open = %s, high = %s, low = %s, close = %s, volume = %s,
                        currency = %s, available_at = clock_timestamp()
                    WHERE id = %s RETURNING id, available_at
                    """,
                    [run_id, trading_date, observed_at, open_price, high, low, close, volume,
                     currency, latest_bar["id"]],
                ).fetchone()
            else:
                bar_fact = latest_bar
                if latest_bar["run_status"] == "failed":
                    connection.execute(
                        "UPDATE raw.price_bar SET ingest_run_id = %s WHERE id = %s",
                        [run_id, latest_bar["id"]],
                    )
            confirm_price_fact(connection, "price_bar", bar_fact["id"], bar_fact["available_at"], run_id)
            stored += 1
            if symbol not in latest or trading_date > latest[symbol]["date"]:
                latest[symbol] = {
                    "instrument_id": instruments[symbol], "date": trading_date,
                    "price": close, "observed_at": observed_at,
                    "previous_observed_at": latest_bar["observed_at"] if latest_bar else None,
                    "previous_ingest_run_id": latest_bar["ingest_run_id"] if latest_bar else None,
                }
        _materialize_latest_quotes(connection, run_id, source_id, latest)
    return stored


def _materialize_latest_quotes(
    connection: Any,
    run_id: UUID,
    source_id: str,
    latest: dict[str, dict[str, Any]],
) -> None:
    for row in latest.values():
        observed_at = row["observed_at"]
        previous_observed_at = row.get("previous_observed_at")
        repair_clock = previous_observed_at is not None and previous_observed_at != observed_at
        # Acquire both quote identities in a stable order during a clock repair.
        for quote_clock in sorted({observed_at, previous_observed_at} if repair_clock else {observed_at}):
            lock_price_fact(connection, "quote", row["instrument_id"], source_id, quote_clock)
        latest_quote = connection.execute(
            """
            SELECT quote.id, quote.available_at, quote.observed_at, quote.price, price_run.status AS run_status
            FROM raw.quote quote
            JOIN ingest.run price_run ON price_run.id = quote.ingest_run_id
            WHERE quote.instrument_id = %s AND quote.source_id = %s AND quote.observed_at = %s
            FOR UPDATE
            """,
            [row["instrument_id"], source_id, observed_at],
        ).fetchone()
        if latest_quote is None and repair_clock:
            latest_quote = connection.execute(
                """SELECT quote.id, quote.available_at, quote.observed_at, quote.price, price_run.status AS run_status
                   FROM raw.quote quote JOIN ingest.run price_run ON price_run.id = quote.ingest_run_id
                   WHERE quote.instrument_id = %s AND quote.source_id = %s AND quote.observed_at = %s
                     AND quote.ingest_run_id = %s FOR UPDATE""",
                [row["instrument_id"], source_id, previous_observed_at, row["previous_ingest_run_id"]],
            ).fetchone()
        if latest_quote is not None and latest_quote["observed_at"] == observed_at and float(latest_quote["price"]) == float(row["price"]):
            if latest_quote["run_status"] == "failed":
                connection.execute(
                    "UPDATE raw.quote SET ingest_run_id = %s WHERE id = %s",
                    [run_id, latest_quote["id"]],
                )
            confirm_price_fact(
                connection, "quote", latest_quote["id"], latest_quote["available_at"], run_id
            )
            continue
        if latest_quote is None:
            quote_fact = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, price, currency)
                VALUES (%s, %s, %s, %s, %s, 'USD') RETURNING id, available_at
                """,
                [row["instrument_id"], source_id, run_id, observed_at, row["price"]],
            ).fetchone()
        else:
            connection.execute(
                "INSERT INTO raw.quote_history SELECT * FROM raw.quote WHERE id = %s",
                [latest_quote["id"]],
            )
            quote_fact = connection.execute(
                """
                UPDATE raw.quote SET ingest_run_id = %s, price = %s, observed_at = %s,
                    currency = 'USD', available_at = clock_timestamp()
                WHERE id = %s RETURNING id, available_at
                """,
                [run_id, row["price"], observed_at, latest_quote["id"]],
            ).fetchone()
        confirm_price_fact(connection, "quote", quote_fact["id"], quote_fact["available_at"], run_id)
