"""PostgreSQL-owned full-chain option history, analytics, and query contracts."""
from __future__ import annotations
from datetime import UTC, date, datetime, timedelta
import math
from statistics import fmean, pstdev
from typing import Any, Sequence
from uuid import UUID
from psycopg.types.json import Jsonb
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.options_history_v3 import OptionHistoryV3Materializer
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
HISTORY_PROFILE = "history_full"
FEATURE_VERSION = "history-v2"
MIN_HISTORY_SAMPLES = 20
MIN_RESIDUAL_POINTS = 10
MIN_RESIDUAL_ABS_DELTA = 0.05
MAX_RESIDUAL_ABS_DELTA = 0.95
MAX_RESIDUAL_SPREAD_PCT = 0.50
class OptionHistoryRepository:
    """The single read/write owner for complete historical option snapshots."""
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.ingestion = IngestionRepository(runtime)
        self.v3 = OptionHistoryV3Materializer(runtime)
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
                ON CONFLICT (source_id, observed_at, universe) DO UPDATE
                SET ingest_run_id = EXCLUDED.ingest_run_id, capture_started_at = now(),
                    capture_finished_at = NULL, capture_state = 'running'
                WHERE raw.option_snapshot.collection_profile = 'history_full'
                  AND raw.option_snapshot.capture_state IN ('partial', 'failed')
                RETURNING id
                """,
                [source_id, run_id, slot_at, slot_at.date(), universe, HISTORY_PROFILE, symbol, slot_at],
            ).fetchone()
            if claimed is None:
                return None
            snapshot_id = int(claimed["id"])
            generation = connection.execute(
                """
                INSERT INTO raw.option_capture_generation
                    (snapshot_id, ingest_run_id, generation, capture_state, capture_started_at)
                SELECT %s, %s, coalesce(max(generation), 0) + 1, 'running', now()
                FROM raw.option_capture_generation
                WHERE snapshot_id = %s
                RETURNING id
                """,
                [snapshot_id, run_id, snapshot_id],
            ).fetchone()
            if generation is None:
                return None
        return snapshot_id
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
        generation = self._generation_for_run(source_id=source_id, symbol=symbol, slot_at=slot_at, run_id=run_id)
        if generation is None:
            raise ValueError("capture generation was not claimed")
        started_at = _as_utc(captured.get("capture_started_at")) or slot_at
        finished_at = _as_utc(captured.get("capture_finished_at")) or datetime.now(UTC)
        for row in rows:
            option_type = str(row.get("option_type") or row.get("type") or "").lower()
            expiration = str(row.get("expiration") or row.get("expiry") or "")[:10]
            row.setdefault("capture_group_key", f"{expiration}:{option_type}")
            row.setdefault("group_started_at", started_at)
            row.setdefault("group_finished_at", finished_at)
            row.setdefault("available_at", finished_at)
            row.setdefault("provider_observed_at", row.get("provider_updated_at") or finished_at)
            row.setdefault("underlying_observed_at", finished_at)
            row.setdefault("underlying_available_at", finished_at)
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
            capture_started_at=started_at,
            capture_finished_at=finished_at,
            expected_contract_count=expected,
            received_contract_count=received,
            capture_state=state,
            capture_generation_id=generation,
            quote_observed_at=finished_at + timedelta(microseconds=generation),
        )
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                UPDATE raw.option_capture_generation
                SET capture_state = %s, expected_contract_count = %s, received_contract_count = %s,
                    completeness = %s, capture_started_at = %s, capture_finished_at = %s,
                    terminal_error = %s, diagnostics = diagnostics || %s
                WHERE id = %s AND capture_state = 'running'
                """,
                [state, expected, received, completeness, started_at, finished_at, "; ".join(errors) or None,
                 Jsonb({"quote_diagnostics": dict(captured.get("quote_diagnostics") or {})}), generation],
            )
            connection.execute(
                """
                UPDATE raw.option_snapshot
                SET capture_state = %s, capture_finished_at = %s, completeness = %s,
                    expected_contract_count = %s, received_contract_count = %s, contract_count = %s,
                    latest_complete_generation_id = CASE WHEN %s = 'complete' THEN %s ELSE latest_complete_generation_id END
                WHERE id = %s
                """,
                [state, finished_at, completeness, expected, received, len(rows), state, generation, snapshot["snapshot_id"]],
            )
        result = {
            **snapshot,
            "symbol": symbol,
            "slot_at": slot_at.isoformat(),
            "expected_contract_count": expected,
            "received_contract_count": received,
            "completeness": completeness,
            "capture_state": state,
            "capture_generation_id": generation,
            "errors": errors,
            "quote_diagnostics": dict(captured.get("quote_diagnostics") or {}),
        }
        if complete:
            result.update(self.materialize_snapshot(int(snapshot["snapshot_id"])))
        return result
    def fail_capture(self, *, source_id: str, symbol: str, slot_at: datetime, run_id: UUID, error: Exception | str) -> None:
        """Terminate a claimed generation so no slot remains permanently running."""
        detail = str(error)
        universe = _history_universe(symbol)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            generation = connection.execute(
                """
                SELECT generation.id, snapshot.id AS snapshot_id
                FROM raw.option_capture_generation generation
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE snapshot.source_id = %s AND snapshot.history_symbol = %s AND snapshot.slot_at = %s
                  AND generation.ingest_run_id = %s AND generation.capture_state = 'running'
                FOR UPDATE
                """,
                [source_id, symbol.upper(), slot_at, run_id],
            ).fetchone()
            if generation is None:
                return
            connection.execute(
                "UPDATE raw.option_capture_generation SET capture_state = 'failed', capture_finished_at = now(), terminal_error = %s WHERE id = %s",
                [detail, generation["id"]],
            )
            connection.execute(
                "UPDATE raw.option_snapshot SET capture_state = 'failed', capture_finished_at = now() WHERE id = %s",
                [generation["snapshot_id"]],
            )
    def _generation_for_run(self, *, source_id: str, symbol: str, slot_at: datetime, run_id: UUID) -> int | None:
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT generation.id
                FROM raw.option_capture_generation generation
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE snapshot.source_id = %s AND snapshot.history_symbol = %s AND snapshot.slot_at = %s
                  AND generation.ingest_run_id = %s
                """,
                [source_id, symbol.upper(), slot_at, run_id],
            ).fetchone()
        return int(row["id"]) if row else None
    def materialize_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        """Write only immutable v3 evidence; v2 rows are rollback-only diagnostics."""
        with self.runtime.read(JOB_PROFILE) as connection:
            current = connection.execute(
                "SELECT latest_complete_generation_id FROM raw.option_snapshot WHERE id = %s", [snapshot_id]
            ).fetchone()
        if current is None or current["latest_complete_generation_id"] is None:
            return {"surface_summaries": 0, "anomalies": 0, "relative_values": 0}
        v3 = self.v3.materialize(snapshot_id=snapshot_id, capture_generation_id=int(current["latest_complete_generation_id"]))
        return {"anomalies": 0, **v3}
    def snapshots(self, *, symbol: str = "QQQ", offset: int = 0, limit: int = 100, include_partial: bool = False) -> dict[str, Any]:
        filters = ["snapshot.history_symbol = %s", "snapshot.collection_profile = %s"]
        parameters: list[Any] = [symbol.upper(), HISTORY_PROFILE]
        if not include_partial:
            filters.append("snapshot.latest_complete_generation_id IS NOT NULL")
        where = " AND ".join(filters)
        with self.runtime.read() as connection:
            count = connection.execute(f"SELECT count(*) AS count FROM raw.option_snapshot snapshot WHERE {where}", parameters).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT snapshot.id AS snapshot_id, snapshot.history_symbol AS symbol, snapshot.slot_at, snapshot.observed_at,
                       coalesce(generation.capture_started_at, snapshot.capture_started_at) AS capture_started_at,
                       coalesce(generation.capture_finished_at, snapshot.capture_finished_at) AS capture_finished_at,
                       coalesce(generation.expected_contract_count, snapshot.expected_contract_count) AS expected_contract_count,
                       coalesce(generation.received_contract_count, snapshot.received_contract_count) AS received_contract_count,
                       coalesce(generation.completeness, snapshot.completeness) AS completeness,
                       coalesce(generation.capture_state, snapshot.capture_state) AS capture_state,
                       snapshot.contract_count, snapshot.latest_complete_generation_id AS capture_generation_id
                FROM raw.option_snapshot snapshot
                LEFT JOIN raw.option_capture_generation generation ON generation.id = snapshot.latest_complete_generation_id
                WHERE {where}
                ORDER BY snapshot.slot_at DESC NULLS LAST, snapshot.observed_at DESC LIMIT %s OFFSET %s
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

    def legacy_surface(self, *, symbol: str = "QQQ", snapshot: int | None = None, option_type: str | None = None) -> dict[str, Any]:
        snapshot_id = self._resolve_snapshot(symbol, snapshot)
        if snapshot_id is None:
            return {"snapshot_id": None, "symbol": symbol.upper(), "x": [], "y": [], "surfaces": {}, "observed": []}
        rows, _ = self._chain_rows(snapshot_id, offset=0, limit=50_000)
        by_type = [option_type] if option_type else ["call", "put"]
        x_values = sorted({float(row["log_moneyness"]) for row in rows if row.get("log_moneyness") is not None})
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

    def surface(
        self, *, symbol: str = "QQQ", snapshot: int | None = None, expiration: date, option_type: str
    ) -> dict[str, Any]:
        """Return one bounded expiry/type evidence series, never a full-chain grid."""

        snapshot_id = self._resolve_snapshot(symbol, snapshot)
        empty = {
            "snapshot_id": snapshot_id, "symbol": symbol.upper(), "expiration": expiration,
            "option_type": option_type, "observed": [], "fitted": [], "uncertainty": [],
            "fit_status": "collecting", "diagnostics": {"blockers": ["no_complete_capture"]},
        }
        if snapshot_id is None:
            return empty
        rows, _ = self._chain_rows(snapshot_id, expiration=expiration, option_type=option_type, offset=0, limit=200)
        if not rows:
            return empty
        with self.runtime.read() as connection:
            summary = connection.execute(
                """
                SELECT surface.fit_status, surface.fit_rmse, surface.metrics, surface.group_duration_seconds,
                       surface.max_quote_age_seconds, surface.eligible_point_count
                FROM analysis.option_surface_summary surface
                JOIN analysis.run run ON run.id = surface.analysis_run_id
                WHERE surface.snapshot_id = %s AND surface.expiration = %s AND surface.option_type = %s
                  AND surface.analysis_run_id IS NOT NULL
                ORDER BY run.finished_at DESC NULLS LAST LIMIT 1
                """, [snapshot_id, expiration, option_type],
            ).fetchone()
            values = connection.execute(
                """
                SELECT DISTINCT ON (value.contract_id) value.contract_id, value.fair_low, value.fair_high,
                       value.classification, value.modeled_net_edge, value.blockers
                FROM analysis.option_relative_value value
                JOIN analysis.run run ON run.id = value.analysis_run_id
                JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE generation.snapshot_id = %s
                  AND generation.id = snapshot.latest_complete_generation_id
                ORDER BY value.contract_id, run.finished_at DESC NULLS LAST
                """, [snapshot_id],
            ).fetchall()
        by_contract = {int(value["contract_id"]): dict(value) for value in values}
        observed = [
            {"contract_id": row["contract_id"], "strike": row["strike"], "bid": row["bid"], "ask": row["ask"],
             "mid": row["mid"], "provider_iv": row["provider_iv"], "provider_delta": row["provider_delta"],
             "observed_at": row["provider_observed_at"], "available_at": row["available_at"]}
            for row in rows
        ]
        fitted = [
            {"contract_id": row["contract_id"], "strike": row["strike"], "fair_low": value["fair_low"],
             "fair_high": value["fair_high"], "classification": value["classification"],
             "modeled_net_edge": value["modeled_net_edge"], "blockers": value["blockers"]}
            for row in rows if (value := by_contract.get(int(row["contract_id"]))) is not None
        ]
        diagnostics = dict(summary["metrics"] or {}) if summary else {"blockers": ["v3_summary_missing"]}
        if summary:
            diagnostics.update({key: summary[key] for key in ("fit_rmse", "group_duration_seconds", "max_quote_age_seconds", "eligible_point_count")})
        return {
            "snapshot_id": snapshot_id, "symbol": symbol.upper(), "expiration": expiration,
            "option_type": option_type, "observed": observed, "fitted": fitted,
            "uncertainty": [{"contract_id": item["contract_id"], "fair_low": item["fair_low"], "fair_high": item["fair_high"]} for item in fitted],
            "fit_status": summary["fit_status"] if summary else "collecting", "diagnostics": diagnostics,
        }

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
                WITH latest_v3 AS (
                    SELECT DISTINCT ON (summary.capture_generation_id, summary.expiration, summary.option_type)
                           summary.*
                    FROM analysis.option_surface_summary summary
                    JOIN analysis.run run ON run.id = summary.analysis_run_id
                    WHERE summary.snapshot_id = %s AND summary.analysis_run_id IS NOT NULL
                    ORDER BY summary.capture_generation_id, summary.expiration, summary.option_type,
                             run.finished_at DESC NULLS LAST
                )
                SELECT summary.expiration, summary.option_type, summary.dte, summary.atm_iv, summary.delta_25_iv, summary.skew_25,
                       summary.smile_slope, summary.smile_curvature, summary.term_slope
                FROM latest_v3 summary
                ORDER BY summary.expiration, summary.option_type
                """, [snapshot_id]
            ).fetchall()
            history = connection.execute(
                """
                WITH latest_v3 AS (
                    SELECT DISTINCT ON (summary.capture_generation_id, summary.expiration, summary.option_type)
                           summary.*, run.finished_at
                    FROM analysis.option_surface_summary summary
                    JOIN analysis.run run ON run.id = summary.analysis_run_id
                    WHERE summary.analysis_run_id IS NOT NULL
                    ORDER BY summary.capture_generation_id, summary.expiration, summary.option_type,
                             run.finished_at DESC NULLS LAST
                )
                SELECT snapshot.slot_at, summary.expiration, summary.option_type, summary.dte, summary.atm_iv,
                       summary.delta_25_iv, summary.skew_25, summary.term_slope
                FROM latest_v3 summary
                JOIN raw.option_snapshot snapshot ON snapshot.id = summary.snapshot_id
                WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = %s
                  AND snapshot.latest_complete_generation_id IS NOT NULL
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
                       anomaly.anomaly_type, 'legacy' AS state, anomaly.observed_value, anomaly.expected_value,
                       anomaly.z_score, anomaly.details, anomaly.created_at, contract.strike
                FROM analysis.option_history_anomaly anomaly
                LEFT JOIN catalog.option_contract contract ON contract.id = anomaly.contract_id
                WHERE anomaly.snapshot_id = %s
                LIMIT %s OFFSET %s
                """, [snapshot_id, limit, offset]
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit, "snapshot_id": snapshot_id}

    def health(self) -> dict[str, Any]:
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS snapshots,
                       count(*) FILTER (WHERE latest_complete_generation_id IS NOT NULL) AS complete_snapshots,
                       max(slot_at) FILTER (WHERE latest_complete_generation_id IS NOT NULL) AS latest_complete_slot,
                       avg(completeness) FILTER (WHERE latest_complete_generation_id IS NOT NULL) AS average_completeness,
                       coalesce((
                           SELECT sum(pg_total_relation_size(inhrelid))
                           FROM pg_inherits
                           WHERE inhparent = 'raw.option_quote'::regclass
                       ), 0)::bigint AS option_quote_bytes,
                       coalesce(pg_total_relation_size('analysis.option_surface_summary'), 0) AS surface_summary_bytes
                FROM raw.option_snapshot WHERE collection_profile = %s
                """, [HISTORY_PROFILE]
            ).fetchone()
            v3 = connection.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON ((run.summary->>'capture_generation_id')::bigint) run.*
                    FROM analysis.run run
                    WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                    ORDER BY (run.summary->>'capture_generation_id')::bigint, run.finished_at DESC NULLS LAST
                )
                SELECT count(*) AS runs,
                       coalesce(sum((summary->>'solver_failures')::integer), 0) AS solver_failures,
                       coalesce(sum((summary->>'fit_attempts')::integer), 0) AS fit_attempts,
                       coalesce(sum((summary->>'succeeded_groups')::integer), 0) AS succeeded_groups
                FROM latest
                """
            ).fetchone()
            shadows = connection.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status = 'entered') AS entered,
                       count(*) FILTER (WHERE status = 'unfilled') AS unfilled,
                       count(*) FILTER (WHERE status = 'pending') AS pending
                FROM analysis.shadow_trade WHERE source_kind = 'system'
                """
            ).fetchone()
        history_bytes = int(row["option_quote_bytes"]) + int(row["surface_summary_bytes"])
        v3_payload = dict(v3)
        fit_attempts = int(v3_payload["fit_attempts"])
        succeeded_groups = int(v3_payload["succeeded_groups"])
        return {
            **dict(row), "storage_bytes": history_bytes, "retention_days": 730,
            "v3_runs": int(v3_payload["runs"]), "v3_succeeded_runs": int(v3_payload["runs"]),
            "solver_failures": int(v3_payload["solver_failures"]), "solver_success_rate": succeeded_groups / fit_attempts if fit_attempts else None,
            "shadow": {key: int(value) for key, value in dict(shadows).items()},
            "canary": {"required_regular_sessions": 5, "completed_sessions": int(row["complete_snapshots"]), "paper_mode_eligible": fit_attempts > 0 and succeeded_groups / fit_attempts >= 0.99 and int(row["complete_snapshots"]) >= 5},
        }

    def _resolve_snapshot(self, symbol: str, snapshot: int | None) -> int | None:
        with self.runtime.read() as connection:
            if snapshot is not None:
                row = connection.execute(
                    """SELECT id FROM raw.option_snapshot WHERE id = %s AND history_symbol = %s
                       AND collection_profile = %s AND latest_complete_generation_id IS NOT NULL""",
                    [snapshot, symbol.upper(), HISTORY_PROFILE],
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT id FROM raw.option_snapshot WHERE history_symbol = %s AND collection_profile = %s
                       AND latest_complete_generation_id IS NOT NULL ORDER BY slot_at DESC NULLS LAST, observed_at DESC LIMIT 1""",
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
        filters = ["quote.snapshot_id = %s", "quote.capture_generation_id = snapshot.latest_complete_generation_id"]
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
                   quote.chance_of_profit_long, quote.chance_of_profit_short, quote.market_data_status,
                   quote.capture_generation_id, quote.capture_group_key, quote.group_started_at,
                   quote.group_finished_at, quote.provider_observed_at, quote.available_at,
                   quote.underlying_observed_at, quote.underlying_available_at
        """
        with self.runtime.read() as connection:
            count = connection.execute(f"SELECT count(*) AS count {base}", parameters).fetchone()["count"]
            rows = connection.execute(
                f"{select} {base} ORDER BY contract.expiration, contract.option_type, contract.strike LIMIT %s OFFSET %s",
                [*parameters, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], int(count)

    def _insert_anomalies(
        self, connection: Any, snapshot_id: int, slot_at: datetime, rows: list[dict[str, Any]], summaries: list[dict[str, Any]]
    ) -> int:
        created = 0
        by_curve = {(summary["expiration"], summary["option_type"]): summary for summary in summaries}
        residuals_by_curve: dict[tuple[Any, Any], list[tuple[dict[str, Any], float, float]]] = {}
        for row in rows:
            summary = by_curve.get((row["expiration"], row["option_type"]))
            iv, x = row.get("provider_iv"), row.get("log_moneyness")
            if summary is None or iv is None or x is None or summary["atm_iv"] is None or not _residual_eligible(row):
                continue
            expected = summary["atm_iv"] + (summary["smile_slope"] or 0) * x + (summary["smile_curvature"] or 0) * x * x
            key = (row["expiration"], row["option_type"])
            residuals_by_curve.setdefault(key, []).append((row, float(iv) - expected, expected))
        for residuals in residuals_by_curve.values():
            if len(residuals) < MIN_RESIDUAL_POINTS:
                continue
            sigma = pstdev([entry[1] for entry in residuals])
            if sigma <= 0:
                continue
            for row, residual, expected in residuals:
                z_score = residual / sigma
                if abs(z_score) < 2.5:
                    continue
                connection.execute(
                    """INSERT INTO analysis.option_history_anomaly
                        (snapshot_id, contract_id, expiration, option_type, anomaly_type, state,
                         observed_value, expected_value, z_score, details)
                        VALUES (%s, %s, %s, %s, 'smile_residual', 'active', %s, %s, %s, %s)""",
                    [snapshot_id, row["contract_id"], row["expiration"], row["option_type"], row["provider_iv"], expected, z_score, Jsonb({"label": "cross-sectional IV residual within the current expiry/type smile; not a trade recommendation"})],
                )
                created += 1
        history_count = connection.execute(
            """SELECT count(*) AS count FROM raw.option_snapshot
                WHERE collection_profile = %s AND capture_state = 'complete' AND slot_at < %s""",
            [HISTORY_PROFILE, slot_at],
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
                           AND snapshot.slot_at < %s
                           AND summary.{field} IS NOT NULL ORDER BY snapshot.slot_at DESC LIMIT %s""",
                    [
                        summary["symbol"], HISTORY_PROFILE, summary["expiration"], summary["option_type"],
                        snapshot_id, slot_at, MIN_HISTORY_SAMPLES,
                    ],
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
    for expiration, option_type in sorted({(row["expiration"], row["option_type"]) for row in rows}):
        curve = [row for row in rows if row["expiration"] == expiration and row["option_type"] == option_type and row.get("provider_iv") is not None and row.get("log_moneyness") is not None]
        if not curve:
            continue
        curve.sort(key=lambda row: abs(float(row["log_moneyness"])))
        atm = curve[0]
        fitted_curve = [row for row in curve if _residual_eligible(row)]
        if len(fitted_curve) < MIN_RESIDUAL_POINTS:
            fitted_curve = curve
        xs = [float(row["log_moneyness"]) for row in fitted_curve]
        ys = [float(row["provider_iv"]) for row in fitted_curve]
        slope = _linear_slope(xs, ys)
        curvature = _curvature(xs, ys)
        spreads = [((row["ask"] - row["bid"]) / row["mid"]) for row in curve if row.get("ask") is not None and row.get("bid") is not None and row.get("mid") and row["mid"] > 0]
        sizes = [float((row.get("bid_size") or 0) + (row.get("ask_size") or 0)) for row in curve]
        summaries.append({
            "symbol": str(atm["symbol"]), "expiration": expiration, "option_type": option_type, "dte": int(atm["dte"]),
            "atm_iv": float(atm["provider_iv"]), "delta_25_iv": _closest_delta_iv(curve, 0.25), "skew_25": None,
            "smile_slope": slope, "smile_curvature": curvature, "term_slope": None,
            "average_spread_pct": fmean(spreads) if spreads else None,
            "liquidity_score": math.log1p(fmean(sizes)) if sizes else 0.0,
            "metrics": {"observed_strikes": len(curve), "iv_points": len(ys), "fitted_iv_points": len(fitted_curve), "provider": "robinhood"},
        })
    by_expiration = {(summary["expiration"], summary["option_type"]): summary for summary in summaries}
    for expiration in {summary["expiration"] for summary in summaries}:
        call = by_expiration.get((expiration, "call"))
        put = by_expiration.get((expiration, "put"))
        if call and put:
            skew_25 = _difference(call["delta_25_iv"], put["delta_25_iv"])
            call["skew_25"] = skew_25
            put["skew_25"] = skew_25
    for option_type in {summary["option_type"] for summary in summaries}:
        term = sorted((summary for summary in summaries if summary["option_type"] == option_type), key=lambda summary: summary["dte"])
        for index, summary in enumerate(term):
            neighbors = term[max(0, index - 1): min(len(term), index + 2)]
            summary["term_slope"] = _linear_slope(
                [float(point["dte"]) for point in neighbors], [float(point["atm_iv"]) for point in neighbors]
            )
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


def _residual_eligible(row: dict[str, Any]) -> bool:
    delta = row.get("provider_delta")
    bid, ask, mid = row.get("bid"), row.get("ask"), row.get("mid")
    if delta is None or bid is None or ask is None or mid is None:
        return False
    if not MIN_RESIDUAL_ABS_DELTA <= abs(float(delta)) <= MAX_RESIDUAL_ABS_DELTA:
        return False
    if bid < 0 or ask < bid or mid <= 0:
        return False
    return ((ask - bid) / mid) <= MAX_RESIDUAL_SPREAD_PCT


def _difference(value: float | None, previous: float | None) -> float | None:
    return (value - previous) if value is not None and previous is not None else None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _history_universe(symbol: str) -> str:
    return f"history_full:{symbol.upper()}"
