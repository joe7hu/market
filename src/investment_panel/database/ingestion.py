"""Idempotent PostgreSQL ingestion for archived payloads and normalized facts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
import hashlib
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


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
                INSERT INTO ingest.source (id, name, family, kind, origin, capabilities)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name, family = EXCLUDED.family, kind = EXCLUDED.kind,
                    origin = EXCLUDED.origin, capabilities = EXCLUDED.capabilities,
                    updated_at = now()
                """,
                [source_id, name, family, kind, origin, Jsonb(capabilities or {})],
            )

    def option_universe(self, configured: Sequence[dict[str, Any]] = ()) -> list[str]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT i.symbol
                FROM catalog.instrument i
                LEFT JOIN app.portfolio_position p ON p.instrument_id = i.id
                LEFT JOIN app.watchlist_item w ON w.instrument_id = i.id
                WHERE p.instrument_id IS NOT NULL
                   OR (w.instrument_id IS NOT NULL AND w.watch_state <> 'excluded')
                ORDER BY (p.instrument_id IS NOT NULL) DESC, i.symbol
                """
            ).fetchall()
        output = [str(row["symbol"]) for row in rows]
        seen = set(output)
        for item in configured:
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                output.append(symbol)
        return output

    def latest_option_snapshot_by_symbol(self, source_id: str, symbols: Sequence[str]) -> dict[str, datetime]:
        normalized = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
        if not normalized:
            return {}
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT i.symbol, max(q.observed_at) AS observed_at
                FROM raw.option_quote q
                JOIN raw.option_snapshot s ON s.id = q.snapshot_id
                JOIN catalog.option_contract c ON c.id = q.contract_id
                JOIN catalog.instrument i ON i.id = c.underlying_instrument_id
                WHERE s.source_id = %s AND i.symbol = ANY(%s)
                GROUP BY i.symbol
                """,
                [source_id, normalized],
            ).fetchall()
        return {str(row["symbol"]): row["observed_at"] for row in rows}

    def store_quotes(self, run_id: UUID, source_id: str, rows: Sequence[dict[str, Any]]) -> int:
        stored = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in rows:
                symbol = str(source.get("symbol") or "").strip().upper()
                observed_at = _aware_datetime(source.get("observed_at") or source.get("time"))
                price = _number(source.get("price") if "price" in source else source.get("close"))
                if not symbol or observed_at is None or price is None:
                    continue
                instrument = connection.execute(
                    """
                    INSERT INTO catalog.instrument (symbol, name, asset_class, category)
                    VALUES (%s, %s, 'equity', 'quote')
                    ON CONFLICT (symbol) DO UPDATE SET updated_at = now()
                    RETURNING id
                    """,
                    [symbol, symbol],
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO raw.quote
                        (instrument_id, source_id, ingest_run_id, observed_at, price,
                         change_abs, change_pct, currency)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, source_id, observed_at) DO UPDATE
                    SET ingest_run_id = EXCLUDED.ingest_run_id, price = EXCLUDED.price,
                        change_abs = EXCLUDED.change_abs, change_pct = EXCLUDED.change_pct,
                        currency = EXCLUDED.currency
                    """,
                    [
                        instrument["id"], source_id, run_id, observed_at, price,
                        _number(source.get("change_abs")), _number(source.get("change_pct") if "change_pct" in source else source.get("change")),
                        str(source.get("currency") or "USD"),
                    ],
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
    ) -> Iterator[UUID]:
        run_id = self.start_run(source_id, capability, source_run_key=source_run_key, started_at=started_at)
        try:
            yield run_id
        except Exception as exc:
            self.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
            raise
        else:
            self.finish_run(run_id, "succeeded")

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
    ) -> dict[str, int]:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if market_session not in {"premarket", "regular", "afterhours", "closed", "unknown"}:
            raise ValueError("market_session is invalid")
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
                     market_session, universe, completeness, contract_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, observed_at, universe) DO UPDATE
                SET ingest_run_id = EXCLUDED.ingest_run_id,
                    payload_id = COALESCE(EXCLUDED.payload_id, raw.option_snapshot.payload_id),
                    market_session = EXCLUDED.market_session,
                    completeness = EXCLUDED.completeness,
                    contract_count = EXCLUDED.contract_count
                RETURNING id
                """,
                [source_id, run_id, payload_id, observed_at, observed_at.date(), market_session, universe, completeness, len(normalized)],
            ).fetchone()
            snapshot_id = int(snapshot["id"])
            if normalized:
                _stage_option_rows(connection, normalized)
                connection.execute(
                    """
                    INSERT INTO catalog.instrument (symbol, name, asset_class, category)
                    SELECT DISTINCT underlying_symbol, underlying_symbol, 'equity', 'option-underlying'
                    FROM option_quote_stage
                    ON CONFLICT (symbol) DO NOTHING
                    """
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
                        (observed_at, snapshot_id, contract_id, underlying_price, bid, ask, mid, last,
                         volume, open_interest, provider_iv, provider_delta, provider_gamma,
                         provider_theta, provider_vega)
                    SELECT %s, %s, c.id, s.underlying_price, s.bid, s.ask, s.mid, s.last,
                           s.volume, s.open_interest, s.provider_iv, s.provider_delta,
                           s.provider_gamma, s.provider_theta, s.provider_vega
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
                        open_interest = EXCLUDED.open_interest, provider_iv = EXCLUDED.provider_iv,
                        provider_delta = EXCLUDED.provider_delta, provider_gamma = EXCLUDED.provider_gamma,
                        provider_theta = EXCLUDED.provider_theta, provider_vega = EXCLUDED.provider_vega
                    """,
                    [observed_at, snapshot_id],
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
            mid DOUBLE PRECISION, last DOUBLE PRECISION, volume BIGINT, open_interest BIGINT,
            provider_iv DOUBLE PRECISION, provider_delta DOUBLE PRECISION,
            provider_gamma DOUBLE PRECISION, provider_theta DOUBLE PRECISION,
            provider_vega DOUBLE PRECISION
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
    symbol = str(row.get("underlying_symbol") or row.get("symbol") or row.get("ticker") or "").strip().upper()
    option_type = str(row.get("option_type") or row.get("type") or "").strip().lower()
    if not symbol:
        raise ValueError("option row requires underlying_symbol")
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
        "volume": _integer(row.get("volume")),
        "open_interest": _integer(row.get("open_interest")),
        "provider_iv": _number(row.get("provider_iv") if "provider_iv" in row else row.get("iv")),
        "provider_delta": _number(row.get("provider_delta") if "provider_delta" in row else row.get("delta")),
        "provider_gamma": _number(row.get("provider_gamma") if "provider_gamma" in row else row.get("gamma")),
        "provider_theta": _number(row.get("provider_theta") if "provider_theta" in row else row.get("theta")),
        "provider_vega": _number(row.get("provider_vega") if "provider_vega" in row else row.get("vega")),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _partition_name(day: date) -> str:
    return f"option_quote_{day.year:04d}{day.month:02d}"


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)
