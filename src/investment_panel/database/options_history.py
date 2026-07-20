"""PostgreSQL-owned full-chain option history, analytics, and query contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
import math
from statistics import fmean, pstdev
from typing import Any, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


HISTORY_PROFILE = "history_full"
FEATURE_VERSION = "history-v1"
MIN_HISTORY_SAMPLES = 20


class OptionHistoryRepository:
    """The single read/write owner for complete historical option snapshots."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.ingestion = IngestionRepository(runtime)

    def claim_slot(self, *, source_id: str, symbol: str, slot_at: datetime, run_id: UUID) -> int | None:
        """Claim one symbol/slot without allowing overlapping collection work."""

        universe = _history_universe(symbol)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            claimed = connection.execute(
                """
                INSERT INTO raw.option_snapshot
                    (source_id, ingest_run_id, observed_at, trading_date, market_session, universe,
                     collection_profile, history_symbol, slot_at, capture_started_at, capture_state)
                VALUES (%s, %s, %s, %s, 'regular', %s, %s, %s, %s, now(), 'running')
                ON CONFLICT (source_id, observed_at, universe) DO NOTHING
                RETURNING id
                """,
                [source_id, run_id, slot_at, slot_at.date(), universe, HISTORY_PROFILE, symbol, slot_at],
            ).fetchone()
            if claimed:
                return int(claimed["id"])
            retried = connection.execute(
                """
                UPDATE raw.option_snapshot
                SET ingest_run_id = %s, capture_started_at = now(), capture_finished_at = NULL,
                    capture_state = 'running'
                WHERE source_id = %s AND observed_at = %s AND universe = %s
                  AND collection_profile = %s AND capture_state IN ('partial', 'failed')
                RETURNING id
                """,
                [run_id, source_id, slot_at, universe, HISTORY_PROFILE],
            ).fetchone()
        return int(retried["id"]) if retried else None

    def store_capture(
        self,
        *,
        run_id: UUID,
        source_id: str,
        symbol: str,
        slot_at: datetime,
        captured: dict[str, Any],
        minimum_completeness: float = 0.98,
    ) -> dict[str, Any]:
        rows = list(captured.get("rows") or [])
        expected = int(captured.get("expected_contract_count") or 0)
        received = int(captured.get("received_contract_count") or len(rows))
        completeness = (received / expected) if expected else 0.0
        errors = [str(error) for error in captured.get("errors") or []]
        complete = expected > 0 and completeness >= minimum_completeness and not errors and not captured.get("timed_out")
        state = "complete" if complete else "partial"
        snapshot = self.ingestion.store_option_snapshot(
            run_id,
            source_id=source_id,
            observed_at=slot_at,
            market_session="regular",
            universe=_history_universe(symbol),
            rows=rows,
            completeness=completeness,
            collection_profile=HISTORY_PROFILE,
            history_symbol=symbol,
            slot_at=slot_at,
            capture_started_at=_as_utc(captured.get("capture_started_at")),
            capture_finished_at=_as_utc(captured.get("capture_finished_at")),
            expected_contract_count=expected,
            received_contract_count=received,
            capture_state=state,
        )
        result = {
            **snapshot,
            "symbol": symbol,
            "slot_at": slot_at.isoformat(),
            "expected_contract_count": expected,
            "received_contract_count": received,
            "completeness": completeness,
            "capture_state": state,
            "errors": errors,
            "quote_diagnostics": dict(captured.get("quote_diagnostics") or {}),
        }
        if complete:
            result.update(self.materialize_snapshot(int(snapshot["snapshot_id"])))
        return result

    def materialize_snapshot(self, snapshot_id: int) -> dict[str, int]:
        """Persist summary/anomaly features only for a complete historical snapshot."""

        rows = self._snapshot_rows(snapshot_id)
        if not rows:
            return {"surface_summaries": 0, "anomalies": 0}
        summaries = _surface_summaries(rows)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("DELETE FROM analysis.option_surface_summary WHERE snapshot_id = %s", [snapshot_id])
            connection.execute("DELETE FROM analysis.option_history_anomaly WHERE snapshot_id = %s", [snapshot_id])
            for summary in summaries:
                previous = connection.execute(
                    """
                    SELECT atm_iv, skew_25, term_slope
                    FROM analysis.option_surface_summary prior
                    JOIN raw.option_snapshot snapshot ON snapshot.id = prior.snapshot_id
                    WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = %s
                      AND snapshot.capture_state = 'complete' AND prior.expiration = %s
                      AND prior.option_type = %s AND prior.snapshot_id <> %s
                    ORDER BY snapshot.slot_at DESC NULLS LAST, snapshot.observed_at DESC LIMIT 1
                    """,
                    [summary["symbol"], HISTORY_PROFILE, summary["expiration"], summary["option_type"], snapshot_id],
                ).fetchone()
                summary["atm_iv_change"] = _difference(summary["atm_iv"], previous["atm_iv"] if previous else None)
                summary["skew_25_change"] = _difference(summary["skew_25"], previous["skew_25"] if previous else None)
                summary["term_slope_change"] = _difference(summary["term_slope"], previous["term_slope"] if previous else None)
                connection.execute(
                    """
                    INSERT INTO analysis.option_surface_summary
                        (snapshot_id, expiration, option_type, feature_version, dte, atm_iv, delta_25_iv, skew_25,
                         smile_slope, smile_curvature, term_slope, average_spread_pct, liquidity_score,
                         atm_iv_change, skew_25_change, term_slope_change, metrics)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        snapshot_id, summary["expiration"], summary["option_type"], FEATURE_VERSION,
                        summary["dte"], summary["atm_iv"], summary["delta_25_iv"], summary["skew_25"], summary["smile_slope"],
                        summary["smile_curvature"], summary["term_slope"], summary["average_spread_pct"],
                        summary["liquidity_score"], summary["atm_iv_change"], summary["skew_25_change"],
                        summary["term_slope_change"], Jsonb(summary["metrics"]),
                    ],
                )
            anomalies = self._insert_anomalies(connection, snapshot_id, rows, summaries)
        return {"surface_summaries": len(summaries), "anomalies": anomalies}

    def snapshots(
        self, *, symbol: str = "QQQ", offset: int = 0, limit: int = 100, include_partial: bool = False
    ) -> dict[str, Any]:
        filters = ["history_symbol = %s", "collection_profile = %s"]
        parameters: list[Any] = [symbol.upper(), HISTORY_PROFILE]
        if not include_partial:
            filters.append("capture_state = 'complete'")
        where = " AND ".join(filters)
        with self.runtime.read() as connection:
            count = connection.execute(f"SELECT count(*) AS count FROM raw.option_snapshot WHERE {where}", parameters).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT id AS snapshot_id, history_symbol AS symbol, slot_at, observed_at, capture_started_at,
                       capture_finished_at, expected_contract_count, received_contract_count, completeness,
                       capture_state, contract_count
                FROM raw.option_snapshot WHERE {where}
                ORDER BY slot_at DESC NULLS LAST, observed_at DESC LIMIT %s OFFSET %s
                """,
                [*parameters, limit, offset],
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit}

    def chain(
        self,
        *,
        symbol: str = "QQQ",
        snapshot: int | None = None,
        expiration: date | None = None,
        option_type: str | None = None,
        min_moneyness: float | None = None,
        max_moneyness: float | None = None,
        offset: int = 0,
        limit: int = 250,
    ) -> dict[str, Any]:
        snapshot_id = self._resolve_snapshot(symbol, snapshot)
        if snapshot_id is None:
            return {"rows": [], "count": 0, "offset": offset, "limit": limit, "snapshot_id": None}
        rows, count = self._chain_rows(
            snapshot_id, expiration=expiration, option_type=option_type, min_moneyness=min_moneyness,
            max_moneyness=max_moneyness, offset=offset, limit=limit,
        )
        return {"rows": rows, "count": count, "offset": offset, "limit": limit, "snapshot_id": snapshot_id}

    def surface(self, *, symbol: str = "QQQ", snapshot: int | None = None, option_type: str | None = None) -> dict[str, Any]:
        snapshot_id = self._resolve_snapshot(symbol, snapshot)
        if snapshot_id is None:
            return {"snapshot_id": None, "symbol": symbol.upper(), "x": [], "y": [], "surfaces": {}, "observed": []}
        rows, _ = self._chain_rows(snapshot_id, offset=0, limit=50_000)
        by_type = [option_type] if option_type else ["call", "put"]
        x_values = sorted({round(float(row["log_moneyness"]), 6) for row in rows if row.get("log_moneyness") is not None})
        y_values = sorted({int(row["dte"]) for row in rows})
        surfaces: dict[str, list[list[float | None]]] = {}
        for kind in by_type:
            grouped = {(int(row["dte"]), float(row["log_moneyness"])): row.get("provider_iv") for row in rows if row["option_type"] == kind and row.get("provider_iv") is not None and row.get("log_moneyness") is not None}
            surfaces[kind] = [
                [_interpolate([(x, iv) for (dte, x), iv in grouped.items() if dte == y], point) for point in x_values]
                for y in y_values
            ]
        observed = [
            {key: row.get(key) for key in ("expiration", "option_type", "dte", "log_moneyness", "provider_iv", "strike")}
            for row in rows if row.get("provider_iv") is not None
        ]
        return {"snapshot_id": snapshot_id, "symbol": symbol.upper(), "x": x_values, "y": y_values, "surfaces": surfaces, "observed": observed}

    def curves(self, *, symbol: str = "QQQ", snapshot: int | None = None, expiration: date | None = None) -> dict[str, Any]:
        snapshot_id = self._resolve_snapshot(symbol, snapshot)
        if snapshot_id is None:
            return {"snapshot_id": None, "smiles": [], "term_structure": [], "history": [], "history_state": "collecting"}
        rows, _ = self._chain_rows(snapshot_id, expiration=expiration, offset=0, limit=50_000)
        smiles: list[dict[str, Any]] = []
        for expiry, kind in sorted({(row["expiration"], row["option_type"]) for row in rows}):
            points = [
                {"moneyness": row["log_moneyness"], "iv": row["provider_iv"], "strike": row["strike"], "delta": row["provider_delta"]}
                for row in rows if row["expiration"] == expiry and row["option_type"] == kind and row.get("provider_iv") is not None
            ]
            smiles.append({"expiration": expiry, "option_type": kind, "dte": next(row["dte"] for row in rows if row["expiration"] == expiry), "points": points})
        with self.runtime.read() as connection:
            term = connection.execute(
                """
                SELECT summary.expiration, summary.option_type, summary.dte, summary.atm_iv, summary.delta_25_iv, summary.skew_25,
                       summary.smile_slope, summary.smile_curvature, summary.term_slope
                FROM analysis.option_surface_summary summary WHERE summary.snapshot_id = %s
                ORDER BY summary.expiration, summary.option_type
                """, [snapshot_id]
            ).fetchall()
            history = connection.execute(
                """
                SELECT snapshot.slot_at, summary.expiration, summary.option_type, summary.dte, summary.atm_iv,
                       summary.delta_25_iv, summary.skew_25, summary.term_slope
                FROM analysis.option_surface_summary summary
                JOIN raw.option_snapshot snapshot ON snapshot.id = summary.snapshot_id
                WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = %s
                  AND snapshot.capture_state = 'complete'
                ORDER BY snapshot.slot_at DESC LIMIT 500
                """, [symbol.upper(), HISTORY_PROFILE]
            ).fetchall()
        state = "ready" if len({row["slot_at"] for row in history}) >= MIN_HISTORY_SAMPLES else "collecting"
        return {"snapshot_id": snapshot_id, "smiles": smiles, "term_structure": [dict(row) for row in term], "history": [dict(row) for row in history], "history_state": state}

    def anomalies(self, *, symbol: str = "QQQ", snapshot: int | None = None, offset: int = 0, limit: int = 250) -> dict[str, Any]:
        snapshot_id = self._resolve_snapshot(symbol, snapshot)
        if snapshot_id is None:
            return {"rows": [], "count": 0, "offset": offset, "limit": limit, "snapshot_id": None}
        with self.runtime.read() as connection:
            count = connection.execute("SELECT count(*) AS count FROM analysis.option_history_anomaly WHERE snapshot_id = %s", [snapshot_id]).fetchone()["count"]
            rows = connection.execute(
                """
                SELECT anomaly.id, anomaly.snapshot_id, anomaly.contract_id, anomaly.expiration, anomaly.option_type,
                       anomaly.anomaly_type, anomaly.state, anomaly.observed_value, anomaly.expected_value,
                       anomaly.z_score, anomaly.details, anomaly.created_at, contract.strike
                FROM analysis.option_history_anomaly anomaly
                LEFT JOIN catalog.option_contract contract ON contract.id = anomaly.contract_id
                WHERE anomaly.snapshot_id = %s
                ORDER BY (anomaly.state = 'active') DESC, abs(anomaly.z_score) DESC NULLS LAST, anomaly.id
                LIMIT %s OFFSET %s
                """, [snapshot_id, limit, offset]
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit, "snapshot_id": snapshot_id}

    def health(self) -> dict[str, Any]:
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS snapshots,
                       count(*) FILTER (WHERE capture_state = 'complete') AS complete_snapshots,
                       max(slot_at) FILTER (WHERE capture_state = 'complete') AS latest_complete_slot,
                       avg(completeness) FILTER (WHERE capture_state = 'complete') AS average_completeness,
                       coalesce(pg_total_relation_size('raw.option_quote'), 0) AS option_quote_bytes,
                       coalesce(pg_total_relation_size('analysis.option_surface_summary'), 0) AS surface_summary_bytes
                FROM raw.option_snapshot WHERE collection_profile = %s
                """, [HISTORY_PROFILE]
            ).fetchone()
        history_bytes = int(row["option_quote_bytes"]) + int(row["surface_summary_bytes"])
        return {**dict(row), "storage_bytes": history_bytes, "retention_days": 730}

    def _resolve_snapshot(self, symbol: str, snapshot: int | None) -> int | None:
        with self.runtime.read() as connection:
            if snapshot is not None:
                row = connection.execute(
                    """SELECT id FROM raw.option_snapshot WHERE id = %s AND history_symbol = %s
                       AND collection_profile = %s AND capture_state = 'complete'""",
                    [snapshot, symbol.upper(), HISTORY_PROFILE],
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT id FROM raw.option_snapshot WHERE history_symbol = %s AND collection_profile = %s
                       AND capture_state = 'complete' ORDER BY slot_at DESC NULLS LAST, observed_at DESC LIMIT 1""",
                    [symbol.upper(), HISTORY_PROFILE],
                ).fetchone()
        return int(row["id"]) if row else None

    def _snapshot_rows(self, snapshot_id: int) -> list[dict[str, Any]]:
        rows, _ = self._chain_rows(snapshot_id, offset=0, limit=50_000)
        return rows

    def _chain_rows(
        self, snapshot_id: int, *, expiration: date | None = None, option_type: str | None = None,
        min_moneyness: float | None = None, max_moneyness: float | None = None, offset: int, limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = ["quote.snapshot_id = %s"]
        parameters: list[Any] = [snapshot_id]
        if expiration is not None:
            filters.append("contract.expiration = %s")
            parameters.append(expiration)
        if option_type is not None:
            filters.append("contract.option_type = %s")
            parameters.append(option_type)
        ratio = "ln(NULLIF(contract.strike / NULLIF(quote.underlying_price, 0), 0))"
        if min_moneyness is not None:
            filters.append(f"{ratio} >= %s")
            parameters.append(min_moneyness)
        if max_moneyness is not None:
            filters.append(f"{ratio} <= %s")
            parameters.append(max_moneyness)
        where = " AND ".join(filters)
        base = f"""
            FROM raw.option_quote quote
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            WHERE {where}
        """
        select = f"""
            SELECT quote.snapshot_id, snapshot.history_symbol AS symbol, snapshot.slot_at, contract.id AS contract_id,
                   contract.expiration, contract.strike::double precision AS strike, contract.option_type,
                   greatest(contract.expiration - snapshot.trading_date, 0) AS dte,
                   {ratio} AS log_moneyness, quote.underlying_price, quote.bid, quote.ask, quote.mid, quote.last,
                   quote.previous_close, quote.bid_size, quote.ask_size, quote.last_trade_at, quote.captured_at,
                   quote.provider_updated_at, quote.provider_iv, quote.provider_delta, quote.provider_gamma,
                   quote.provider_theta, quote.provider_vega, quote.provider_rho, quote.volume, quote.open_interest,
                   quote.chance_of_profit_long, quote.chance_of_profit_short, quote.market_data_status
        """
        with self.runtime.read() as connection:
            count = connection.execute(f"SELECT count(*) AS count {base}", parameters).fetchone()["count"]
            rows = connection.execute(
                f"{select} {base} ORDER BY contract.expiration, contract.option_type, contract.strike LIMIT %s OFFSET %s",
                [*parameters, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], int(count)

    def _insert_anomalies(self, connection: Any, snapshot_id: int, rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> int:
        created = 0
        by_curve = {(summary["expiration"], summary["option_type"]): summary for summary in summaries}
        residuals: list[tuple[dict[str, Any], float, float]] = []
        for row in rows:
            summary = by_curve.get((row["expiration"], row["option_type"]))
            iv, x = row.get("provider_iv"), row.get("log_moneyness")
            if summary is None or iv is None or x is None or summary["atm_iv"] is None:
                continue
            expected = summary["atm_iv"] + (summary["smile_slope"] or 0) * x + (summary["smile_curvature"] or 0) * x * x
            residuals.append((row, float(iv) - expected, expected))
        sigma = pstdev([entry[1] for entry in residuals]) if len(residuals) >= 3 else 0.0
        for row, residual, expected in residuals:
            z_score = residual / sigma if sigma > 0 else None
            if z_score is None or abs(z_score) < 2.5:
                continue
            connection.execute(
                """INSERT INTO analysis.option_history_anomaly
                    (snapshot_id, contract_id, expiration, option_type, anomaly_type, state,
                     observed_value, expected_value, z_score, details)
                    VALUES (%s, %s, %s, %s, 'smile_residual', 'active', %s, %s, %s, %s)""",
                [snapshot_id, row["contract_id"], row["expiration"], row["option_type"], row["provider_iv"], expected, z_score, Jsonb({"label": "cross-sectional IV residual; not a trade recommendation"})],
            )
            created += 1
        history_count = connection.execute(
            """SELECT count(*) AS count FROM raw.option_snapshot
                WHERE collection_profile = %s AND capture_state = 'complete' AND id <> %s""",
            [HISTORY_PROFILE, snapshot_id],
        ).fetchone()["count"]
        state = "active" if history_count >= MIN_HISTORY_SAMPLES else "collecting"
        for summary in summaries:
            for field, label in (("atm_iv", "atm_iv_change"), ("skew_25", "skew_change"), ("term_slope", "term_structure_change")):
                value = summary.get(field)
                if value is None:
                    continue
                historic = connection.execute(
                    f"""SELECT summary.{field} AS value FROM analysis.option_surface_summary summary
                         JOIN raw.option_snapshot snapshot ON snapshot.id = summary.snapshot_id
                         WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = %s
                           AND snapshot.capture_state = 'complete' AND summary.expiration = %s
                           AND summary.option_type = %s AND summary.snapshot_id <> %s
                           AND summary.{field} IS NOT NULL ORDER BY snapshot.slot_at DESC LIMIT %s""",
                    [summary["symbol"], HISTORY_PROFILE, summary["expiration"], summary["option_type"], snapshot_id, MIN_HISTORY_SAMPLES],
                ).fetchall()
                values = [float(row["value"]) for row in historic]
                mean = fmean(values) if values else None
                std = pstdev(values) if len(values) >= 2 else None
                z_score = ((float(value) - mean) / std) if mean is not None and std and std > 0 else None
                if state == "collecting" or (z_score is not None and abs(z_score) >= 2.5):
                    connection.execute(
                        """INSERT INTO analysis.option_history_anomaly
                            (snapshot_id, expiration, option_type, anomaly_type, state,
                             observed_value, expected_value, z_score, details)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        [snapshot_id, summary["expiration"], summary["option_type"], label, state, value, mean, z_score, Jsonb({"history_samples": len(values), "minimum_samples": MIN_HISTORY_SAMPLES, "label": "statistical history signal; not a trade recommendation"})],
                    )
                    created += 1
        return created


def _surface_summaries(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    global_atm: list[tuple[int, float]] = []
    for expiration, option_type in sorted({(row["expiration"], row["option_type"]) for row in rows}):
        curve = [row for row in rows if row["expiration"] == expiration and row["option_type"] == option_type and row.get("provider_iv") is not None and row.get("log_moneyness") is not None]
        if not curve:
            continue
        curve.sort(key=lambda row: abs(float(row["log_moneyness"])))
        atm = curve[0]
        xs = [float(row["log_moneyness"]) for row in curve]
        ys = [float(row["provider_iv"]) for row in curve]
        slope = _linear_slope(xs, ys)
        curvature = _curvature(xs, ys)
        spreads = [((row["ask"] - row["bid"]) / row["mid"]) for row in curve if row.get("ask") is not None and row.get("bid") is not None and row.get("mid") and row["mid"] > 0]
        sizes = [float((row.get("bid_size") or 0) + (row.get("ask_size") or 0)) for row in curve]
        global_atm.append((int(atm["dte"]), float(atm["provider_iv"])))
        summaries.append({
            "symbol": str(atm["symbol"]), "expiration": expiration, "option_type": option_type, "dte": int(atm["dte"]),
            "atm_iv": float(atm["provider_iv"]), "delta_25_iv": _closest_delta_iv(curve, 0.25), "skew_25": None,
            "smile_slope": slope, "smile_curvature": curvature, "term_slope": None,
            "average_spread_pct": fmean(spreads) if spreads else None,
            "liquidity_score": math.log1p(fmean(sizes)) if sizes else 0.0,
            "metrics": {"observed_strikes": len(curve), "iv_points": len(ys), "provider": "robinhood"},
        })
    by_expiration = {(summary["expiration"], summary["option_type"]): summary for summary in summaries}
    for expiration in {summary["expiration"] for summary in summaries}:
        call = by_expiration.get((expiration, "call"))
        put = by_expiration.get((expiration, "put"))
        if call and put:
            skew_25 = _difference(call["delta_25_iv"], put["delta_25_iv"])
            call["skew_25"] = skew_25
            put["skew_25"] = skew_25
    term_slope = _linear_slope([point[0] for point in global_atm], [point[1] for point in global_atm])
    for summary in summaries:
        summary["term_slope"] = term_slope
    return summaries


def _linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x, mean_y = fmean(xs), fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else None


def _curvature(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    slope = _linear_slope(xs, ys)
    if slope is None:
        return None
    residual_slope = _linear_slope([x * x for x in xs], [y - slope * x for x, y in zip(xs, ys)])
    return residual_slope


def _closest_delta_iv(rows: Sequence[dict[str, Any]], target: float) -> float | None:
    eligible = [row for row in rows if row.get("provider_delta") is not None]
    if not eligible:
        return None
    nearest = min(eligible, key=lambda row: abs(abs(float(row["provider_delta"])) - target))
    return float(nearest["provider_iv"])


def _interpolate(points: Sequence[tuple[float, Any]], x: float) -> float | None:
    cleaned = sorted((float(point_x), float(value)) for point_x, value in points if value is not None)
    if not cleaned or x < cleaned[0][0] or x > cleaned[-1][0]:
        return None
    for left, right in zip(cleaned, cleaned[1:]):
        if left[0] <= x <= right[0]:
            if right[0] == left[0]:
                return left[1]
            return left[1] + (right[1] - left[1]) * ((x - left[0]) / (right[0] - left[0]))
    return cleaned[0][1] if x == cleaned[0][0] else cleaned[-1][1]


def _difference(value: float | None, previous: float | None) -> float | None:
    return (value - previous) if value is not None and previous is not None else None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _history_universe(symbol: str) -> str:
    return f"history_full:{symbol.upper()}"
