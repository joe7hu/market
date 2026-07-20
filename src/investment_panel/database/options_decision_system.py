"""Bounded read models for the QQQ-only, paper-only decision surface."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.analysis.history_v3 import static_arbitrage_findings
from investment_panel.core.robinhood_options.collector import RobinhoodClient, _payload_list, option_quote_row
from investment_panel.database.runtime import DatabaseRuntime


class OptionsDecisionSystemRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def decision_brief(self, *, symbol: str = "QQQ", lane: str = "thesis") -> dict[str, Any]:
        with self.runtime.read() as connection:
            latest = connection.execute(
                """
                SELECT run.id, run.summary, run.finished_at
                FROM analysis.run run
                JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                  AND snapshot.history_symbol = %s
                ORDER BY run.finished_at DESC NULLS LAST LIMIT 1
                """,
                [symbol.upper()],
            ).fetchone()
            if latest is None:
                return _empty_brief(symbol, lane, "No complete v3 capture is available yet.")
            classes = ["relative_cheap", "historical_static_arbitrage_candidate"] if lane == "anomaly" else ["relative_cheap"]
            candidate = connection.execute(
                """
                SELECT value.id, value.classification, value.fair_low, value.fair_high, value.modeled_net_edge,
                       value.confidence, value.blockers, value.evidence, contract.expiration, contract.strike,
                       contract.option_type, snapshot.id AS snapshot_id, generation.id AS capture_generation_id
                FROM analysis.option_relative_value value
                JOIN catalog.option_contract contract ON contract.id = value.contract_id
                JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE value.analysis_run_id = %s AND value.classification = ANY(%s)
                ORDER BY value.modeled_net_edge DESC NULLS LAST, value.id LIMIT 1
                """,
                [latest["id"], classes],
            ).fetchone()
        return {
            "symbol": symbol.upper(), "lane": lane, "mode": "shadow", "analysis_run_id": str(latest["id"]),
            "as_of": latest["finished_at"], "state": "WATCH" if candidate else "COLLECTING",
            "summary": dict(latest["summary"] or {}), "strongest_candidate": _candidate_payload(dict(candidate)) if candidate else None,
            "paper_only": True,
        }

    def candidates(
        self,
        *,
        symbol: str = "QQQ",
        lane: str | None = None,
        paper_state: str | None = None,
        structure: str | None = None,
        expiration: date | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        filters = ["instrument.symbol = %s", "option_decision.paper_state IS NOT NULL"]
        values: list[Any] = [symbol.upper()]
        if lane:
            filters.append("option_decision.discovery_lane = %s")
            values.append(lane)
        if paper_state:
            filters.append("option_decision.paper_state = %s")
            values.append(paper_state)
        if structure:
            filters.append("option_decision.structure = %s")
            values.append(structure)
        if expiration:
            filters.append("contract.expiration = %s")
            values.append(expiration)
        where = " AND ".join(filters)
        with self.runtime.read() as connection:
            count = connection.execute(
                f"""SELECT count(*) AS count FROM analysis.decision decision
                    JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                    JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                    JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                    WHERE {where}""",
                values,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT decision.id::text AS decision_id, decision.as_of, decision.reasons, decision.blockers,
                       option_decision.paper_state, option_decision.discovery_lane, option_decision.structure,
                       option_decision.max_loss, option_decision.entry_price, option_decision.expected_value,
                       option_decision.data_confidence, option_decision.execution_confidence,
                       option_decision.fair_low, option_decision.fair_high, option_decision.modeled_net_edge,
                       option_decision.market_regime, option_decision.model_version,
                       contract.expiration, contract.strike, contract.option_type
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                WHERE {where}
                ORDER BY decision.as_of DESC, option_decision.modeled_net_edge DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                [*values, limit, offset],
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit}

    def relative_values(
        self,
        *,
        symbol: str = "QQQ",
        snapshot: int | None = None,
        classification: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        if snapshot is None:
            with self.runtime.read() as connection:
                current = connection.execute(
                    """SELECT id FROM raw.option_snapshot WHERE history_symbol = %s
                       AND latest_complete_generation_id IS NOT NULL ORDER BY slot_at DESC NULLS LAST LIMIT 1""",
                    [symbol.upper()],
                ).fetchone()
            if current is None:
                return {"rows": [], "count": 0, "offset": offset, "limit": limit}
            snapshot = int(current["id"])
        filters = ["snapshot.history_symbol = %s", "snapshot.id = %s"]
        values: list[Any] = [symbol.upper(), snapshot]
        if classification:
            filters.append("value.classification = %s")
            values.append(classification)
        where = " AND ".join(filters)
        with self.runtime.read() as connection:
            count = connection.execute(
                f"""WITH latest AS (
                        SELECT run.id FROM analysis.run run
                        JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                        WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                          AND generation.snapshot_id = %s
                        ORDER BY run.finished_at DESC NULLS LAST LIMIT 1
                    )
                    SELECT count(*) AS count FROM analysis.option_relative_value value
                    JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                    JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                    WHERE {where}
                      AND value.analysis_run_id = (SELECT id FROM latest)""", [snapshot, *values],
            ).fetchone()["count"]
            rows = connection.execute(
                f"""WITH latest AS (
                        SELECT run.id FROM analysis.run run
                        JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                        WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                          AND generation.snapshot_id = %s
                        ORDER BY run.finished_at DESC NULLS LAST LIMIT 1
                    )
                SELECT value.id, value.analysis_run_id::text AS analysis_run_id, value.capture_generation_id,
                       value.classification, value.fair_low, value.fair_high, value.modeled_net_edge,
                       value.edge_side, value.confidence, value.quality_status, value.blockers, value.evidence,
                       contract.id AS contract_id, contract.expiration, contract.strike, contract.option_type,
                       snapshot.id AS snapshot_id
                FROM analysis.option_relative_value value
                JOIN catalog.option_contract contract ON contract.id = value.contract_id
                JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE {where}
                  AND value.analysis_run_id = (SELECT id FROM latest)
                ORDER BY value.modeled_net_edge DESC NULLS LAST, value.id
                LIMIT %s OFFSET %s
                """, [snapshot, *values, limit, offset],
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit}

    def paper_journal(self, *, symbol: str = "QQQ", offset: int = 0, limit: int = 100) -> dict[str, Any]:
        with self.runtime.read() as connection:
            count = connection.execute(
                """SELECT count(*) AS count FROM analysis.shadow_trade shadow
                    JOIN analysis.decision decision ON decision.id = shadow.decision_id
                    JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                    WHERE instrument.symbol = %s""", [symbol.upper()],
            ).fetchone()["count"]
            rows = connection.execute(
                """
                SELECT shadow.id::text AS shadow_id, shadow.decision_id::text AS decision_id, shadow.status,
                       shadow.entry_at, shadow.entry_price, shadow.exit_at, shadow.exit_price,
                       shadow.pending_entry_reason, shadow.entry_cohort_id, shadow.structure,
                       shadow.market_regime, shadow.fill_basis, shadow.source_kind, shadow.metrics
                FROM analysis.shadow_trade shadow
                JOIN analysis.decision decision ON decision.id = shadow.decision_id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE instrument.symbol = %s
                ORDER BY coalesce(shadow.entry_at, shadow.created_at) DESC
                LIMIT %s OFFSET %s
                """, [symbol.upper(), limit, offset],
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit}

    def verification_result(self, candidate_id: int, client: RobinhoodClient | None = None) -> dict[str, Any]:
        """Read through to a live, size/skew-aware package re-quote and persist the result."""

        context = self._verification_context(candidate_id)
        if context is None:
            raise ValueError("relative-value candidate not found")
        if context["classification"] != "historical_static_arbitrage_candidate":
            return self._record_verification(candidate_id, "rejected", ["not_static_arbitrage_candidate"], {})
        if client is None:
            return self._record_verification(candidate_id, "unavailable", ["live_requote_required"], {})
        try:
            equity_payload = client.get_equity_quotes([context["symbol"]])
            equity_rows = _payload_list(equity_payload, "results")
            equity = dict(equity_rows[0].get("quote") or {}) if equity_rows else {}
            spot = _number(equity.get("last_trade_price") or equity.get("adjusted_mark_price"))
            instruments = [row["instrument"] for row in context["rows"] if row.get("instrument")]
            payload = client.get_option_quotes(instruments)
            quotes = {str((item.get("quote") or {}).get("instrument_id") or item.get("instrument_id") or ""): dict(item.get("quote") or {}) for item in _payload_list(payload, "results")}
            live_rows: list[dict[str, Any]] = []
            timestamps = [_timestamp(equity.get("updated_at"))]
            blockers: list[str] = []
            for stored in context["rows"]:
                quote = quotes.get(str(stored["instrument"]))
                if quote is None:
                    blockers.append("missing_live_leg")
                    continue
                row = option_quote_row(stored["provider_instrument"], quote)
                if row is None:
                    blockers.append("malformed_live_leg")
                    continue
                row["contract_id"] = stored["contract_id"]
                row["bid_size"] = _number(quote.get("bid_size"))
                row["ask_size"] = _number(quote.get("ask_size"))
                live_rows.append(row)
                timestamps.append(_timestamp(quote.get("updated_at")))
                if row.get("bid") is None or row.get("ask") is None or row["ask"] < row["bid"]:
                    blockers.append("crossed_or_missing_live_leg")
                if (row["bid_size"] is not None and row["bid_size"] < 1) or (row["ask_size"] is not None and row["ask_size"] < 1):
                    blockers.append("displayed_size_unavailable")
            usable_times = [value for value in timestamps if value is not None]
            if spot is None:
                blockers.append("missing_live_underlying")
            if len(usable_times) != len(timestamps) or (usable_times and (max(usable_times) - min(usable_times)).total_seconds() > 5):
                blockers.append("live_package_timestamp_skew")
            findings = static_arbitrage_findings(live_rows, spot=spot, option_type=context["option_type"]) if not blockers else []
            expected = set(context["contract_ids"])
            verified = any(expected.issubset(set(finding["contract_ids"])) for finding in findings)
            if not verified and not blockers:
                blockers.append("live_worst_side_edge_not_present")
            status = "verified" if verified and not blockers else "rejected"
            return self._record_verification(
                candidate_id, status, sorted(set(blockers)),
                {"spot": spot, "live_findings": findings, "checked_at": datetime.now(UTC).isoformat(), "paper_only": True},
            )
        except Exception as exc:
            return self._record_verification(candidate_id, "unavailable", ["live_requote_unavailable"], {"error": f"{type(exc).__name__}: {exc}"})

    def _verification_context(self, candidate_id: int) -> dict[str, Any] | None:
        with self.runtime.read() as connection:
            candidate = connection.execute(
                """
                SELECT value.id, value.classification, value.evidence, contract.option_type,
                       snapshot.history_symbol AS symbol, value.capture_generation_id
                FROM analysis.option_relative_value value
                JOIN catalog.option_contract contract ON contract.id = value.contract_id
                JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE value.id = %s
                """, [candidate_id],
            ).fetchone()
            if candidate is None:
                return None
            evidence = dict(candidate["evidence"] or {})
            findings = [item for item in evidence.get("static_findings", []) if isinstance(item, dict)]
            contract_ids = sorted({int(contract_id) for item in findings for contract_id in item.get("contract_ids", [])})
            if not contract_ids:
                contract_ids = [connection.execute("SELECT contract_id FROM analysis.option_relative_value WHERE id = %s", [candidate_id]).fetchone()["contract_id"]]
            rows = connection.execute(
                """
                SELECT quote.contract_id, quote.provider_payload
                FROM raw.option_quote quote
                WHERE quote.capture_generation_id = %s AND quote.contract_id = ANY(%s)
                """, [candidate["capture_generation_id"], contract_ids],
            ).fetchall()
        packed = []
        for row in rows:
            payload = dict(row["provider_payload"] or {})
            instrument = dict(payload.get("instrument") or {})
            instrument_id = instrument.get("id")
            if instrument_id:
                packed.append({"contract_id": int(row["contract_id"]), "instrument": str(instrument_id), "provider_instrument": instrument})
        return {**dict(candidate), "contract_ids": contract_ids, "rows": packed}

    def _record_verification(self, candidate_id: int, status: str, blockers: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
        with self.runtime.transaction() as connection:
            connection.execute(
                """INSERT INTO analysis.option_relative_value_verification (relative_value_id, status, blockers, evidence)
                   VALUES (%s, %s, %s, %s)""", [candidate_id, status, blockers, Jsonb(evidence)]
            )
        return {
            "candidate_id": candidate_id, "verified": status == "verified", "status": status,
            "classification": "verified_static_arbitrage_candidate" if status == "verified" else "historical_static_arbitrage_candidate",
            "blockers": blockers, "paper_only": True,
        }


def _candidate_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "relative_value_id": value["id"], "classification": value["classification"],
        "fair_low": value["fair_low"], "fair_high": value["fair_high"],
        "modeled_net_edge": value["modeled_net_edge"], "confidence": value["confidence"],
        "blockers": value["blockers"], "expiration": value["expiration"], "strike": float(value["strike"]),
        "option_type": value["option_type"], "snapshot_id": value["snapshot_id"],
        "capture_generation_id": value["capture_generation_id"], "paper_only": True,
    }


def _empty_brief(symbol: str, lane: str, message: str) -> dict[str, Any]:
    return {"symbol": symbol.upper(), "lane": lane, "mode": "shadow", "analysis_run_id": None,
            "as_of": None, "state": "COLLECTING", "summary": {"message": message},
            "strongest_candidate": None, "paper_only": True}


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
