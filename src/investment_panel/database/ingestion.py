"""Idempotent PostgreSQL ingestion for archived payloads and normalized facts."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.price_bar_ingestion import store_price_bars as _store_price_bars
from investment_panel.database.price_fact_versions import confirm_price_fact, lock_price_fact
from investment_panel.database.ingestion_coerce import (
    aware_datetime as _aware_datetime,
    calendar_date as _date,
    integer as _integer,
    number as _number,
)
from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.option_snapshot_freshness import latest_option_snapshot_by_symbol
from investment_panel.database.source_registry import set_source_enabled, sync_research_source_enablement
@dataclass
class IngestionRun:
    """One ingestion lifecycle with exactly one terminal state."""

    repository: "IngestionRepository"
    id: UUID
    finalized: bool = False

    def finish(
        self,
        status: str = "succeeded",
        *,
        item_count: int | None = None,
        instrument_count: int | None = None,
        failure_detail: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if self.finalized:
            raise ValueError(f"ingestion run is already finalized: {self.id}")
        self.repository.finish_run(
            self.id,
            status,
            item_count=item_count,
            instrument_count=instrument_count,
            failure_detail=failure_detail,
            summary=summary,
        )
        self.finalized = True
class IngestionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
    def register_source(
        self,
        source_id: str,
        *,
        name: str,
        family: str,
        kind: str,
        origin: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        with self.runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ingest.source (id, name, family, kind, origin, capabilities, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name, family = EXCLUDED.family, kind = EXCLUDED.kind,
                    origin = EXCLUDED.origin,
                    capabilities = EXCLUDED.capabilities,
                    enabled = true,
                    updated_at = now()
                """,
                [source_id, name, family, kind, origin, Jsonb(capabilities or {})],
            )

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        set_source_enabled(self.runtime, source_id, enabled)

    def sync_research_source_enablement(
        self,
        *,
        news_ids: Sequence[str],
        blog_sources: Sequence[tuple[str, str]],
        news_enabled: bool,
        blogs_enabled: bool,
        x_enabled: bool,
    ) -> None:
        sync_research_source_enablement(
            self.runtime, news_ids=news_ids, blog_sources=blog_sources,
            news_enabled=news_enabled, blogs_enabled=blogs_enabled, x_enabled=x_enabled,
        )

    def option_universe(self, configured: Sequence[dict[str, Any]] = ()) -> list[str]:
        with self.runtime.read() as connection:
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
                    WHERE catalyst.starts_at >= now() AND catalyst.starts_at < now() + interval '90 days'
                    GROUP BY regexp_replace(upper(instrument.symbol), '[.]+$', '')
                )
                SELECT i.symbol, p.instrument_id IS NOT NULL AS is_owned, w.watch_state,
                       coalesce(source_signal.source_roots, 0) AS source_roots,
                       upcoming_catalyst.starts_at
                FROM canonical_instruments i
                LEFT JOIN app.portfolio_position p ON p.instrument_id = i.id
                LEFT JOIN app.watchlist_item w ON w.instrument_id = i.id
                LEFT JOIN source_signal ON source_signal.symbol = i.symbol
                LEFT JOIN upcoming_catalyst ON upcoming_catalyst.symbol = i.symbol
                WHERE p.instrument_id IS NOT NULL OR w.instrument_id IS NOT NULL
                   OR (i.asset_class IN ('equity', 'etf') AND
                       (source_signal.symbol IS NOT NULL OR upcoming_catalyst.symbol IS NOT NULL))
                ORDER BY (p.instrument_id IS NOT NULL) DESC,
                         (w.watch_state IS NOT NULL AND w.watch_state <> 'excluded') DESC,
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
        return output

    def latest_option_snapshot_by_symbol(self, source_id: str, symbols: Sequence[str]) -> dict[str, datetime]:
        return latest_option_snapshot_by_symbol(self.runtime, source_id, symbols)

    def store_quotes(self, run_id: UUID, source_id: str, rows: Sequence[dict[str, Any]]) -> int:
        stored = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in rows:
                try:
                    symbol = canonical_symbol(source.get("symbol"))
                except ValueError:
                    continue
                observed_at = _aware_datetime(source.get("observed_at") or source.get("time"))
                price = _number(source.get("price") if "price" in source else source.get("close"))
                if not symbol or observed_at is None or price is None:
                    continue
                instrument_id = reconcile_instrument(
                    connection,
                    symbol,
                    name=source.get("name") or symbol,
                    asset_class=source.get("asset_class"),
                    category="quote",
                )
                change_abs = _number(source.get("change_abs"))
                change_pct = _number(source.get("change_pct") if "change_pct" in source else source.get("change"))
                currency = str(source.get("currency") or "USD")
                lock_price_fact(connection, "quote", instrument_id, source_id, observed_at)
                latest = connection.execute(
                    """
                    SELECT quote.id, quote.available_at, quote.price, quote.change_abs,
                           quote.change_pct, quote.currency, price_run.status AS run_status
                    FROM raw.quote quote
                    JOIN ingest.run price_run ON price_run.id = quote.ingest_run_id
                    WHERE quote.instrument_id = %s AND quote.source_id = %s AND quote.observed_at = %s
                    FOR UPDATE
                    """,
                    [instrument_id, source_id, observed_at],
                ).fetchone()
                current_fact = (price, change_abs, change_pct, currency)
                if latest is not None and tuple(latest[key] for key in ("price", "change_abs", "change_pct", "currency")) == current_fact:
                    if latest["run_status"] == "failed":
                        connection.execute(
                            "UPDATE raw.quote SET ingest_run_id = %s WHERE id = %s",
                            [run_id, latest["id"]],
                        )
                    confirm_price_fact(connection, "quote", latest["id"], latest["available_at"], run_id)
                    stored += 1
                    continue
                if latest is None:
                    fact = connection.execute(
                        """
                        INSERT INTO raw.quote
                            (instrument_id, source_id, ingest_run_id, observed_at, price,
                             change_abs, change_pct, currency)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id, available_at
                        """,
                        [instrument_id, source_id, run_id, observed_at, price,
                         change_abs, change_pct, currency],
                    )
                else:
                    connection.execute("INSERT INTO raw.quote_history SELECT * FROM raw.quote WHERE id = %s", [latest["id"]])
                    fact = connection.execute(
                        """
                        UPDATE raw.quote SET ingest_run_id = %s, price = %s, change_abs = %s,
                            change_pct = %s, currency = %s, available_at = clock_timestamp()
                        WHERE id = %s
                        RETURNING id, available_at
                        """,
                        [run_id, price, change_abs, change_pct, currency, latest["id"]],
                    ).fetchone()
                if latest is None:
                    fact = fact.fetchone()
                confirm_price_fact(connection, "quote", fact["id"], fact["available_at"], run_id)
                stored += 1
        return stored

    def store_price_bars(
        self,
        run_id: UUID,
        source_id: str,
        rows: Sequence[dict[str, Any]],
        *,
        asset_classes: dict[str, str] | None = None,
    ) -> int:
        return _store_price_bars(self.runtime, run_id, source_id, rows, asset_classes)

    def store_fundamental_observations(
        self,
        run_id: UUID,
        source_id: str,
        metric_set: str,
        rows: Sequence[dict[str, Any]],
    ) -> int:
        stored = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in rows:
                try:
                    symbol = canonical_symbol(source.get("symbol"))
                except ValueError:
                    continue
                observed_at = _aware_datetime(source.get("observed_at"))
                period_end = _date(source.get("period_end") or observed_at)
                values = source.get("values")
                if not symbol or observed_at is None or period_end is None or not isinstance(values, dict):
                    continue
                name = str(source.get("name") or symbol).strip() or symbol
                asset_class = str(source.get("asset_class") or "equity")
                instrument_id = reconcile_instrument(
                    connection,
                    symbol,
                    name=name,
                    asset_class=asset_class,
                    category="fundamentals",
                )
                connection.execute(
                    """
                    INSERT INTO raw.fundamental_observation
                        (instrument_id, source_id, ingest_run_id, metric_set,
                         period_end, observed_at, values)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, source_id, metric_set, period_end, observed_at)
                    DO UPDATE SET ingest_run_id = EXCLUDED.ingest_run_id,
                        values = EXCLUDED.values
                    """,
                    [instrument_id, source_id, run_id, metric_set, period_end, observed_at, Jsonb(values)],
                )
                stored += 1
        return stored

    @contextmanager
    def run(
        self,
        source_id: str,
        capability: str,
        *,
        source_run_key: str | None = None,
        started_at: datetime | None = None,
    ) -> Iterator[IngestionRun]:
        run_id = self.start_run(source_id, capability, source_run_key=source_run_key, started_at=started_at)
        run = IngestionRun(self, run_id)
        try:
            yield run
        except Exception as exc:
            if not run.finalized:
                run.finish("failed", failure_detail=f"{type(exc).__name__}: {exc}")
            raise
        else:
            if not run.finalized:
                run.finish()

    def start_run(
        self,
        source_id: str,
        capability: str,
        *,
        source_run_key: str | None = None,
        started_at: datetime | None = None,
    ) -> UUID:
        with self.runtime.transaction() as connection:
            if source_run_key:
                existing = connection.execute(
                    "SELECT id, status FROM ingest.run WHERE source_id = %s AND source_run_key = %s",
                    [source_id, source_run_key],
                ).fetchone()
                if existing:
                    if existing["status"] == "running":
                        return UUID(str(existing["id"]))
                    raise ValueError(f"ingestion run already finalized: {source_id}/{source_run_key}")
            row = connection.execute(
                """
                INSERT INTO ingest.run (source_id, source_run_key, capability, started_at, status)
                VALUES (%s, %s, %s, %s, 'running') RETURNING id
                """,
                [source_id, source_run_key, capability, started_at or datetime.now(UTC)],
            ).fetchone()
        return UUID(str(row["id"]))

    def finish_run(
        self,
        run_id: UUID,
        status: str,
        *,
        item_count: int | None = None,
        instrument_count: int | None = None,
        failure_detail: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"succeeded", "partial", "failed", "skipped"}:
            raise ValueError("finished ingestion status is invalid")
        with self.runtime.transaction() as connection:
            result = connection.execute(
                """
                UPDATE ingest.run
                SET status = %s, finished_at = now(),
                    item_count = COALESCE(%s, item_count),
                    instrument_count = COALESCE(%s, instrument_count),
                    failure_detail = %s,
                    summary = summary || %s
                WHERE id = %s AND status = 'running'
                """,
                [status, item_count, instrument_count, failure_detail, Jsonb(summary or {}), run_id],
            )
            if result.rowcount != 1:
                raise ValueError(f"ingestion run is not running: {run_id}")

    def record_payload(
        self,
        run_id: UUID,
        archive_uri: str,
        *,
        sha256: str,
        byte_count: int,
        encoding: str = "json",
        schema_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        digest = sha256.lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO ingest.payload
                    (run_id, archive_uri, sha256, encoding, byte_count, schema_version, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO UPDATE
                SET metadata = ingest.payload.metadata || EXCLUDED.metadata
                RETURNING id
                """,
                [run_id, archive_uri, digest, encoding, byte_count, schema_version, Jsonb(metadata or {})],
            ).fetchone()
        return int(row["id"])

    def record_payload_file(
        self,
        run_id: UUID,
        archive_path: str | Path,
        **metadata: Any,
    ) -> int:
        path = Path(archive_path)
        digest = hashlib.sha256()
        byte_count = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
        return self.record_payload(
            run_id,
            path.resolve().as_uri(),
            sha256=digest.hexdigest(),
            byte_count=byte_count,
            metadata=metadata,
        )

    def store_option_snapshot(
        self,
        run_id: UUID,
        *,
        source_id: str,
        observed_at: datetime,
        market_session: str,
        universe: str,
        rows: Sequence[dict[str, Any]],
        payload_id: int | None = None,
        completeness: float | None = None,
        collection_profile: str = "radar",
        history_symbol: str | None = None,
        slot_at: datetime | None = None,
        capture_started_at: datetime | None = None,
        capture_finished_at: datetime | None = None,
        expected_contract_count: int | None = None,
        received_contract_count: int | None = None,
        capture_state: str = "complete",
        capture_generation_id: int | None = None,
        quote_observed_at: datetime | None = None,
    ) -> dict[str, int]:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if market_session not in {"premarket", "regular", "afterhours", "closed", "unknown"}:
            raise ValueError("market_session is invalid")
        if collection_profile not in {"radar", "history_full"}:
            raise ValueError("collection_profile is invalid")
        if capture_state not in {"running", "complete", "partial", "failed"}:
            raise ValueError("capture_state is invalid")
        normalized = [_normalize_option_row(row) for row in rows]
        partition = _partition_name(observed_at.date())
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0))")
            connection.execute(
                sql.SQL("CREATE TABLE IF NOT EXISTS raw.{} PARTITION OF raw.option_quote FOR VALUES FROM ({}) TO ({})").format(
                    sql.Identifier(partition),
                    sql.Literal(_month_start(observed_at.date())),
                    sql.Literal(_next_month(observed_at.date())),
                )
            )
            snapshot = connection.execute(
                """
                INSERT INTO raw.option_snapshot
                    (source_id, ingest_run_id, payload_id, observed_at, trading_date,
                     market_session, universe, completeness, contract_count, collection_profile,
                     history_symbol, slot_at, capture_started_at, capture_finished_at,
                     expected_contract_count, received_contract_count, capture_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, observed_at, universe) DO UPDATE
                SET ingest_run_id = EXCLUDED.ingest_run_id,
                    payload_id = COALESCE(EXCLUDED.payload_id, raw.option_snapshot.payload_id),
                    market_session = EXCLUDED.market_session,
                    completeness = EXCLUDED.completeness,
                    contract_count = EXCLUDED.contract_count,
                    collection_profile = EXCLUDED.collection_profile,
                    history_symbol = EXCLUDED.history_symbol,
                    slot_at = EXCLUDED.slot_at,
                    capture_started_at = EXCLUDED.capture_started_at,
                    capture_finished_at = EXCLUDED.capture_finished_at,
                    expected_contract_count = EXCLUDED.expected_contract_count,
                    received_contract_count = EXCLUDED.received_contract_count,
                    capture_state = EXCLUDED.capture_state
                RETURNING id
                """,
                [
                    source_id, run_id, payload_id, observed_at, observed_at.date(), market_session,
                    universe, completeness, len(normalized), collection_profile, history_symbol,
                    slot_at, capture_started_at, capture_finished_at, expected_contract_count,
                    received_contract_count, capture_state,
                ],
            ).fetchone()
            snapshot_id = int(snapshot["id"])
            if normalized:
                _stage_option_rows(connection, normalized)
                for symbol in sorted({str(row["underlying_symbol"]) for row in normalized}):
                    reconcile_instrument(
                        connection, symbol, name=symbol, category="option-underlying"
                    )
                connection.execute(
                    """
                    INSERT INTO catalog.option_contract
                        (underlying_instrument_id, expiration, strike, option_type, multiplier, provider_symbols)
                    SELECT DISTINCT i.id, s.expiration, s.strike, s.option_type, s.multiplier,
                           CASE WHEN s.provider_symbol IS NULL THEN '{}'::jsonb
                                ELSE jsonb_build_object(%s::text, s.provider_symbol) END
                    FROM option_quote_stage s
                    JOIN catalog.instrument i ON i.symbol = s.underlying_symbol
                    ON CONFLICT (underlying_instrument_id, expiration, strike, option_type, multiplier)
                    DO UPDATE SET provider_symbols = catalog.option_contract.provider_symbols || EXCLUDED.provider_symbols
                    """,
                    [source_id],
                )
                connection.execute(
                    """
                    INSERT INTO raw.option_quote
                        (observed_at, snapshot_id, capture_generation_id, contract_id, underlying_price, bid, ask, mid, last,
                         bid_size, ask_size, last_trade_at, captured_at, market_data_status, volume, open_interest, provider_iv, provider_delta, provider_gamma,
                         provider_theta, provider_vega, previous_close, provider_rho,
                         chance_of_profit_long, chance_of_profit_short, provider_updated_at, provider_payload,
                         capture_group_key, group_started_at, group_finished_at, provider_observed_at,
                         available_at, underlying_observed_at, underlying_available_at)
                    SELECT %s, %s, %s, c.id, s.underlying_price, s.bid, s.ask, s.mid, s.last,
                           s.bid_size, s.ask_size, s.last_trade_at, s.captured_at, s.market_data_status, s.volume, s.open_interest, s.provider_iv, s.provider_delta,
                           s.provider_gamma, s.provider_theta, s.provider_vega, s.previous_close,
                           s.provider_rho, s.chance_of_profit_long, s.chance_of_profit_short,
                           s.provider_updated_at, s.provider_payload, s.capture_group_key,
                           s.group_started_at, s.group_finished_at, s.provider_observed_at,
                           coalesce(s.available_at, %s), s.underlying_observed_at, s.underlying_available_at
                    FROM option_quote_stage s
                    JOIN catalog.instrument i ON i.symbol = s.underlying_symbol
                    JOIN catalog.option_contract c
                      ON c.underlying_instrument_id = i.id
                     AND c.expiration = s.expiration AND c.strike = s.strike
                     AND c.option_type = s.option_type AND c.multiplier = s.multiplier
                    ON CONFLICT (snapshot_id, contract_id, observed_at) DO UPDATE
                    SET underlying_price = EXCLUDED.underlying_price,
                        bid = EXCLUDED.bid, ask = EXCLUDED.ask, mid = EXCLUDED.mid,
                        last = EXCLUDED.last, volume = EXCLUDED.volume,
                        bid_size = EXCLUDED.bid_size, ask_size = EXCLUDED.ask_size,
                        last_trade_at = EXCLUDED.last_trade_at,
                        captured_at = EXCLUDED.captured_at,
                        market_data_status = EXCLUDED.market_data_status,
                        open_interest = EXCLUDED.open_interest, provider_iv = EXCLUDED.provider_iv,
                        provider_delta = EXCLUDED.provider_delta, provider_gamma = EXCLUDED.provider_gamma,
                        provider_theta = EXCLUDED.provider_theta, provider_vega = EXCLUDED.provider_vega,
                        previous_close = EXCLUDED.previous_close, provider_rho = EXCLUDED.provider_rho,
                        chance_of_profit_long = EXCLUDED.chance_of_profit_long,
                        chance_of_profit_short = EXCLUDED.chance_of_profit_short,
                        provider_updated_at = EXCLUDED.provider_updated_at,
                        provider_payload = EXCLUDED.provider_payload,
                        capture_group_key = EXCLUDED.capture_group_key,
                        group_started_at = EXCLUDED.group_started_at,
                        group_finished_at = EXCLUDED.group_finished_at,
                        provider_observed_at = EXCLUDED.provider_observed_at,
                        available_at = EXCLUDED.available_at,
                        underlying_observed_at = EXCLUDED.underlying_observed_at,
                        underlying_available_at = EXCLUDED.underlying_available_at
                    """,
                    [quote_observed_at or observed_at, snapshot_id, capture_generation_id, observed_at],
                )
            connection.execute(
                "UPDATE ingest.run SET item_count = %s, instrument_count = %s WHERE id = %s",
                [len(normalized), len({row["underlying_symbol"] for row in normalized}), run_id],
            )
        return {"snapshot_id": snapshot_id, "contract_count": len(normalized)}


def _stage_option_rows(connection: Any, rows: Sequence[dict[str, Any]]) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE option_quote_stage (
            underlying_symbol TEXT NOT NULL, expiration DATE NOT NULL, strike NUMERIC(20, 6) NOT NULL,
            option_type TEXT NOT NULL, multiplier INTEGER NOT NULL, provider_symbol TEXT,
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


def _normalize_option_row(row: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "underlying_symbol": symbol,
        "expiration": expiration,
        "strike": strike,
        "option_type": option_type,
        "multiplier": int(row.get("multiplier") or 100),
        "provider_symbol": str(row.get("provider_symbol") or row.get("contract_symbol") or "").strip() or None,
        "underlying_price": _number(row.get("underlying_price")),
        "bid": _number(row.get("bid")),
        "ask": _number(row.get("ask")),
        "mid": _number(row.get("mid")),
        "last": _number(row.get("last")),
        "bid_size": _integer(row.get("bid_size")),
        "ask_size": _integer(row.get("ask_size")),
        "last_trade_at": _aware_datetime(row.get("last_trade_at")),
        "captured_at": _aware_datetime(row.get("captured_at")),
        "market_data_status": str(row.get("market_data_status") or row.get("market_data") or "").lower() or None,
        "volume": _integer(row.get("volume")),
        "open_interest": _integer(row.get("open_interest")),
        "provider_iv": _number(row.get("provider_iv") if "provider_iv" in row else row.get("iv")),
        "provider_delta": _number(row.get("provider_delta") if "provider_delta" in row else row.get("delta")),
        "provider_gamma": _number(row.get("provider_gamma") if "provider_gamma" in row else row.get("gamma")),
        "provider_theta": _number(row.get("provider_theta") if "provider_theta" in row else row.get("theta")),
        "provider_vega": _number(row.get("provider_vega") if "provider_vega" in row else row.get("vega")),
        "previous_close": _number(row.get("previous_close") if "previous_close" in row else row.get("close")),
        "provider_rho": _number(row.get("provider_rho") if "provider_rho" in row else row.get("rho")),
        "chance_of_profit_long": _number(row.get("chance_of_profit_long")),
        "chance_of_profit_short": _number(row.get("chance_of_profit_short")),
        "provider_updated_at": _aware_datetime(row.get("provider_updated_at") or row.get("updated_at")),
        "provider_payload": Jsonb(dict(row.get("provider_payload") or {})),
        "capture_group_key": str(row.get("capture_group_key") or f"{expiration}:{option_type}"),
        "group_started_at": _aware_datetime(row.get("group_started_at")),
        "group_finished_at": _aware_datetime(row.get("group_finished_at")),
        "provider_observed_at": _aware_datetime(row.get("provider_observed_at") or row.get("provider_updated_at") or row.get("updated_at")),
        "available_at": _aware_datetime(row.get("available_at") or row.get("captured_at")),
        "underlying_observed_at": _aware_datetime(row.get("underlying_observed_at")),
        "underlying_available_at": _aware_datetime(row.get("underlying_available_at")),
    }


def _partition_name(day: date) -> str:
    return f"option_quote_{day.year:04d}{day.month:02d}"


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)
