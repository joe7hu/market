"""Bounded read models for the QQQ-only, paper-only decision surface."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.analysis.history_v3 import MODEL_REVISION, static_arbitrage_findings
from investment_panel.core.robinhood_options.collector import RobinhoodClient, _payload_list, option_quote_row
from investment_panel.core.option_underwriting import thesis_blocker, thesis_invalidation
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.options_decision_readiness import next_required_action
from investment_panel.database.options_journal import learning_progress, paper_journal, shadow_observations
from investment_panel.database.options_history_canary import canary_health
from investment_panel.database.options_decision_workspace import latest_run, workspace_payload
from investment_panel.database.options_decision_verification import candidate_finding, same_finding_identity


class OptionsDecisionSystemRepository:
    def __init__(self, runtime: DatabaseRuntime, *, mode: str = "shadow") -> None:
        self.runtime = runtime
        self.mode = mode

    def decision_brief(self, *, symbol: str = "QQQ", lane: str = "thesis") -> dict[str, Any]:
        if self.mode == "disabled":
            return _empty_brief(symbol, lane, "The options decision system is disabled.", mode=self.mode)
        with self.runtime.read() as connection:
            latest = connection.execute(
                """
                SELECT run.id, run.summary, run.finished_at
                FROM analysis.run run
                JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                  AND run.summary->>'model_revision' = %s
                  AND snapshot.history_symbol = %s
                -- Replay completion order is not market chronology.  The
                -- decision brief must continue to select the newest captured
                -- QQQ cohort after an append-only historical rematerialization.
                ORDER BY snapshot.slot_at DESC NULLS LAST, generation.id DESC,
                         run.finished_at DESC NULLS LAST LIMIT 1
                """,
                [MODEL_REVISION, symbol.upper()],
            ).fetchone()
            if latest is None:
                return _empty_brief(symbol, lane, "No post-fix v3 capture is available yet.", mode=self.mode)
            candidate = connection.execute(
                """
                SELECT decision.id::text AS decision_id, decision.reasons, decision.blockers,
                       option_decision.paper_state, option_decision.discovery_lane, option_decision.structure,
                       option_decision.entry_price, option_decision.fill_assumption,
                       option_decision.probability_profit, option_decision.expected_value, option_decision.max_loss,
                       option_decision.synthetic_legs, option_decision.details,
                       option_decision.data_confidence, option_decision.execution_confidence,
                       option_decision.market_regime, value.id AS relative_value_id, value.classification,
                       option_decision.route_version, option_decision.strategy_route,
                       option_decision.market_regime_detail, option_decision.event_state,
                       value.fair_low, value.fair_high, value.modeled_net_edge, value.confidence,
                       value.evidence, contract.expiration, contract.strike, contract.option_type,
                       option_decision.snapshot_id, value.capture_generation_id,
                       thesis.thesis AS thesis_payload, thesis.updated_at AS thesis_updated_at
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN analysis.option_relative_value value ON value.id = option_decision.relative_value_id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                LEFT JOIN app.thesis thesis ON thesis.id = option_decision.thesis_id
                WHERE decision.run_id = %s AND option_decision.discovery_lane = %s
                ORDER BY CASE option_decision.paper_state
                    WHEN 'PAPER_READY' THEN 1 WHEN 'WATCH' THEN 2 WHEN 'COLLECTING' THEN 3 ELSE 4 END,
                    option_decision.modeled_net_edge DESC NULLS LAST, decision.id
                LIMIT 1
                """,
                [latest["id"], lane],
            ).fetchone()
            readiness = _readiness(connection, latest=dict(latest), symbol=symbol.upper())
        candidate_data = dict(candidate) if candidate else None
        return {
            "symbol": symbol.upper(), "lane": lane, "mode": self.mode, "analysis_run_id": str(latest["id"]),
            "as_of": latest["finished_at"], "state": candidate_data["paper_state"] if candidate_data else "COLLECTING",
            "summary": dict(latest["summary"] or {}), "readiness": readiness,
            "strongest_candidate": _candidate_payload(candidate_data) if candidate_data else None,
            "paper_only": True,
        }

    def candidates(
        self,
        *,
        symbol: str = "QQQ",
        scope: str = "current",
        lane: str | None = None,
        paper_state: str | None = None,
        structure: str | None = None,
        expiration: date | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        filters = ["instrument.symbol = %s", "option_decision.paper_state IS NOT NULL", "option_decision.model_version = %s"]
        values: list[Any] = [symbol.upper(), MODEL_REVISION]
        latest_run_id: str | None = None
        as_of: Any = None
        capture_generation_id: int | None = None
        if scope != "history":
            latest = latest_run(self.runtime, symbol=symbol)
            if latest is None:
                return {"items": [], "total": 0, "next_cursor": None, "as_of": None, "capture_generation_id": None,
                        "model_revision": MODEL_REVISION, "scope": scope, "analysis_run_id": None,
                        "rows": [], "count": 0, "offset": offset, "limit": limit}
            latest_run_id = str(latest["id"])
            as_of = latest["finished_at"]
            capture_generation_id = (latest.get("summary") or {}).get("capture_generation_id")
            filters.append("decision.run_id = %s")
            values.append(latest_run_id)
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
                       option_decision.max_loss, option_decision.entry_price, option_decision.fill_assumption,
                       option_decision.probability_profit, option_decision.expected_value,
                       option_decision.synthetic_legs, option_decision.details,
                       option_decision.data_confidence, option_decision.execution_confidence,
                       option_decision.fair_low, option_decision.fair_high, option_decision.modeled_net_edge,
                       option_decision.market_regime, option_decision.model_version,
                       option_decision.route_version, option_decision.strategy_route,
                       option_decision.market_regime_detail, option_decision.event_state,
                       value.id AS relative_value_id, value.classification, value.confidence, value.evidence,
                       contract.expiration, contract.strike, contract.option_type,
                       thesis.thesis AS thesis_payload, thesis.updated_at AS thesis_updated_at
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                JOIN analysis.option_relative_value value ON value.id = option_decision.relative_value_id
                LEFT JOIN app.thesis thesis ON thesis.id = option_decision.thesis_id
                WHERE {where}
                ORDER BY decision.as_of DESC, option_decision.modeled_net_edge DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                [*values, limit, offset],
            ).fetchall()
        items = [_candidate_payload(dict(row)) for row in rows]
        next_cursor = str(offset + limit) if offset + len(items) < int(count) else None
        return {"items": items, "total": int(count), "next_cursor": next_cursor, "as_of": as_of,
                "capture_generation_id": capture_generation_id, "model_revision": MODEL_REVISION,
                "scope": scope, "analysis_run_id": latest_run_id,
                "rows": items, "count": int(count), "offset": offset, "limit": limit}

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
        effective_classification = """
            CASE WHEN verification.status = 'verified'
                      AND value.classification = 'historical_static_arbitrage_candidate'
                 THEN 'verified_static_arbitrage_candidate'
                 ELSE value.classification END
        """
        filters = ["snapshot.history_symbol = %s", "snapshot.id = %s"]
        values: list[Any] = [symbol.upper(), snapshot]
        if classification:
            filters.append(f"({effective_classification}) = %s")
            values.append(classification)
        where = " AND ".join(filters)
        with self.runtime.read() as connection:
            count = connection.execute(
                f"""WITH latest AS (
                        SELECT run.id FROM analysis.run run
                        JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                        WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                          AND generation.snapshot_id = %s
                          AND run.summary->>'model_revision' = %s
                        ORDER BY run.finished_at DESC NULLS LAST LIMIT 1
                    )
                    SELECT count(*) AS count FROM analysis.option_relative_value value
                    JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                    JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                    LEFT JOIN LATERAL (
                        SELECT status, verified_at
                        FROM analysis.option_relative_value_verification
                        WHERE relative_value_id = value.id
                        ORDER BY verified_at DESC, id DESC LIMIT 1
                    ) verification ON true
                    WHERE {where}
                      AND value.analysis_run_id = (SELECT id FROM latest)""", [snapshot, MODEL_REVISION, *values],
            ).fetchone()["count"]
            rows = connection.execute(
                f"""WITH latest AS (
                        SELECT run.id FROM analysis.run run
                        JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                    WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                      AND generation.snapshot_id = %s
                      AND run.summary->>'model_revision' = %s
                    ORDER BY run.finished_at DESC NULLS LAST LIMIT 1
                    )
                SELECT value.id, value.analysis_run_id::text AS analysis_run_id, value.capture_generation_id,
                       {effective_classification} AS classification,
                       verification.status AS verification_status, verification.verified_at,
                       value.fair_low, value.fair_high, value.modeled_net_edge,
                       value.edge_side, value.confidence, value.quality_status, value.blockers, value.evidence,
                       contract.id AS contract_id, contract.expiration, contract.strike, contract.option_type,
                       snapshot.id AS snapshot_id
                FROM analysis.option_relative_value value
                JOIN catalog.option_contract contract ON contract.id = value.contract_id
                JOIN raw.option_capture_generation generation ON generation.id = value.capture_generation_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                LEFT JOIN LATERAL (
                    SELECT status, verified_at
                    FROM analysis.option_relative_value_verification
                    WHERE relative_value_id = value.id
                    ORDER BY verified_at DESC, id DESC LIMIT 1
                ) verification ON true
                WHERE {where}
                  AND value.analysis_run_id = (SELECT id FROM latest)
                ORDER BY value.modeled_net_edge DESC NULLS LAST, value.id
                LIMIT %s OFFSET %s
                """, [snapshot, MODEL_REVISION, *values, limit, offset],
            ).fetchall()
        return {"rows": [dict(row) for row in rows], "count": int(count), "offset": offset, "limit": limit}

    def workspace(self, *, symbol: str = "QQQ", lane: str = "thesis") -> dict[str, Any]:
        return workspace_payload(self.runtime, symbol=symbol, lane=lane, mode=self.mode, decision_brief=self.decision_brief)

    def paper_journal(self, *, symbol: str = "QQQ", offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return paper_journal(self.runtime, symbol=symbol, offset=offset, limit=limit)

    def shadow_observations(
        self,
        *,
        symbol: str = "QQQ",
        offset: int = 0,
        limit: int = 100,
        include_legacy: bool = False,
    ) -> dict[str, Any]:
        return shadow_observations(
            self.runtime,
            symbol=symbol,
            offset=offset,
            limit=limit,
            include_legacy=include_legacy,
        )

    def learning_progress(self, *, symbol: str = "QQQ") -> dict[str, Any]:
        return learning_progress(self.runtime, symbol=symbol)

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
            verified = any(same_finding_identity(context["finding"], finding) for finding in findings)
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
            fallback_contract = connection.execute(
                "SELECT contract_id FROM analysis.option_relative_value WHERE id = %s", [candidate_id]
            ).fetchone()["contract_id"]
            finding = candidate_finding(findings, int(fallback_contract))
            contract_ids = [int(value) for value in finding.get("contract_ids", [])] if finding else [int(fallback_contract)]
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
        return {**dict(candidate), "finding": finding or {}, "contract_ids": contract_ids, "rows": packed}

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
    details = dict(value.get("details") or {})
    scenario = dict(details.get("historical_paths") or {})
    calibration = dict(details.get("calibration") or {})
    quote_package = dict(details.get("quote_package") or {})
    thesis = {**dict(value.get("thesis_payload") or {}), **dict(details.get("thesis") or {})}
    legs = [dict(leg) for leg in value.get("synthetic_legs") or []]
    fair_low, fair_high = _number(value.get("fair_low")), _number(value.get("fair_high"))
    market_regime = dict(value.get("market_regime_detail") or {})
    if not market_regime:
        market_regime = {
            "state": value.get("market_regime") or "unavailable",
            "trend_state": "unavailable",
            "quality_status": "unavailable",
            "reason_codes": ["daily_market_regime_not_materialized_for_history_run"],
        }
    strategy_route = dict(value.get("strategy_route") or {})
    if not strategy_route:
        from investment_panel.analysis.strategy_routing import ROUTE_VERSION

        strategy_route = {
            "route_version": value.get("route_version") or ROUTE_VERSION,
            "shadow": True,
            "selected_structure": "NO_TRADE",
            "alternative_structures": [],
            "trend_state": "unavailable",
            "volatility_state": "unstable",
            "event_state": value.get("event_state") or "insufficient_event_evidence",
            "selection_reasons": [],
            "rejected_structures": [],
            "route_blockers": ["daily_strategy_route_not_materialized_for_history_run"],
            "as_of": value.get("as_of").isoformat() if isinstance(value.get("as_of"), datetime) else value.get("as_of"),
            "evidence_refs": [],
            "paper_quantity_authorized": False,
            "ai_can_override": False,
        }
    return {
        "decision_id": str(value["decision_id"]), "relative_value_id": value["relative_value_id"],
        "paper_state": value["paper_state"], "discovery_lane": value["discovery_lane"],
        "structure": value["structure"], "expiration": value["expiration"],
        "strike": float(value["strike"]), "option_type": value["option_type"],
        "legs": legs,
        "conservative_entry": {
            "price": _number(value.get("entry_price")),
            "fill_basis": value.get("fill_assumption") or "worst_side_quote",
        },
        "one_unit_max_loss": _number(value.get("max_loss")),
        "fair_value_interval": {"low": fair_low, "high": fair_high},
        "expected_value_interval": {
            "expected": _number(value.get("expected_value")),
            "lower_95": _number(scenario.get("lower_95_expected_value")),
        },
        "forecast": {
            "probability_profit": _number(value.get("probability_profit")),
        },
        "uncertainty": {
            "fair_value_width": fair_high - fair_low if fair_low is not None and fair_high is not None else None,
            "data_confidence": _number(value.get("data_confidence")),
            "execution_confidence": _number(value.get("execution_confidence")),
            "relative_value_confidence": _number(value.get("confidence")),
        },
        "modeled_net_edge": _number(value.get("modeled_net_edge")),
        "quote_quality": {
            "max_quote_age_seconds": _number(quote_package.get("max_quote_age_seconds")),
            "interleg_skew_seconds": _number(quote_package.get("interleg_skew_seconds")),
        },
        "liquidity": dict(quote_package.get("liquidity") or {}),
        "thesis": {
            "id": thesis.get("id"),
            "revision": thesis.get("revision") or _revision(value.get("thesis_updated_at")),
            "direction": thesis.get("direction"),
            "invalidation": thesis.get("invalidation"),
            "eligible": bool(thesis.get("schema_version") == 2 and thesis.get("invalidation")),
        },
        "state_reasons": list(value.get("reasons") or []),
        "blockers": list(value.get("blockers") or []),
        "reassessment_date": details.get("reassessment_date") or value.get("expiration"),
        "comparable_exact_structure_outcomes": calibration,
        "strategy_route": strategy_route,
        "market_regime": market_regime,
        "paper_only": True,
    }


def _empty_brief(symbol: str, lane: str, message: str, *, mode: str) -> dict[str, Any]:
    return {"symbol": symbol.upper(), "lane": lane, "mode": mode, "analysis_run_id": None,
            "as_of": None, "state": "COLLECTING", "summary": {"message": message},
            "readiness": _empty_readiness(), "strongest_candidate": None, "paper_only": True}


def _readiness(connection: Any, *, latest: dict[str, Any], symbol: str) -> dict[str, Any]:
    summary = dict(latest.get("summary") or {})
    generation_id = int(summary["capture_generation_id"])
    capture = connection.execute(
        """
        SELECT generation.capture_state, generation.completeness, snapshot.latest_complete_generation_id
        FROM raw.option_capture_generation generation
        JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
        WHERE generation.id = %s
        """,
        [generation_id],
    ).fetchone()
    group = connection.execute(
        """
        SELECT count(*) AS group_count,
               count(*) FILTER (WHERE surface.metrics->'blockers' ? 'missing_aligned_underlying') AS missing_underlying,
               count(*) FILTER (WHERE surface.metrics->'blockers' ? 'inconsistent_aligned_underlying') AS inconsistent_underlying
        FROM analysis.option_surface_summary surface
        WHERE surface.analysis_run_id = %s
        """,
        [latest["id"]],
    ).fetchone()
    thesis = connection.execute(
        """
        SELECT thesis.thesis, thesis.updated_at
        FROM app.thesis thesis
        JOIN catalog.instrument instrument ON instrument.id = thesis.instrument_id
        WHERE instrument.symbol = %s AND thesis.status = 'current'
        ORDER BY thesis.updated_at DESC, thesis.id DESC LIMIT 1
        """,
        [symbol],
    ).fetchone()
    calibration_rows = connection.execute(
        """
        SELECT option_decision.structure, option_decision.market_regime, option_decision.model_version,
               option_decision.details->'calibration' AS calibration
        FROM analysis.option_decision option_decision
        JOIN analysis.decision decision ON decision.id = option_decision.decision_id
        WHERE decision.run_id = %s
        ORDER BY option_decision.structure, option_decision.market_regime
        """,
        [latest["id"]],
    ).fetchall()
    blocker_rows = connection.execute(
        """
        SELECT blocker, count(*) AS count
        FROM (
            SELECT unnest(decision.blockers) AS blocker
            FROM analysis.decision decision WHERE decision.run_id = %s
            UNION ALL
            SELECT unnest(value.blockers) AS blocker
            FROM analysis.option_relative_value value WHERE value.analysis_run_id = %s
        ) blockers
        GROUP BY blocker ORDER BY count(*) DESC, blocker LIMIT 8
        """,
        [latest["id"], latest["id"]],
    ).fetchall()
    canary = canary_health(connection, symbol=symbol, model_revision=MODEL_REVISION)
    thesis_payload = dict(thesis["thesis"] or {}) if thesis else {}
    calibration = [_calibration_readiness(dict(row)) for row in calibration_rows]
    top_blockers = [{"blocker": row["blocker"], "count": int(row["count"])} for row in blocker_rows]
    analysis = {
        "eligible_groups": int(summary.get("eligible_groups") or 0),
        "fit_attempts": int(summary.get("fit_attempts") or 0),
        "succeeded_groups": int(summary.get("succeeded_groups") or 0),
        "solver_failures": int(summary.get("solver_failures") or 0),
    }
    active_thesis_blocker = thesis_blocker(thesis_payload)
    return {
        "capture": {
            "capture_state": capture["capture_state"] if capture else None,
            "completeness": _number(capture["completeness"]) if capture else None,
            "capture_generation_id": generation_id,
            "complete_captures": int(canary["complete_captures"]),
        },
        "underlying": {
            "group_count": int(group["group_count"]),
            "groups_with_missing_underlying": int(group["missing_underlying"]),
            "groups_with_inconsistent_underlying": int(group["inconsistent_underlying"]),
        },
        "analysis": analysis,
        "thesis": {
            "eligible": active_thesis_blocker is None,
            "present": bool(thesis),
            "revision": thesis_payload.get("revision") or _revision(thesis["updated_at"] if thesis else None),
            "direction": thesis_payload.get("direction"),
            "blocker": active_thesis_blocker,
            "invalidation": thesis_invalidation(thesis_payload),
        },
        "calibration": calibration,
        "canary": {
            "observed_regular_session_dates": int(canary["observed_regular_session_dates"]),
            "qualified_regular_sessions": int(canary["qualified_regular_sessions"]),
            "required_regular_sessions": int(canary["required_regular_sessions"]),
            "canary_revision": str(canary["canary_revision"]),
            "canary_started_at": canary["canary_started_at"],
            "disqualification_reasons": list(canary["disqualification_reasons"]),
        },
        "top_blockers": top_blockers,
        "next_required_action": next_required_action(analysis, thesis_payload, canary),
    }


def _empty_readiness() -> dict[str, Any]:
    return {
        "capture": {"capture_state": None, "completeness": None, "capture_generation_id": None, "complete_captures": 0},
        "underlying": {"group_count": 0, "groups_with_missing_underlying": 0, "groups_with_inconsistent_underlying": 0},
        "analysis": {"eligible_groups": 0, "fit_attempts": 0, "succeeded_groups": 0, "solver_failures": 0},
        "thesis": {
            "eligible": False, "present": False, "revision": None,
            "direction": None, "blocker": "thesis_upgrade_required", "invalidation": None,
        },
        "calibration": [],
        "canary": {
            "observed_regular_session_dates": 0, "qualified_regular_sessions": 0,
            "required_regular_sessions": 5, "canary_revision": MODEL_REVISION,
            "canary_started_at": None, "disqualification_reasons": [],
        },
        "top_blockers": [], "next_required_action": "collect_post_fix_complete_capture",
    }


def _calibration_readiness(row: dict[str, Any]) -> dict[str, Any]:
    calibration = dict(row.get("calibration") or {})
    sample_size = int(calibration.get("sample_size") or 0)
    missing = []
    if sample_size < 30:
        missing.append("30_mature_exact_structure_outcomes_required")
    if _number(calibration.get("lower_95_expectancy")) is None or _number(calibration.get("lower_95_expectancy")) <= 0:
        missing.append("positive_lower_95_expectancy_required")
    if _number(calibration.get("brier_score")) is None or _number(calibration.get("brier_score")) > 0.25:
        missing.append("brier_score_at_or_below_0_25_required")
    return {
        "structure": row["structure"], "market_regime": row.get("market_regime"),
        "model_revision": row.get("model_version") or MODEL_REVISION,
        "mature_outcomes": sample_size, "lower_95_expectancy": _number(calibration.get("lower_95_expectancy")),
        "brier_score": _number(calibration.get("brier_score")), "missing_prerequisites": missing,
    }


def _revision(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


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
