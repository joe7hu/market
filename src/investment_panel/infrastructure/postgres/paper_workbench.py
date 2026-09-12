"""Read-only paper-book projections backed by the canonical order journal."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isfinite
from typing import Any

from investment_panel.domain.decision import market_session_bounds
from investment_panel.infrastructure.postgres.runtime import (
    DatabaseRuntime,
    JOB_PROFILE,
)


CALCULATION_VERSION = "paper-workbench.v2"
MAX_PERFORMANCE_ROWS = 10_000
MAX_PERFORMANCE_EVENT_ORDERS = 4_000
MARK_STALE_AFTER = timedelta(days=3)


class PaperWorkbenchRepository:
    """Compose paper views without making shadow or outcome rows authoritative."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def trades(
        self,
        *,
        book: str = "paper",
        sleeve: str | None = None,
        symbol: str | None = None,
        instrument_kind: str | None = None,
        strategy_revision: int | None = None,
        lifecycle: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        lane: str | None = None,
        structure: str | None = None,
        evidence_class: str | None = None,
        reconciliation_status: str | None = None,
        limit: int = 100,
        cursor: tuple[datetime, str] | tuple[datetime, str, datetime] | None = None,
    ) -> dict[str, Any]:
        where, params = _where_clause(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
        )
        scope_where, scope_params = list(where), list(params)
        snapshot_at = cursor[2] if cursor and len(cursor) > 2 else None
        if snapshot_at is not None:
            _add_snapshot_filter(where, params, snapshot_at)
            _add_snapshot_filter(scope_where, scope_params, snapshot_at)
        if cursor is not None:
            where.append("(paper.created_at, paper.id) < (%s, %s::uuid)")
            params.extend([cursor[0], cursor[1]])
        safe_limit = max(1, min(100, limit))
        rows, total, _pending, _counts, watermark, as_of = self._rows(
            where,
            params,
            limit=safe_limit + 1,
            count_where=scope_where,
            count_params=scope_params,
            as_of=snapshot_at,
            compact=True,
            include_fill_rows=False,
            include_legs=False,
        )
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]
        return {
            **_scope_payload(
                book=book,
                sleeve=sleeve,
                symbol=symbol,
                instrument_kind=instrument_kind,
                strategy_revision=strategy_revision,
                lifecycle=lifecycle,
                date_from=date_from,
                date_to=date_to,
                lane=lane,
                structure=structure,
                evidence_class=evidence_class,
                reconciliation_status=reconciliation_status,
            ),
            "as_of": as_of,
            "source_watermark": watermark,
            "calculation_version": CALCULATION_VERSION,
            "snapshot_id": _snapshot_id(
                symbol=symbol,
                instrument_kind=instrument_kind,
                strategy_revision=strategy_revision,
                lifecycle=lifecycle,
                book=book,
                sleeve=sleeve,
                date_from=date_from,
                date_to=date_to,
                lane=lane,
                structure=structure,
                evidence_class=evidence_class,
                reconciliation_status=reconciliation_status,
                watermark=watermark,
                as_of=as_of,
            ),
            "counts": {
                "total": total,
                "eligible": total,
                "pending": _pending,
                "excluded": 0,
                "reconciled_orders": _counts["reconciled_orders"],
            },
            "quality_status": "complete" if not has_more and not cursor else "partial",
            "missing_evidence_reasons": _missing_reasons(rows),
            "has_more": has_more,
            "rows": rows,
        }

    def export_rows(
        self,
        *,
        book: str = "paper",
        sleeve: str | None = None,
        symbol: str | None = None,
        instrument_kind: str | None = None,
        strategy_revision: int | None = None,
        lifecycle: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        lane: str | None = None,
        structure: str | None = None,
        evidence_class: str | None = None,
        reconciliation_status: str | None = None,
    ) -> dict[str, Any]:
        """Return the bounded full scoped population for explicit export."""

        where, params = _where_clause(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
        )
        rows, total, _pending, _counts, watermark, as_of = self._rows(
            where, params, limit=MAX_PERFORMANCE_ROWS
        )
        return {
            **_scope_payload(
                book=book,
                sleeve=sleeve,
                symbol=symbol,
                instrument_kind=instrument_kind,
                strategy_revision=strategy_revision,
                lifecycle=lifecycle,
                date_from=date_from,
                date_to=date_to,
                lane=lane,
                structure=structure,
                evidence_class=evidence_class,
                reconciliation_status=reconciliation_status,
            ),
            "as_of": as_of,
            "source_watermark": watermark,
            "calculation_version": CALCULATION_VERSION,
            "snapshot_id": _snapshot_id(
                symbol=symbol,
                instrument_kind=instrument_kind,
                strategy_revision=strategy_revision,
                lifecycle=lifecycle,
                book=book,
                sleeve=sleeve,
                date_from=date_from,
                date_to=date_to,
                lane=lane,
                structure=structure,
                evidence_class=evidence_class,
                reconciliation_status=reconciliation_status,
                watermark=watermark,
                as_of=as_of,
            ),
            "total": total,
            "rows": rows,
        }

    def trade(self, trade_id: str) -> dict[str, Any] | None:
        where = ["paper.id = %s::uuid", "paper.paper_only IS TRUE"]
        as_of = datetime.now(UTC)
        with self.runtime.snapshot(JOB_PROFILE) as connection:
            row = connection.execute(self._select_sql(where), [trade_id]).fetchone()
            if row is None:
                return None
            raw_row = dict(row)
            marks, watermark = _current_marks(connection, [raw_row], as_of)
        raw_row.update(marks.get(str(raw_row["paper_order_id"]), {}))
        watermark = _max_datetime(
            watermark, raw_row.get("staged_at"), raw_row.get("updated_at")
        )
        return {
            **paper_trade_payload(raw_row),
            "as_of": as_of,
            "source_watermark": watermark,
            "calculation_version": CALCULATION_VERSION,
        }

    def performance(
        self,
        *,
        book: str = "paper",
        sleeve: str | None = None,
        symbol: str | None = None,
        instrument_kind: str | None = None,
        strategy_revision: int | None = None,
        lifecycle: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        lane: str | None = None,
        structure: str | None = None,
        evidence_class: str | None = None,
        reconciliation_status: str | None = None,
    ) -> dict[str, Any]:
        where, params = _where_clause(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
        )
        rows, total, _pending, counts, watermark, as_of = self._rows(
            where,
            params,
            limit=MAX_PERFORMANCE_ROWS,
            compact=True,
            include_fill_rows=False,
            include_legs=False,
        )
        filled = [row for row in rows if row["filled_quantity"] > 0]
        realized_rows = [row for row in rows if row["realized_pnl"] is not None]
        realized_eligible_rows = [row for row in rows if row["exited_quantity"] > 0]
        open_rows = [row for row in rows if row["remaining_quantity"] > 0]
        realized = sum(
            (Decimal(str(row["realized_pnl"])) for row in realized_rows), Decimal("0")
        )
        unrealized_rows = [
            row for row in open_rows if row.get("unrealized_pnl") is not None
        ]
        unrealized = sum(
            (Decimal(str(row["unrealized_pnl"])) for row in unrealized_rows),
            Decimal("0"),
        )
        exposure_rows = [row for row in open_rows if row.get("mark_value") is not None]
        open_exposure = sum(
            (Decimal(str(row["mark_value"])) for row in exposure_rows), Decimal("0")
        )
        cumulative = Decimal("0")
        series = []
        # ponytail: cap detailed event fetches at 4k orders; aggregate P&L
        # remains full-scope and older exits fall back to order exit_at.
        fill_rows_by_order = self._exit_fill_rows(rows[:MAX_PERFORMANCE_EVENT_ORDERS], as_of)
        exit_events = _realized_exit_events(realized_rows, fill_rows_by_order)
        for event in exit_events:
            cumulative += event["pnl"]
            series.append(
                {
                    "at": event["at"],
                    "cumulative_net_pnl": _money(cumulative),
                    "trade_id": event["trade_id"],
                    "journal_id": event.get("journal_id"),
                }
            )
        peak = Decimal("0")
        max_drawdown = Decimal("0")
        drawdown_series: list[dict[str, Any]] = []
        for point in series:
            value = Decimal(str(point["cumulative_net_pnl"]))
            peak = max(peak, value)
            drawdown = value - peak
            max_drawdown = min(max_drawdown, drawdown)
            drawdown_series.append({
                "at": point["at"],
                "drawdown": _money(drawdown),
                "trade_id": point["trade_id"],
            })
        missing = _missing_reasons(rows)
        if "opening_capital_unavailable" not in missing:
            missing.append("opening_capital_unavailable")
        if total > MAX_PERFORMANCE_ROWS:
            missing.append("performance_population_bounded_at_10000_rows")
        realized_complete = total <= MAX_PERFORMANCE_ROWS and len(realized_rows) == len(realized_eligible_rows)
        known_realized = (
            _money(realized) if realized_rows or not realized_eligible_rows else None
        )
        known_unrealized = (
            _money(unrealized)
            if total <= MAX_PERFORMANCE_ROWS and (not open_rows or len(unrealized_rows) == len(open_rows))
            else None
        )
        net_pnl = (
            _money(realized + unrealized)
            if realized_complete and known_unrealized is not None
            else None
        )
        display_indices = paper_chart_indices(series, drawdown_series)
        visuals = paper_performance_visuals(rows, exit_events, fill_rows_by_order=fill_rows_by_order)
        event_markers = visuals["event_markers"]
        mark_coverage = (
            sum(row.get("mark_value") is not None for row in open_rows) / len(open_rows)
            if open_rows
            else 0.0
        )
        return {
            **_scope_payload(
                book=book,
                sleeve=sleeve,
                symbol=symbol,
                instrument_kind=instrument_kind,
                strategy_revision=strategy_revision,
                lifecycle=lifecycle,
                date_from=date_from,
                date_to=date_to,
                lane=lane,
                structure=structure,
                evidence_class=evidence_class,
                reconciliation_status=reconciliation_status,
            ),
            "as_of": as_of,
            "source_watermark": watermark,
            "calculation_version": CALCULATION_VERSION,
            "snapshot_id": _snapshot_id(
                symbol=symbol,
                instrument_kind=instrument_kind,
                strategy_revision=strategy_revision,
                lifecycle=lifecycle,
                book=book,
                sleeve=sleeve,
                date_from=date_from,
                date_to=date_to,
                lane=lane,
                structure=structure,
                evidence_class=evidence_class,
                reconciliation_status=reconciliation_status,
                watermark=watermark,
                as_of=as_of,
            ),
            "trades": [{key: value for key, value in row.items() if key not in {"decision", "execution", "outcome", "artifacts", "mark"}} for row in rows[:100]],
            "currency": "USD",
            "accounting_basis": "app.paper_order plus deterministic app.trade_journal fills, fees, and multiplier evidence",
            "counts": {
                "total_orders": total,
                "filled_orders": counts["filled_orders"],
                "reconciled_orders": counts["reconciled_orders"],
                "closed_trades": counts["closed_trades"],
                "open_trades": counts["open_trades"],
                "staged_orders": counts["staged_orders"],
                "realized_pnl_known": len(realized_rows),
                "realized_pnl_unknown": max(
                    len(realized_eligible_rows) - len(realized_rows), 0
                ),
                "open_mark_known": len(unrealized_rows),
                "open_mark_unknown": max(len(open_rows) - len(unrealized_rows), 0),
            },
            "net_pnl": net_pnl,
            "realized_pnl": known_realized,
            "realized_pnl_status": "complete" if realized_complete else "partial",
            "unrealized_pnl": known_unrealized,
            "open_exposure": _money(open_exposure) if exposure_rows else 0.0 if not open_rows else None,
            "open_exposure_status": "complete" if len(exposure_rows) == len(open_rows) else "partial",
            "nav": None,
            "nav_status": "unavailable",
            "return_pct": None,
            "return_status": "unavailable",
            "capital_status": "unavailable",
            "flow_status": "unavailable",
            "drawdown": _money(max_drawdown) if series and realized_complete else None,
            "evidence_coverage": {
                "filled_orders": counts["filled_orders"],
                "reconciled_orders": counts["reconciled_orders"],
                "realized_pnl_coverage": len(realized_rows)
                / len(realized_eligible_rows)
                if realized_eligible_rows
                else 1.0
                if filled
                else 0.0,
                "mark_coverage": mark_coverage,
            },
            "quality_status": "partial"
            if missing or total > MAX_PERFORMANCE_ROWS
            else "complete",
            "missing_evidence_reasons": sorted(set(missing)),
            "series": {
                "kind": "cumulative_verified_realized_net_pnl",
                "points": [series[index] for index in display_indices],
                "full_resolution_point_count": len(series),
                "display_method": "bucket_first_last_pnl_extremes_and_drawdown_trough.v1",
                "drawdown_points": [drawdown_series[index] for index in display_indices] if realized_complete else [],
                "available_series": ["cumulative_net_pnl", "drawdown"],
                "unavailable_series": ["nav", "capital_normalized_return"],
                "basis": "verified realized journal exit events",
                "annotations": [
                    {
                        "at": point["at"],
                        "kind": "paper_exit",
                        "trade_id": point["trade_id"],
                        "journal_id": point.get("journal_id"),
                    }
                    for point in series
                ],
                "event_markers": event_markers,
                "available_event_kinds": sorted({str(event["kind"]) for event in event_markers}),
                "gaps": [
                    {
                        "reason": row.get("mark_gap_reason")
                        or "verified_current_mark_unavailable",
                        "trade_id": row["paper_order_id"],
                    }
                    for row in open_rows
                    if row.get("mark_status") != "verified"
                ],
                "statistics_basis": "full scoped paper-order population; realized series only; no annualized statistics",
                "drawdown_basis": "cumulative verified realized net P&L, not NAV",
                "unavailable_series_reasons": {
                    "nav": "opening_capital_and_paper_cash_flows_unavailable",
                    "capital_normalized_return": "opening_capital_unavailable",
                },
            },
            "attribution": visuals["attribution"],
        }

    def _exit_fill_rows(
        self, rows: list[dict[str, Any]], as_of: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        """Load fills only for realized rows that need event-level visuals."""

        order_ids = [
            str(row["paper_order_id"])
            for row in rows
            if row.get("paper_order_id") is not None
        ]
        if not order_ids:
            return {}
        with self.runtime.snapshot(JOB_PROFILE) as connection:
            fill_rows = connection.execute(
                """
                SELECT journal.details->>'paper_order_id' AS paper_order_id,
                       journal.id::text AS id,
                       journal.action,
                       journal.quantity,
                       journal.price,
                       journal.created_at,
                       journal.details->'fees' AS fees,
                       journal.details->'contract_multiplier' AS contract_multiplier,
                       journal.details->'entry_contract_multiplier' AS entry_contract_multiplier,
                       journal.details->'exit_contract_multiplier' AS exit_contract_multiplier
                FROM app.trade_journal journal
                JOIN app.paper_order paper
                  ON paper.id::text = journal.details->>'paper_order_id'
                 AND paper.id = ANY(%s::uuid[])
                 AND paper.paper_only IS TRUE
                 AND journal.decision_id IS NOT DISTINCT FROM paper.decision_id
                 AND journal.instrument_id = paper.instrument_id
                WHERE journal.rationale = 'deterministic_options_paper_execution'
                  AND (journal.action = 'paper_entry'
                       OR journal.action = 'paper_exit'
                       OR journal.action LIKE 'paper_exit:%%')
                  AND journal.created_at <= %s
                ORDER BY journal.details->>'paper_order_id', journal.created_at, journal.id
                """,
                [order_ids, as_of],
            ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in fill_rows:
            fill = dict(row)
            result.setdefault(str(fill.pop("paper_order_id")), []).append(fill)
        return result

    def _rows(
        self,
        where: list[str],
        params: list[Any],
        *,
        limit: int,
        count_where: list[str] | None = None,
        count_params: list[Any] | None = None,
        as_of: datetime | None = None,
        compact: bool = False,
        include_fill_rows: bool = True,
        include_legs: bool = True,
    ) -> tuple[list[dict[str, Any]], int, int, dict[str, int], datetime | None, datetime]:
        as_of = as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            raise ValueError("paper workbench snapshot must be timezone-aware")
        with self.runtime.snapshot(JOB_PROFILE) as connection:
            count_row = connection.execute(
                f"""SELECT count(*) AS count,
                           count(*) FILTER (WHERE paper.status = ANY(%s::text[])) AS pending,
                           count(*) FILTER (WHERE coalesce(projection.entry_quantity, 0) > 0) AS filled_orders,
                           count(*) FILTER (WHERE paper.status = ANY(ARRAY['closed', 'exited', 'invalidated']::text[])) AS closed_trades,
                           count(*) FILTER (WHERE paper.status = ANY(ARRAY['open', 'entered', 'partial_exited']::text[])) AS open_trades,
                           count(*) FILTER (WHERE paper.status = ANY(ARRAY['staged', 'pending', 'submitted', 'cancelled', 'rejected']::text[])) AS staged_orders,
                           count(*) FILTER (WHERE {_reconciliation_predicate("verified")}) AS reconciled_orders,
                           max(greatest(paper.created_at, coalesce(paper.updated_at, paper.created_at))) AS source_watermark
                    FROM app.paper_order paper
                    JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                    LEFT JOIN analysis.decision decision ON decision.id = paper.decision_id
                    LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = paper.decision_id
                    LEFT JOIN analysis.paper_trade_projection projection
                      ON projection.paper_order_id = paper.id
                    {("WHERE " + " AND ".join(count_where if count_where is not None else where)) if (count_where or where) else ""}""",
                [
                    ["staged", "pending", "submitted", "cancelled", "rejected"],
                    *(count_params if count_params is not None else params),
                ],
            ).fetchone()
            raw_rows = [
                dict(row)
                for row in connection.execute(
                    self._select_sql(
                        where,
                        compact=compact,
                        include_fill_rows=include_fill_rows,
                        include_legs=include_legs,
                    )
                    + " ORDER BY paper.created_at DESC, paper.id DESC LIMIT %s",
                    [*params, limit],
                ).fetchall()
            ]
            # Closed orders do not need a current quote. Avoid joining every
            # historical position to the live mark tables on each dashboard read.
            marks, mark_watermark = _current_marks(
                connection,
                [row for row in raw_rows if _raw_has_open_quantity(row)],
                as_of,
            )
        return (
            [
                paper_trade_payload(
                    {**row, **marks.get(str(row["paper_order_id"]), {})}
                )
                for row in raw_rows
            ],
            int(count_row["count"]),
            int(count_row["pending"]),
            {
                "filled_orders": int(count_row["filled_orders"]),
                "closed_trades": int(count_row["closed_trades"]),
                "open_trades": int(count_row["open_trades"]),
                "staged_orders": int(count_row["staged_orders"]),
                "reconciled_orders": int(count_row["reconciled_orders"]),
            },
            _max_datetime(count_row["source_watermark"], mark_watermark),
            as_of,
        )

    @staticmethod
    def _select_sql(
        where: list[str],
        *,
        compact: bool = False,
        include_fill_rows: bool = True,
        include_legs: bool = True,
    ) -> str:
        """Select the bounded list projection or the full detail record."""
        detail_select = (
            """
                   thesis.revision AS thesis_revision,
                   thesis.thesis AS thesis_payload,
                   outcome.maturity_state AS outcome_state,
                   outcome.observed_through,
                   outcome.current_return,
                   outcome.realized_exit_return,
                   outcome.realized_exit_basis,
                   outcome.mae,
                   outcome.max_drawdown AS outcome_max_drawdown,
                   shadow.id::text AS shadow_id,
                   shadow.status AS shadow_status,
                   shadow.entry_at AS shadow_entry_at,
                   shadow.exit_at AS shadow_exit_at,
                   shadow.metrics AS shadow_metrics,
                   evidence.items AS decision_evidence,
"""
            if not compact
            else
            """
                   NULL::integer AS thesis_revision,
                   NULL::jsonb AS thesis_payload,
                   NULL::text AS outcome_state,
                   NULL::timestamptz AS observed_through,
                   NULL::double precision AS current_return,
                   NULL::double precision AS realized_exit_return,
                   NULL::text AS realized_exit_basis,
                   NULL::double precision AS mae,
                   NULL::double precision AS outcome_max_drawdown,
                   NULL::text AS shadow_id,
                   NULL::text AS shadow_status,
                   NULL::timestamptz AS shadow_entry_at,
                   NULL::timestamptz AS shadow_exit_at,
                   NULL::jsonb AS shadow_metrics,
                   NULL::jsonb AS decision_evidence,
"""
        )
        detail_joins = (
            """
            LEFT JOIN app.thesis thesis ON thesis.id = option_decision.thesis_id
            LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = paper.decision_id
            LEFT JOIN analysis.shadow_trade shadow ON shadow.decision_id = paper.decision_id
            LEFT JOIN LATERAL (
                SELECT coalesce(jsonb_agg(jsonb_build_object(
                           'evidence_kind', evidence.evidence_kind,
                           'reference_key', evidence.reference_key,
                           'reference_url', evidence.reference_url,
                           'detail', evidence.detail
                       ) ORDER BY evidence.evidence_kind, evidence.reference_key), '[]'::jsonb) AS items
                FROM analysis.decision_evidence evidence
                WHERE evidence.decision_id = paper.decision_id
            ) evidence ON TRUE
"""
            if not compact
            else ""
        )
        fill_rows_select = "projection.fill_rows" if include_fill_rows else "NULL::jsonb"
        legs_select = "legs.items" if include_legs else "NULL::jsonb"
        legs_join = (
            """
            LEFT JOIN LATERAL (
                SELECT coalesce(jsonb_agg(jsonb_build_object(
                           'leg_index', leg.leg_index,
                           'contract_id', leg.contract_id,
                           'option_type', leg.option_type,
                           'side', leg.side,
                           'strike', leg.strike,
                           'bid', leg.bid,
                           'ask', leg.ask,
                           'bid_size', leg.bid_size,
                           'ask_size', leg.ask_size,
                           'quote_time', leg.quote_time,
                           'expiration', contract.expiration,
                           'multiplier', contract.multiplier
                       ) ORDER BY leg.leg_index), '[]'::jsonb) AS items
                FROM app.paper_order_leg leg
                JOIN catalog.option_contract contract ON contract.id = leg.contract_id
                WHERE leg.paper_order_id = paper.id
            ) legs ON TRUE
            """
            if include_legs
            else ""
        )
        return f"""
            SELECT paper.id::text AS paper_order_id,
                   paper.book,
                   paper.sleeve,
                   paper.decision_id::text AS decision_id,
                   paper.instrument_id,
                   instrument.symbol,
                   instrument.asset_class,
                   paper.created_at AS staged_at,
                   paper.updated_at,
                   paper.exit_at AS paper_exit_at,
                   paper.side,
                   paper.quantity AS requested_quantity,
                   paper.limit_price AS staged_limit_price,
                   paper.status AS paper_status,
                   paper.policy_result,
                   paper.structure,
                   paper.reserved_collateral,
                   paper.ticket_snapshot,
                   paper.policy_snapshot,
                   paper.event_id,
                   paper.event_signal_id,
                   paper.ticker_decision_id,
                   paper.max_loss,
                   paper.planned_loss,
                   paper.strategy_family AS order_strategy_family,
                   paper.objective_version,
                   paper.lane,
                   paper.ticker_decision_revision,
                   paper.expression_kind,
                   paper.paper_only,
                   paper.contract_multiplier AS order_multiplier,
                   paper.thesis_snapshot,
                   decision.as_of AS decision_at,
                   decision.state AS decision_state,
                   decision.reasons,
                   decision.blockers,
                   decision.kind AS decision_kind,
                   decision.lane AS decision_lane,
                   decision.strategy_revision_id,
                   revision.strategy_key,
                   revision.revision AS strategy_revision,
                   revision.name AS strategy_name,
                   revision.strategy_family,
                   revision.authority_group,
                   option_decision.structure AS decision_structure,
                   option_decision.contract_id,
                   option_decision.paper_state,
                   option_decision.discovery_lane,
                   option_decision.model_version,
                   option_decision.probability_profit,
                   option_decision.expected_value,
                   option_decision.max_loss AS decision_max_loss,
                   option_decision.risk_adjusted_expectancy,
                   option_decision.data_confidence,
                   option_decision.execution_confidence,
                   option_decision.modeled_net_edge,
                   option_decision.details AS decision_details,
                   option_decision.synthetic_legs,
                   option_decision.quote_observed_at,
                   contract.expiration,
                   contract.strike,
                   contract.option_type,
                   contract.multiplier AS catalog_multiplier,
{detail_select}                   projection.entry_quantity,
                   projection.exit_quantity,
                   projection.entry_units,
                   projection.exit_units,
                   projection.actual_fees,
                   projection.entry_fees,
                   projection.exit_fees,
                   projection.missing_fees,
                   projection.invalid_fills,
                   projection.fill_multipliers_verified,
                   projection.latest_fill_at,
                   projection.journal_ids,
                   {fill_rows_select} AS fill_rows,
                   {legs_select} AS order_legs
            FROM app.paper_order paper
            JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
            LEFT JOIN analysis.decision decision ON decision.id = paper.decision_id
            LEFT JOIN analysis.strategy_revision revision ON revision.id = decision.strategy_revision_id
            LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = paper.decision_id
            LEFT JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
            LEFT JOIN analysis.paper_trade_projection projection
              ON projection.paper_order_id = paper.id
{detail_joins}
{legs_join}
            {("WHERE " + " AND ".join(where)) if where else ""}
        """


def paper_chart_indices(series: list[dict[str, Any]], drawdowns: list[dict[str, Any]], limit: int = 2000) -> list[int]:
    """Bound display points while statistics continue using every journal event."""
    limit = max(5, int(limit))
    if len(series) <= limit:
        return list(range(len(series)))
    width = (len(series) + limit // 5 - 1) // (limit // 5)
    selected: set[int] = set()
    for start in range(0, len(series), width):
        bucket = range(start, min(start + width, len(series)))
        selected.update((bucket.start, bucket.stop - 1,
                         min(bucket, key=lambda index: series[index]["cumulative_net_pnl"]),
                         max(bucket, key=lambda index: series[index]["cumulative_net_pnl"]),
                         min(bucket, key=lambda index: drawdowns[index]["drawdown"])))
    return sorted(selected)


def _current_marks(
    connection: Any,
    rows: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[dict[str, dict[str, Any]], datetime | None]:
    """Read one verified mark set for a page; never use a staged price as a mark."""

    stock_instrument_ids = sorted(
        {
            int(row["instrument_id"])
            for row in rows
            if row.get("instrument_id") is not None and not _is_option_order(row)
        }
    )
    contract_ids = sorted(
        {contract_id for row in rows for contract_id in _option_contract_ids(row)}
    )
    stock_marks: dict[int, dict[str, Any]] = {}
    option_marks: dict[int, dict[str, Any]] = {}
    if stock_instrument_ids:
        stock_rows = connection.execute(
            """
            SELECT mark.instrument_id, mark.price, mark.currency, mark.source_id,
                   mark.observed_at, mark.available_at, mark.valuation_status,
                   mark.source_kind
            FROM analysis.paper_current_mark_projection mark
            JOIN ingest.source source
              ON source.id = mark.source_id
             AND source.enabled
             AND source.operational_state = 'active'
            WHERE mark.instrument_id = ANY(%s::bigint[])
              AND mark.observed_at <= %s
              AND mark.available_at <= %s
              AND mark.projected_at <= %s
            ORDER BY mark.instrument_id
            """,
            [stock_instrument_ids, as_of, as_of, as_of],
        ).fetchall()
        projected_ids = {int(row["instrument_id"]) for row in stock_rows}
        missing_ids = [
            instrument_id
            for instrument_id in stock_instrument_ids
            if instrument_id not in projected_ids
        ]
        if missing_ids:
            has_daily_source = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM ingest.source
                    WHERE enabled AND operational_state = 'active'
                      AND kind IN ('daily_bars', 'daily_quote')
                ) AS present
                """
            ).fetchone()["present"]
            if has_daily_source:
                fallback_rows = connection.execute(
                    """
                    SELECT priced.instrument_id, priced.price, priced.currency, priced.source_id,
                           priced.observed_at, priced.available_at, priced.valuation_status,
                           priced.source_kind
                    FROM raw.current_price_at(%s, %s::bigint[]) priced
                    ORDER BY priced.instrument_id
                    """,
                    [as_of, missing_ids],
                ).fetchall()
            else:
                # Keep the common quote-only fallback indexed by instrument
                # when a source has not populated the projection yet.
                fallback_rows = connection.execute(
                    """
                    SELECT current.instrument_id, current.price, current.currency,
                           current.source_id, current.observed_at, current.available_at,
                           'market_quote'::text AS valuation_status, current.source_kind
                    FROM unnest(%s::bigint[]) requested(instrument_id)
                    CROSS JOIN LATERAL (
                        SELECT quote.instrument_id, quote.price, quote.currency,
                               quote.source_id, quote.observed_at, quote.available_at,
                               source.kind AS source_kind, run.finished_at AS confirmed_at
                        FROM raw.quote quote
                        JOIN raw.quote_fact_availability availability
                          ON availability.fact_id = quote.id
                         AND availability.fact_available_at = quote.available_at
                        JOIN ingest.run run
                          ON run.id = availability.ingest_run_id
                         AND run.status IN ('succeeded', 'partial')
                         AND run.finished_at IS NOT NULL
                         AND run.finished_at <= %s
                        JOIN ingest.source source
                          ON source.id = quote.source_id
                         AND source.enabled
                         AND source.operational_state = 'active'
                        WHERE quote.instrument_id = requested.instrument_id
                          AND quote.price > 0
                          AND quote.observed_at <= %s
                          AND quote.available_at <= %s
                        UNION ALL
                        SELECT quote.instrument_id, quote.price, quote.currency,
                               quote.source_id, quote.observed_at, quote.available_at,
                               source.kind AS source_kind, run.finished_at AS confirmed_at
                        FROM raw.quote_history quote
                        JOIN raw.quote_fact_availability availability
                          ON availability.fact_id = quote.id
                         AND availability.fact_available_at = quote.available_at
                        JOIN ingest.run run
                          ON run.id = availability.ingest_run_id
                         AND run.status IN ('succeeded', 'partial')
                         AND run.finished_at IS NOT NULL
                         AND run.finished_at <= %s
                        JOIN ingest.source source
                          ON source.id = quote.source_id
                         AND source.enabled
                         AND source.operational_state = 'active'
                        WHERE quote.instrument_id = requested.instrument_id
                          AND quote.price > 0
                          AND quote.observed_at <= %s
                          AND quote.available_at <= %s
                        ORDER BY confirmed_at DESC, observed_at DESC,
                                 available_at DESC, source_id
                        LIMIT 1
                    ) current
                    ORDER BY current.instrument_id
                    """,
                    [missing_ids, as_of, as_of, as_of, as_of, as_of, as_of],
                ).fetchall()
            stock_rows.extend(fallback_rows)
        stock_marks = {int(row["instrument_id"]): dict(row) for row in stock_rows}
    if contract_ids:
        option_rows = connection.execute(
            """
            SELECT DISTINCT ON (quote.contract_id)
                   quote.contract_id, quote.bid, quote.ask, quote.mid, quote.last,
                   quote.observed_at, quote.available_at, quote.id AS quote_id,
                   snapshot.source_id, snapshot.id AS snapshot_id,
                   source.kind AS source_kind,
                   quote.capture_generation_id
            FROM raw.option_quote quote
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN ingest.run snapshot_run ON snapshot_run.id = snapshot.ingest_run_id
            JOIN ingest.source source
              ON source.id = snapshot.source_id
             AND source.enabled
             AND source.operational_state = 'active'
            LEFT JOIN raw.option_capture_generation generation
              ON generation.id = quote.capture_generation_id
            WHERE quote.contract_id = ANY(%s::bigint[])
              AND quote.observed_at <= %s
              AND quote.available_at <= %s
              AND snapshot.capture_state IN ('complete', 'partial')
              AND snapshot_run.status IN ('succeeded', 'partial')
              AND snapshot_run.finished_at IS NOT NULL
              AND snapshot_run.finished_at <= %s
              AND (
                    generation.id IS NULL
                    OR (
                        generation.capture_state IN ('complete', 'partial')
                        AND generation.capture_finished_at IS NOT NULL
                        AND generation.capture_finished_at <= %s
                    )
              )
              AND (
                    (quote.bid > 0 AND quote.ask >= quote.bid)
                    OR quote.mid > 0
              )
            ORDER BY quote.contract_id, quote.observed_at DESC, quote.available_at DESC, quote.id DESC
            """,
            [contract_ids, as_of, as_of, as_of, as_of],
        ).fetchall()
        option_marks = {int(row["contract_id"]): dict(row) for row in option_rows}

    marks: dict[str, dict[str, Any]] = {}
    watermark: datetime | None = None
    for row in rows:
        order_id = str(row["paper_order_id"])
        if _is_option_order(row):
            mark_rows = [
                option_marks[contract_id]
                for contract_id in _option_contract_ids(row)
                if contract_id in option_marks
            ]
            if mark_rows:
                mark = mark_rows[0]
                marks[order_id] = {
                    "option_marks": [
                        {
                            "contract_id": int(mark_row["contract_id"]),
                            "bid": mark_row.get("bid"),
                            "ask": mark_row.get("ask"),
                            "mid": mark_row.get("mid"),
                            "last": mark_row.get("last"),
                            "observed_at": mark_row.get("observed_at"),
                            "available_at": mark_row.get("available_at"),
                            "source": mark_row.get("source_id"),
                            "source_kind": mark_row.get("source_kind"),
                            "snapshot_id": mark_row.get("snapshot_id"),
                            "quote_id": mark_row.get("quote_id"),
                        }
                        for mark_row in mark_rows
                    ],
                    "option_mark_bid": mark.get("bid"),
                    "option_mark_ask": mark.get("ask"),
                    "option_mark_mid": mark.get("mid"),
                    "option_mark_last": mark.get("last"),
                    "option_mark_observed_at": mark.get("observed_at"),
                    "option_mark_available_at": mark.get("available_at"),
                    "option_mark_source": mark.get("source_id"),
                    "option_mark_snapshot_id": mark.get("snapshot_id"),
                    "option_mark_quote_id": mark.get("quote_id"),
                    "mark_as_of": as_of,
                }
                for mark_row in mark_rows:
                    watermark = _max_datetime(watermark, mark_row.get("available_at"))
        else:
            mark = (
                stock_marks.get(int(row["instrument_id"]))
                if row.get("instrument_id") is not None
                else None
            )
            if mark is not None:
                marks[order_id] = {
                    "stock_mark_price": mark.get("price"),
                    "stock_mark_currency": mark.get("currency"),
                    "stock_mark_observed_at": mark.get("observed_at"),
                    "stock_mark_available_at": mark.get("available_at"),
                    "stock_mark_source": mark.get("source_id"),
                    "stock_mark_status": mark.get("valuation_status"),
                    "stock_mark_source_kind": mark.get("source_kind"),
                    "mark_as_of": as_of,
                }
                watermark = _max_datetime(watermark, mark.get("available_at"))
    return marks, watermark


def _order_legs(row: dict[str, Any]) -> list[dict[str, Any]]:
    legs = row.get("order_legs")
    return (
        [dict(leg) for leg in legs if isinstance(leg, dict)]
        if isinstance(legs, list)
        else []
    )


def _option_contract_ids(row: dict[str, Any]) -> list[int]:
    contract_ids: list[int] = []
    if row.get("contract_id") is not None:
        contract_ids.append(int(row["contract_id"]))
    for leg in _order_legs(row):
        if leg.get("contract_id") is not None:
            contract_ids.append(int(leg["contract_id"]))
    return list(dict.fromkeys(contract_ids))


def _is_option_order(row: dict[str, Any]) -> bool:
    structure = str(row.get("structure") or row.get("decision_structure") or "").lower()
    expression_kind = str(row.get("expression_kind") or "").lower()
    return (
        row.get("contract_id") is not None
        or bool(_order_legs(row))
        or structure
        in {
            "cash_secured_put",
            "put_credit_spread",
            "call_credit_spread",
            "long_call",
            "long_put",
            "debit_spread",
            "call_debit_spread",
            "put_debit_spread",
        }
        or expression_kind in {"call", "put", "debit_spread", "cash_secured_put"}
    )


def _is_credit_order(row: dict[str, Any]) -> bool:
    structure = str(row.get("structure") or row.get("decision_structure") or "").lower()
    return (
        structure
        in {
            "cash_secured_put",
            "put_credit_spread",
            "call_credit_spread",
            "credit_spread",
            "short_option",
        }
        or str(row.get("side") or "").lower() == "sell"
    )


def _snapshot_id(
    *,
    book: str,
    sleeve: str | None,
    symbol: str | None,
    instrument_kind: str | None,
    strategy_revision: int | None,
    lifecycle: str | None,
    date_from: date | None,
    date_to: date | None,
    lane: str | None,
    structure: str | None,
    evidence_class: str | None,
    reconciliation_status: str | None,
    watermark: datetime | None,
    as_of: datetime | None = None,
) -> str:
    payload = {
        "scope": _scope_values(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
        ),
        "source_watermark": watermark.isoformat()
        if isinstance(watermark, datetime)
        else None,
        "as_of": as_of.isoformat() if isinstance(as_of, datetime) else None,
        "calculation_version": CALCULATION_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _max_datetime(*values: Any) -> datetime | None:
    datetimes = [value for value in values if isinstance(value, datetime)]
    return max(datetimes) if datetimes else None


def _add_snapshot_filter(
    where: list[str], params: list[Any], snapshot_at: datetime
) -> None:
    """Keep cursor pages on the same visible paper-order snapshot."""
    where.extend(
        [
            "paper.created_at <= %s",
            "coalesce(paper.updated_at, paper.created_at) <= %s",
        ]
    )
    params.extend([snapshot_at, snapshot_at])


def _where_clause(
    *,
    book: str,
    sleeve: str | None,
    symbol: str | None,
    instrument_kind: str | None,
    strategy_revision: int | None,
    lifecycle: str | None,
    date_from: date | None,
    date_to: date | None,
    lane: str | None,
    structure: str | None,
    evidence_class: str | None,
    reconciliation_status: str | None,
) -> tuple[list[str], list[Any]]:
    normalized_book = str(book or "paper").strip().lower()
    if normalized_book != "paper":
        raise ValueError("unsupported paper book")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("paper date_from must be on or before date_to")
    if evidence_class and reconciliation_status and evidence_class != reconciliation_status:
        raise ValueError("evidence_class and reconciliation_status must match")
    status_filter = evidence_class or reconciliation_status
    if status_filter and status_filter not in {"verified", "partial", "unavailable"}:
        raise ValueError("unsupported paper evidence class")
    where = ["paper.paper_only IS TRUE", "paper.book = %s"]
    params: list[Any] = [normalized_book]
    if sleeve:
        where.append("paper.sleeve = %s")
        params.append(sleeve.strip())
    if symbol:
        where.append("instrument.symbol = %s")
        params.append(symbol.strip().upper())
    if instrument_kind:
        where.append("lower(instrument.asset_class) = %s")
        params.append(instrument_kind.strip().lower())
    if strategy_revision is not None:
        where.append("decision.strategy_revision_id = %s")
        params.append(strategy_revision)
    if lifecycle:
        status_map = {
            "staged": ("staged", "pending", "submitted", "cancelled", "rejected"),
            "open": ("open", "entered", "partial_exited"),
            "closed": ("closed", "exited", "invalidated"),
        }
        statuses = status_map.get(lifecycle)
        if statuses is None:
            raise ValueError("unsupported paper lifecycle")
        where.append("paper.status = ANY(%s::text[])")
        params.append(list(statuses))
    if date_from is not None:
        where.append("paper.created_at >= %s::date")
        params.append(date_from)
    if date_to is not None:
        where.append("paper.created_at < (%s::date + interval '1 day')")
        params.append(date_to)
    if lane:
        where.append("lower(coalesce(paper.lane, decision.lane, '')) = %s")
        params.append(lane.strip().lower())
    if structure:
        where.append("lower(coalesce(paper.structure, option_decision.structure, '')) = %s")
        params.append(structure.strip().lower())
    if status_filter:
        where.append(_reconciliation_predicate(status_filter))
    return where, params


def _reconciliation_predicate(status: str) -> str:
    """Match the payload's evidence class from the maintained fill projection."""
    fill = "coalesce(projection.entry_quantity, 0) > 0"
    valid_fills = """
        coalesce(projection.missing_fees, 0) = 0
        AND coalesce(projection.invalid_fills, 0) = 0
    """
    is_option = """
        (
            option_decision.contract_id IS NOT NULL
            OR lower(coalesce(paper.structure, option_decision.structure, '')) IN (
                'cash_secured_put', 'put_credit_spread', 'call_credit_spread',
                'long_call', 'long_put', 'debit_spread', 'call_debit_spread',
                'put_debit_spread'
            )
            OR lower(coalesce(paper.expression_kind, '')) IN ('call', 'put', 'debit_spread', 'cash_secured_put')
        )
    """
    valid_multiplier = "projection.fill_multipliers_verified IS TRUE"
    verified = f"({fill} AND {valid_fills} AND (NOT {is_option} OR {valid_multiplier}))"
    if status == "verified":
        return verified
    if status == "unavailable":
        return f"NOT ({fill})"
    return f"({fill} AND NOT {verified})"


def paper_trade_payload(row: dict[str, Any]) -> dict[str, Any]:
    entry_quantity = _decimal(row.get("entry_quantity")) or Decimal("0")
    exit_quantity = _decimal(row.get("exit_quantity")) or Decimal("0")
    entry_units = _decimal(row.get("entry_units")) or Decimal("0")
    exit_units = _decimal(row.get("exit_units")) or Decimal("0")
    filled = max(entry_quantity, Decimal("0"))
    remaining = max(filled - max(exit_quantity, Decimal("0")), Decimal("0"))
    has_fill = filled > 0
    entry_price = entry_units / filled if has_fill and entry_units >= 0 else None
    exit_price = (
        exit_units / exit_quantity if exit_quantity > 0 and exit_units >= 0 else None
    )
    structure = row.get("structure") or row.get("decision_structure") or ""
    is_option = _is_option_order(row)
    fees_verified = (
        has_fill
        and int(row.get("missing_fees") or 0) == 0
        and int(row.get("invalid_fills") or 0) == 0
    )
    multiplier_verified = (not is_option) or row.get(
        "fill_multipliers_verified"
    ) is True
    evidence_reasons: list[str] = []
    if not has_fill:
        evidence_reasons.append("no_fill_journal")
    if has_fill and not fees_verified:
        evidence_reasons.append("fee_evidence_missing_or_invalid")
    if has_fill and is_option and not multiplier_verified:
        evidence_reasons.append("contract_multiplier_evidence_missing_or_conflicting")
    reconciliation = (
        "verified"
        if has_fill and fees_verified and multiplier_verified
        else "unavailable"
        if not has_fill
        else "partial"
    )
    credit = _is_credit_order(row)
    multiplier = (
        Decimal("1") if not is_option else _decimal(row.get("order_multiplier"))
    )
    realized_pnl = None
    if (
        has_fill
        and fees_verified
        and multiplier_verified
        and exit_quantity > 0
        and multiplier
        and multiplier > 0
    ):
        entry_vwap = entry_units / filled
        exit_vwap = exit_units / exit_quantity
        entry_fees = _decimal(row.get("entry_fees")) or Decimal("0")
        exit_fees = _decimal(row.get("exit_fees")) or Decimal("0")
        gross = (
            (entry_vwap - exit_vwap if credit else exit_vwap - entry_vwap)
            * multiplier
            * exit_quantity
        )
        realized_pnl = _money_decimal(
            gross - entry_fees * exit_quantity / filled - exit_fees
        )
    lifecycle = (
        "staged"
        if not has_fill
        else "closed"
        if remaining == 0 and exit_quantity > 0
        else "partial_exited"
        if exit_quantity > 0
        else "open"
    )
    if row.get("paper_status") in {"cancelled", "rejected"} and not has_fill:
        lifecycle = "staged"
    origin = (
        "ticker_decision_paper_policy"
        if row.get("ticker_decision_id") is not None
        else "event_paper_policy"
        if row.get("event_id") is not None or row.get("event_signal_id") is not None
        else "decision_linked_paper_order"
        if row.get("decision_id") is not None
        else "unattributed_paper_order"
    )
    mark = _paper_mark_payload(
        row,
        remaining=remaining,
        filled=filled,
        entry_price=entry_price,
        credit=credit,
        multiplier_verified=multiplier_verified,
        fees_verified=fees_verified,
    )
    net_pnl = None
    if lifecycle == "closed":
        net_pnl = realized_pnl
    elif remaining > 0 and mark["unrealized_pnl"] is not None:
        if exit_quantity == 0 or realized_pnl is not None:
            net_pnl = _money_decimal(
                (
                    Decimal(str(realized_pnl))
                    if realized_pnl is not None
                    else Decimal("0")
                )
                + Decimal(str(mark["unrealized_pnl"]))
            )
    strategy = {
        "revision_id": str(row["strategy_revision_id"])
        if row.get("strategy_revision_id") is not None
        else None,
        "key": row.get("strategy_key"),
        "revision": row.get("strategy_revision"),
        "name": row.get("strategy_name"),
        "family": row.get("strategy_family") or row.get("order_strategy_family"),
        "model_revision": row.get("model_version"),
    }
    return {
        "record_kind": "paper_trade" if has_fill else "paper_order",
        "paper_order_id": row.get("paper_order_id"),
        "book": row.get("book") or "paper",
        "sleeve": row.get("sleeve"),
        "symbol": row.get("symbol"),
        "instrument_kind": row.get("asset_class"),
        "structure": structure,
        "strike": _number(row.get("strike")),
        "option_type": row.get("option_type"),
        "expiration": row.get("expiration"),
        "decision_id": row.get("decision_id"),
        "strategy": strategy,
        "strategy_revision_id": strategy["revision_id"],
        "decision_at": row.get("decision_at"),
        "staged_at": row.get("staged_at"),
        "exit_at": row.get("paper_exit_at"),
        "lifecycle": lifecycle,
        "paper_status": row.get("paper_status"),
        "origin": origin,
        "authority": "canonical_paper_order_and_trade_journal",
        "requested_quantity": _number(row.get("requested_quantity")),
        "filled_quantity": _number(filled),
        "exited_quantity": _number(exit_quantity),
        "remaining_quantity": _number(remaining),
        "staged_limit_price": _number(row.get("staged_limit_price")),
        "entry_price": _number(entry_price),
        "exit_price": _number(exit_price),
        "realized_pnl": _number(realized_pnl),
        "unrealized_pnl": mark["unrealized_pnl"],
        "net_pnl": _number(net_pnl),
        "initial_risk": _number(
            row.get("planned_loss")
            or row.get("max_loss")
            or row.get("decision_max_loss")
        ),
        "reconciliation_status": reconciliation,
        "evidence_status": reconciliation,
        "evidence_reasons": evidence_reasons
        or ["fill_fee_and_multiplier_evidence_verified"],
        "mark_status": mark["mark_status"],
        "mark_gap_reason": mark["mark_gap_reason"],
        "mark_price": mark["mark_price"],
        "mark_value": mark["mark_value"],
        "mark_observed_at": mark["mark_observed_at"],
        "mark_available_at": mark["mark_available_at"],
        "mark_source": mark["mark_source"],
        "mark_basis": mark["mark_basis"],
        "mark_multiplier": mark["mark_multiplier"],
        "mark_stale": mark["mark_stale"],
        "mark": mark["mark"],
        "decision": {
            "state": row.get("decision_state"),
            "kind": row.get("decision_kind"),
            "lane": row.get("decision_lane") or row.get("lane"),
            "reasons": list(row.get("reasons") or []),
            "blockers": list(row.get("blockers") or []),
            "thesis": row.get("thesis_snapshot") or row.get("thesis_payload") or {},
            "forecast": {
                "probability_profit": _number(row.get("probability_profit")),
                "expected_value": _number(row.get("expected_value")),
                "risk_adjusted_expectancy": _number(
                    row.get("risk_adjusted_expectancy")
                ),
                "data_confidence": _number(row.get("data_confidence")),
                "execution_confidence": _number(row.get("execution_confidence")),
            },
            "evidence": list(row.get("decision_evidence") or []),
        },
        "execution": {
            "paper_only": bool(row.get("paper_only")),
            "side": row.get("side"),
            "structure": structure,
            "staged_at": row.get("staged_at"),
            "staged_limit_price": _number(row.get("staged_limit_price")),
            "entry_price": _number(entry_price),
            "exit_price": _number(exit_price),
            "fees": _number(row.get("actual_fees"))
            if has_fill and fees_verified
            else None,
            "multiplier": _number(multiplier),
            "multiplier_evidence": "verified"
            if multiplier_verified and has_fill
            else "unavailable",
            "ticket_snapshot": row.get("ticket_snapshot") or {},
            "policy_result": row.get("policy_result") or {},
            "fills": list(row.get("fill_rows") or []),
            "legs": list(row.get("order_legs") or []),
        },
        "outcome": {
            "state": row.get("outcome_state"),
            "observed_through": row.get("observed_through"),
            "current_return": _number(row.get("current_return")),
            "realized_exit_return": _number(row.get("realized_exit_return")),
            "realized_exit_basis": row.get("realized_exit_basis"),
            "mae": _number(row.get("mae")),
            "max_drawdown": _number(row.get("outcome_max_drawdown")),
            "accounting_note": "Outcome rows are research context; paper P&L uses verified journal fills only.",
        },
        "related_research": {
            "shadow_id": row.get("shadow_id"),
            "relationship": "same_decision_research_observation"
            if row.get("shadow_id")
            else None,
            "paper_book_inclusion": "paper_order_only",
        },
        "artifacts": {
            "decision_id": row.get("decision_id"),
            "decision_evidence": list(row.get("decision_evidence") or []),
            "ticket_snapshot_present": bool(row.get("ticket_snapshot")),
            "policy_snapshot_present": bool(row.get("policy_snapshot")),
            "source_ids": [
                item.get("reference_key")
                for item in row.get("decision_evidence") or []
                if isinstance(item, dict)
            ],
        },
    }


def _realized_exit_events(
    rows: list[dict[str, Any]],
    fill_rows_by_order: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        if row.get("realized_pnl") is None:
            continue
        fill_rows = (
            fill_rows_by_order.get(str(row["paper_order_id"]), [])
            if fill_rows_by_order is not None
            else [
                fill
                for fill in row.get("execution", {}).get("fills", [])
                if isinstance(fill, dict)
            ]
        )
        if not fill_rows:
            if row.get("exit_at") is not None:
                events.append(
                    {
                        "at": row["exit_at"],
                        "pnl": Decimal(str(row["realized_pnl"])),
                        "trade_id": row["paper_order_id"],
                    }
                )
            continue
        filled = _decimal(row.get("filled_quantity"))
        entry_price = _decimal(row.get("entry_price"))
        multiplier = _decimal(row.get("execution", {}).get("multiplier"))
        if filled is None or filled <= 0 or entry_price is None or multiplier is None:
            continue
        entry_fees = sum(
            (
                _decimal(fill.get("fees")) or Decimal("0")
                for fill in fill_rows
                if fill.get("action") == "paper_entry"
            ),
            Decimal("0"),
        )
        row_events: list[dict[str, Any]] = []
        for fill in fill_rows:
            if not str(fill.get("action") or "").startswith("paper_exit"):
                continue
            quantity = _decimal(fill.get("quantity"))
            price = _decimal(fill.get("price"))
            fee = _decimal(fill.get("fees"))
            at = _as_datetime(fill.get("created_at")) or _as_datetime(
                row.get("exit_at")
            )
            if (
                quantity is None
                or quantity <= 0
                or price is None
                or fee is None
                or at is None
            ):
                row_events = []
                break
            gross = (
                (entry_price - price if _is_credit_order(row) else price - entry_price)
                * multiplier
                * quantity
            )
            row_events.append(
                {
                    "at": at,
                    "pnl": gross - entry_fees * quantity / filled - fee,
                    "trade_id": row["paper_order_id"],
                    "journal_id": fill.get("id"),
                }
            )
        events.extend(row_events)
    events.sort(
        key=lambda event: (
            _as_datetime(event.get("at")) or datetime.max.replace(tzinfo=UTC),
            str(event.get("trade_id") or ""),
            str(event.get("journal_id") or ""),
        )
    )
    return events


def _raw_has_open_quantity(row: dict[str, Any]) -> bool:
    """Only open raw orders need a current mark lookup."""

    entry = _decimal(row.get("entry_quantity")) or Decimal("0")
    exited = _decimal(row.get("exit_quantity")) or Decimal("0")
    return entry > exited


def paper_performance_visuals(
    rows: list[dict[str, Any]],
    exit_events: list[dict[str, Any]] | None = None,
    *,
    fill_rows_by_order: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Return verified event overlays and attribution for a paper scope."""

    realized_rows = [row for row in rows if row.get("realized_pnl") is not None]
    verified_events = (
        _realized_exit_events(realized_rows)
        if exit_events is None
        else exit_events
    )
    return {
        "event_markers": _performance_event_markers(rows, verified_events, fill_rows_by_order),
        "attribution": _performance_attribution(realized_rows),
    }


def _performance_event_markers(
    rows: list[dict[str, Any]],
    exit_events: list[dict[str, Any]],
    fill_rows_by_order: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Build chart overlays without turning unverified fills into P&L."""

    verified_exits = {
        str(event.get("journal_id")): event
        for event in exit_events
        if event.get("journal_id") is not None
    }
    markers: list[dict[str, Any]] = []
    seen_strategies: set[str] = set()
    ordered_rows = sorted(
        rows,
        key=lambda row: _as_datetime(row.get("decision_at"))
        or _as_datetime(row.get("staged_at"))
        or datetime.max.replace(tzinfo=UTC),
    )
    for row in ordered_rows:
        strategy = row.get("strategy") or {}
        strategy_name = strategy.get("name") or strategy.get("key")
        strategy_key = str(strategy.get("revision_id") or strategy_name or "")
        if strategy_key and strategy_key not in seen_strategies:
            seen_strategies.add(strategy_key)
            at = _as_datetime(row.get("decision_at")) or _as_datetime(row.get("staged_at"))
            if at is not None:
                markers.append(
                    {
                        "at": at,
                        "kind": "strategy_observed",
                        "trade_id": row.get("paper_order_id"),
                        "symbol": row.get("symbol"),
                        "strategy": strategy_name,
                        "label": "Strategy first observed",
                        "_pnl": None,
                    }
                )
        fills = (
            fill_rows_by_order.get(str(row.get("paper_order_id")), [])
            if fill_rows_by_order is not None
            else [
                fill
                for fill in (row.get("execution") or {}).get("fills", [])
                if isinstance(fill, dict)
            ]
        )
        for fill in fills:
            action = str(fill.get("action") or "")
            at = _as_datetime(fill.get("created_at"))
            if at is None:
                continue
            if action == "paper_entry":
                markers.append(
                    {
                        "at": at,
                        "kind": "entry",
                        "trade_id": row.get("paper_order_id"),
                        "symbol": row.get("symbol"),
                        "strategy": strategy_name,
                        "price": _number(fill.get("price")),
                        "quantity": _number(fill.get("quantity")),
                        "label": "Entry filled",
                        "_pnl": None,
                    }
                )
            elif action.startswith("paper_exit"):
                verified = verified_exits.get(str(fill.get("id")))
                partial = action != "paper_exit" or row.get("remaining_quantity", 0) > 0
                markers.append(
                    {
                        "at": at,
                        "kind": "partial_exit" if partial else "exit",
                        "trade_id": row.get("paper_order_id"),
                        "symbol": row.get("symbol"),
                        "strategy": strategy_name,
                        "price": _number(fill.get("price")),
                        "quantity": _number(fill.get("quantity")),
                        "label": "Partial exit" if partial else "Exit filled",
                        "status": "verified" if verified else "unavailable",
                        "_pnl": verified.get("pnl") if verified else None,
                    }
                )
        if not fills and row.get("exit_at") is not None:
            matching = next(
                (
                    event
                    for event in exit_events
                    if event.get("trade_id") == row.get("paper_order_id")
                ),
                None,
            )
            markers.append(
                {
                    "at": row["exit_at"],
                    "kind": "exit",
                    "trade_id": row.get("paper_order_id"),
                    "symbol": row.get("symbol"),
                    "strategy": strategy_name,
                    "label": "Exit recorded",
                    "status": "verified" if matching else "unavailable",
                    "_pnl": matching.get("pnl") if matching else None,
                }
            )
        if row.get("remaining_quantity", 0) > 0 and row.get("mark_observed_at") is not None:
            markers.append(
                {
                    "at": row["mark_observed_at"],
                    "kind": "open_position",
                    "trade_id": row.get("paper_order_id"),
                    "symbol": row.get("symbol"),
                    "strategy": strategy_name,
                    "label": "Open position",
                    "status": row.get("mark_status"),
                    "_pnl": None,
                }
            )
    rank = {"strategy_observed": 0, "entry": 1, "partial_exit": 2, "exit": 3, "open_position": 4}
    markers.sort(key=lambda marker: (_as_datetime(marker.get("at")) or datetime.max.replace(tzinfo=UTC), rank.get(str(marker.get("kind")), 9)))
    cumulative = Decimal("0")
    peak = Decimal("0")
    for marker in markers:
        pnl = marker.pop("_pnl", None)
        marker["pnl"] = _money(pnl) if isinstance(pnl, Decimal) else _number(pnl)
        if isinstance(pnl, Decimal):
            cumulative += pnl
        peak = max(peak, cumulative)
        marker["cumulative_net_pnl"] = _money(cumulative)
        marker["drawdown"] = _money(cumulative - peak)
    return markers[:4000]


def _performance_attribution(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return small, verified cohorts for the decision-led decomposition view."""

    dimensions = {
        "strategy": lambda row: (row.get("strategy") or {}).get("name") or (row.get("strategy") or {}).get("key") or "Unattributed",
        "symbol": lambda row: row.get("symbol") or "Unattributed",
        "structure": lambda row: (row.get("execution") or {}).get("structure") or "Unspecified",
        "holding_period": _holding_period_bucket,
        "confidence": _confidence_bucket,
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for dimension, bucket_for in dimensions.items():
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            pnl = _decimal(row.get("realized_pnl"))
            if pnl is None:
                continue
            label = str(bucket_for(row))
            group = groups.setdefault(label, {"label": label, "pnl": Decimal("0"), "trades": 0, "wins": 0})
            group["pnl"] += pnl
            group["trades"] += 1
            group["wins"] += int(pnl > 0)
        result[dimension] = [
            {
                "label": label,
                "pnl": _money(group["pnl"]),
                "trades": group["trades"],
                "wins": group["wins"],
                "win_rate": group["wins"] / group["trades"] if group["trades"] else None,
                "average_pnl": _money(group["pnl"] / group["trades"]) if group["trades"] else None,
            }
            for label, group in sorted(groups.items(), key=lambda item: item[1]["pnl"], reverse=True)
        ]
    return result


def _holding_period_bucket(row: dict[str, Any]) -> str:
    fills = [
        fill
        for fill in (row.get("execution") or {}).get("fills", [])
        if isinstance(fill, dict)
    ]
    entry = next((_as_datetime(fill.get("created_at")) for fill in fills if fill.get("action") == "paper_entry"), None)
    exits = [_as_datetime(fill.get("created_at")) for fill in fills if str(fill.get("action") or "").startswith("paper_exit")]
    exit_at = max((value for value in exits if value is not None), default=_as_datetime(row.get("exit_at")))
    if entry is None or exit_at is None or exit_at < entry:
        return "Unavailable"
    days = (exit_at - entry).total_seconds() / 86_400
    if days <= 1:
        return "1 day or less"
    if days <= 5:
        return "2–5 days"
    if days <= 20:
        return "6–20 days"
    return "21+ days"


def _confidence_bucket(row: dict[str, Any]) -> str:
    probability = _decimal((row.get("decision") or {}).get("forecast", {}).get("probability_profit"))
    if probability is None:
        return "Unavailable"
    if probability < Decimal("0.5"):
        return "Below 50%"
    if probability < Decimal("0.65"):
        return "50–65%"
    if probability < Decimal("0.8"):
        return "65–80%"
    return "80%+"


def _paper_mark_payload(
    row: dict[str, Any],
    *,
    remaining: Decimal,
    filled: Decimal,
    entry_price: Decimal | None,
    credit: bool,
    multiplier_verified: bool,
    fees_verified: bool,
) -> dict[str, Any]:
    empty = {
        "unrealized_pnl": None,
        "mark_status": "not_required" if remaining <= 0 else "unavailable",
        "mark_gap_reason": None
        if remaining <= 0
        else "verified_current_mark_unavailable",
        "mark_price": None,
        "mark_value": None,
        "mark_observed_at": None,
        "mark_available_at": None,
        "mark_source": None,
        "mark_basis": None,
        "mark_multiplier": None,
        "mark_stale": None,
        "mark": {
            "status": "not_required" if remaining <= 0 else "unavailable",
            "basis": None,
            "price": None,
            "value": None,
            "observed_at": None,
            "available_at": None,
            "source": None,
            "stale": None,
        },
    }
    if remaining <= 0:
        return empty

    as_of = _as_datetime(row.get("mark_as_of")) or datetime.now(UTC)
    is_option = _is_option_order(row)
    legs = _order_legs(row)
    if is_option and legs:
        mark_rows = {
            int(mark_row["contract_id"]): mark_row
            for mark_row in row.get("option_marks") or []
            if isinstance(mark_row, dict) and mark_row.get("contract_id") is not None
        }
        missing_contract_ids = [
            int(leg["contract_id"])
            for leg in legs
            if leg.get("contract_id") is None
            or int(leg["contract_id"]) not in mark_rows
        ]
        if missing_contract_ids:
            empty["mark_gap_reason"] = "verified_option_leg_quote_unavailable"
            empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
            return empty

        signed_leg_values: list[Decimal] = []
        observed_values: list[datetime] = []
        available_values: list[datetime] = []
        sources: list[str] = []
        used_mid = False
        leg_evidence: list[dict[str, Any]] = []
        package_multiplier: Decimal | None = None
        for leg in legs:
            contract_id = int(leg["contract_id"])
            mark_row = mark_rows[contract_id]
            expiration = _as_date(leg.get("expiration"))
            if _option_expired(expiration, as_of):
                empty["mark_gap_reason"] = "option_expired_without_settlement_evidence"
                empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
                return empty
            bid = _decimal(mark_row.get("bid"))
            ask = _decimal(mark_row.get("ask"))
            mid = _decimal(mark_row.get("mid"))
            short = str(leg.get("side") or "").lower() in {"short", "sell"}
            if bid is not None and ask is not None and bid > 0 and ask >= bid:
                leg_price = ask if short else bid
            elif mid is not None and mid > 0:
                leg_price = mid
                used_mid = True
            else:
                empty["mark_gap_reason"] = "verified_option_leg_quote_unavailable"
                empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
                return empty
            leg_multiplier = _decimal(leg.get("multiplier"))
            if leg_multiplier is None or leg_multiplier <= 0:
                empty["mark_status"] = "unreconciled"
                empty["mark_gap_reason"] = (
                    "contract_multiplier_evidence_missing_or_conflicting"
                )
                empty["mark"]["status"] = "unreconciled"
                empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
                return empty
            order_multiplier = _decimal(row.get("order_multiplier"))
            if order_multiplier is None or leg_multiplier != order_multiplier:
                empty["mark_status"] = "unreconciled"
                empty["mark_gap_reason"] = (
                    "contract_multiplier_evidence_missing_or_conflicting"
                )
                empty["mark"]["status"] = "unreconciled"
                empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
                return empty
            if package_multiplier is None:
                package_multiplier = leg_multiplier
            elif package_multiplier != leg_multiplier:
                empty["mark_status"] = "unreconciled"
                empty["mark_gap_reason"] = (
                    "contract_multiplier_evidence_missing_or_conflicting"
                )
                empty["mark"]["status"] = "unreconciled"
                empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
                return empty
            signed_leg_values.append(
                (-leg_price if short else leg_price) * leg_multiplier
            )
            observed = _as_datetime(mark_row.get("observed_at"))
            available = _as_datetime(mark_row.get("available_at"))
            if observed is not None:
                observed_values.append(observed)
            if available is not None:
                available_values.append(available)
            if mark_row.get("source") is not None:
                sources.append(str(mark_row["source"]))
            leg_evidence.append(
                {
                    "contract_id": contract_id,
                    "quote_id": mark_row.get("quote_id"),
                    "snapshot_id": mark_row.get("snapshot_id"),
                    "source": mark_row.get("source"),
                    "source_kind": mark_row.get("source_kind"),
                    "side": leg.get("side"),
                }
            )

        if not multiplier_verified:
            empty["mark_status"] = "unreconciled"
            empty["mark_gap_reason"] = (
                "contract_multiplier_evidence_missing_or_conflicting"
            )
            empty["mark"]["status"] = "unreconciled"
            empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
            return empty
        if package_multiplier is None:
            empty["mark_status"] = "unreconciled"
            empty["mark_gap_reason"] = (
                "contract_multiplier_evidence_missing_or_conflicting"
            )
            empty["mark"]["status"] = "unreconciled"
            empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
            return empty

        package_mark = sum(signed_leg_values, Decimal("0"))
        package_price = abs(package_mark) / package_multiplier
        observed_at = min(observed_values) if observed_values else None
        available_at = max(available_values) if available_values else None
        stale = observed_at is None or as_of - observed_at > MARK_STALE_AFTER
        source_values = list(dict.fromkeys(sources))
        source = (
            source_values[0] if len(source_values) == 1 else "multiple_verified_sources"
        )
        basis = (
            "conservative_liquidation_legs_with_mid_fallback"
            if used_mid
            else "conservative_liquidation_legs"
        )
        base = {
            "mark_price": _number(package_price),
            "mark_value": None,
            "mark_observed_at": observed_at,
            "mark_available_at": available_at,
            "mark_source": source,
            "mark_basis": basis,
            "mark_multiplier": _number(package_multiplier),
            "mark_stale": stale,
            "mark": {
                "status": "stale" if stale else "verified",
                "basis": basis,
                "price": _number(package_price),
                "value": None,
                "observed_at": observed_at,
                "available_at": available_at,
                "source": source,
                "stale": stale,
                "as_of": as_of,
                "signed_value_per_package": _number(package_mark),
                "signed_price_per_package": _number(package_mark / package_multiplier),
                "evidence": {"legs": leg_evidence},
            },
            "unrealized_pnl": None,
            "mark_status": "stale" if stale else "verified",
            "mark_gap_reason": "verified_mark_stale" if stale else None,
        }
        if stale:
            return base

        mark_value = package_mark * remaining
        base["mark_value"] = _number(_money_decimal(mark_value))
        base["mark"]["value"] = base["mark_value"]
        if entry_price is not None and fees_verified:
            entry_value_per_package = entry_price * package_multiplier
            gross_per_unit = (
                package_mark - entry_value_per_package
                if not credit
                else entry_value_per_package + package_mark
            )
            entry_fees = _decimal(row.get("entry_fees")) or Decimal("0")
            base["unrealized_pnl"] = _number(
                _money_decimal(
                    gross_per_unit * remaining - entry_fees * remaining / filled
                )
            )
        return base

    if is_option:
        bid = _decimal(row.get("option_mark_bid"))
        ask = _decimal(row.get("option_mark_ask"))
        mid = _decimal(row.get("option_mark_mid"))
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            mark_price = ask if credit else bid
            basis = (
                "conservative_liquidation_ask"
                if credit
                else "conservative_liquidation_bid"
            )
        elif mid is not None and mid > 0:
            mark_price = mid
            basis = "mid"
        else:
            empty["mark_gap_reason"] = "verified_option_quote_unavailable"
            empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
            return empty
        observed_at = row.get("option_mark_observed_at")
        available_at = row.get("option_mark_available_at")
        source = row.get("option_mark_source")
        evidence = {
            "quote_id": row.get("option_mark_quote_id"),
            "snapshot_id": row.get("option_mark_snapshot_id"),
        }
        expiration = _as_date(row.get("expiration"))
        if _option_expired(expiration, as_of):
            empty["mark_gap_reason"] = "option_expired_without_settlement_evidence"
            empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
            return empty
    else:
        mark_price = _decimal(row.get("stock_mark_price"))
        if mark_price is None or mark_price <= 0:
            empty["mark_gap_reason"] = "verified_current_mark_unavailable"
            empty["mark"]["gap_reason"] = empty["mark_gap_reason"]
            return empty
        basis = "confirmed_quote_price"
        observed_at = row.get("stock_mark_observed_at")
        available_at = row.get("stock_mark_available_at")
        source = row.get("stock_mark_source")
        evidence = {
            "source_kind": row.get("stock_mark_source_kind"),
            "valuation_status": row.get("stock_mark_status"),
        }

    as_of = _as_datetime(row.get("mark_as_of")) or datetime.now(UTC)
    observed = _as_datetime(observed_at)
    stale = observed is None or as_of - observed > MARK_STALE_AFTER
    base = {
        "mark_price": _number(mark_price),
        "mark_value": None,
        "mark_observed_at": observed_at,
        "mark_available_at": available_at,
        "mark_source": source,
        "mark_basis": basis,
        "mark_multiplier": _number(row.get("order_multiplier"))
        if is_option and multiplier_verified
        else 1.0
        if not is_option
        else None,
        "mark_stale": stale,
        "mark": {
            "status": "stale" if stale else "verified",
            "basis": basis,
            "price": _number(mark_price),
            "value": None,
            "observed_at": observed_at,
            "available_at": available_at,
            "source": source,
            "stale": stale,
            "as_of": as_of,
            "evidence": evidence,
        },
        "unrealized_pnl": None,
        "mark_status": "stale" if stale else "verified",
        "mark_gap_reason": "verified_mark_stale" if stale else None,
    }
    if stale:
        return base

    multiplier = (
        Decimal("1") if not is_option else _decimal(row.get("order_multiplier"))
    )
    if multiplier is None or multiplier <= 0 or (is_option and not multiplier_verified):
        base["mark_status"] = "unreconciled"
        base["mark_gap_reason"] = "contract_multiplier_evidence_missing_or_conflicting"
        base["mark"]["status"] = "unreconciled"
        base["mark"]["gap_reason"] = base["mark_gap_reason"]
        return base

    mark_value = (
        mark_price
        * multiplier
        * remaining
        * (Decimal("-1") if credit else Decimal("1"))
    )
    base["mark_value"] = _number(_money_decimal(mark_value))
    base["mark"]["value"] = base["mark_value"]
    if entry_price is not None and fees_verified:
        entry_value = entry_price * multiplier * remaining
        gross = (
            entry_value - (mark_price * multiplier * remaining)
            if credit
            else (mark_price * multiplier * remaining) - entry_value
        )
        entry_fees = _decimal(row.get("entry_fees")) or Decimal("0")
        base["unrealized_pnl"] = _number(
            _money_decimal(gross - entry_fees * remaining / filled)
        )
    return base


def _scope_payload(
    *,
    book: str,
    sleeve: str | None,
    symbol: str | None,
    instrument_kind: str | None,
    strategy_revision: int | None,
    lifecycle: str | None,
    date_from: date | None,
    date_to: date | None,
    lane: str | None,
    structure: str | None,
    evidence_class: str | None,
    reconciliation_status: str | None,
) -> dict[str, Any]:
    scope = _scope_values(
        book=book,
        sleeve=sleeve,
        symbol=symbol,
        instrument_kind=instrument_kind,
        strategy_revision=strategy_revision,
        lifecycle=lifecycle,
        date_from=date_from,
        date_to=date_to,
        lane=lane,
        structure=structure,
        evidence_class=evidence_class,
        reconciliation_status=reconciliation_status,
    )
    scope_id = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {"scope": {**scope, "scope_id": scope_id}}


def _scope_values(
    *,
    book: str,
    sleeve: str | None,
    symbol: str | None,
    instrument_kind: str | None,
    strategy_revision: int | None,
    lifecycle: str | None,
    date_from: date | None,
    date_to: date | None,
    lane: str | None,
    structure: str | None,
    evidence_class: str | None,
    reconciliation_status: str | None,
) -> dict[str, Any]:
    return {
        "book": str(book or "paper").strip().lower(),
        "sleeve": sleeve.strip() if sleeve else None,
        "symbol": symbol.strip().upper() if symbol else None,
        "instrument_kind": instrument_kind.strip().lower() if instrument_kind else None,
        "strategy_revision": strategy_revision,
        "lifecycle": lifecycle,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "lane": lane.strip().lower() if lane else None,
        "structure": structure.strip().lower() if structure else None,
        "evidence_class": evidence_class or reconciliation_status,
    }


def _missing_reasons(rows: list[dict[str, Any]]) -> list[str]:
    reasons = set()
    for row in rows:
        reasons.update(row.get("evidence_reasons") or [])
        if row.get("remaining_quantity", 0) > 0:
            if row.get("mark_status") != "verified":
                reasons.add(
                    row.get("mark_gap_reason") or "verified_current_mark_unavailable"
                )
    return sorted(reasons)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _option_expired(expiration: date | None, as_of: datetime) -> bool:
    return bool(
        expiration is not None
        and as_of >= market_session_bounds(expiration)[1].astimezone(UTC)
    )


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _number(value: Any) -> float | None:
    decimal = _decimal(value)
    if decimal is None:
        return None
    result = float(decimal)
    return result if isfinite(result) else None


def _money_decimal(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _money(value: Decimal) -> float:
    return float(_money_decimal(value))


__all__ = ["CALCULATION_VERSION", "PaperWorkbenchRepository", "paper_trade_payload"]
