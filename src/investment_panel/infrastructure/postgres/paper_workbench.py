"""Read-only paper-book projections backed by the canonical order journal."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isfinite
from typing import Any

from investment_panel.infrastructure.postgres.options_paper_ledger import PAPER_FILL_MULTIPLIERS_SQL
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE


CALCULATION_VERSION = "paper-workbench.v1"
MAX_PERFORMANCE_ROWS = 10_000


class PaperWorkbenchRepository:
    """Compose paper views without making shadow or outcome rows authoritative."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def trades(
        self,
        *,
        symbol: str | None = None,
        strategy_revision: int | None = None,
        lifecycle: str | None = None,
        limit: int = 100,
        cursor: tuple[datetime, str] | None = None,
    ) -> dict[str, Any]:
        where, params = _where_clause(symbol=symbol, strategy_revision=strategy_revision, lifecycle=lifecycle)
        scope_where, scope_params = list(where), list(params)
        if cursor is not None:
            where.append("(paper.created_at, paper.id) < (%s, %s::uuid)")
            params.extend([cursor[0], cursor[1]])
        rows, total, _pending, watermark = self._rows(
            where,
            params,
            limit=max(1, min(100, limit)),
            count_where=scope_where,
            count_params=scope_params,
        )
        return {
            **_scope_payload(symbol=symbol, strategy_revision=strategy_revision, lifecycle=lifecycle),
            "as_of": datetime.now(UTC),
            "source_watermark": watermark,
            "calculation_version": CALCULATION_VERSION,
            "counts": {"total": total, "eligible": total, "pending": _pending, "excluded": 0},
            "quality_status": "complete" if len(rows) == total else "partial",
            "missing_evidence_reasons": _missing_reasons(rows),
            "rows": rows,
        }

    def trade(self, trade_id: str) -> dict[str, Any] | None:
        where = ["paper.id = %s::uuid"]
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(self._select_sql(where), [trade_id]).fetchone()
        return paper_trade_payload(dict(row)) if row else None

    def performance(
        self,
        *,
        symbol: str | None = None,
        strategy_revision: int | None = None,
        lifecycle: str | None = None,
    ) -> dict[str, Any]:
        where, params = _where_clause(symbol=symbol, strategy_revision=strategy_revision, lifecycle=lifecycle)
        rows, total, _pending, watermark = self._rows(where, params, limit=MAX_PERFORMANCE_ROWS)
        filled = [row for row in rows if row["filled_quantity"] > 0]
        closed = [row for row in rows if row["lifecycle"] == "closed"]
        realized_rows = [row for row in rows if row["realized_pnl"] is not None]
        realized = sum((Decimal(str(row["realized_pnl"])) for row in realized_rows), Decimal("0"))
        series_rows = sorted(
            (row for row in realized_rows if row.get("exit_at") is not None),
            key=lambda row: (str(row["exit_at"]), row["paper_order_id"]),
        )
        cumulative = Decimal("0")
        series = []
        for row in series_rows:
            cumulative += Decimal(str(row["realized_pnl"]))
            series.append({"at": row["exit_at"], "cumulative_net_pnl": _money(cumulative), "trade_id": row["paper_order_id"]})
        missing = _missing_reasons(rows)
        if "opening_capital_unavailable" not in missing:
            missing.append("opening_capital_unavailable")
        if total > MAX_PERFORMANCE_ROWS:
            missing.append("performance_population_bounded_at_10000_rows")
        known_realized = _money(realized) if not filled or realized_rows else None
        has_open_exposure = any(row["remaining_quantity"] > 0 for row in rows)
        net_pnl = known_realized if not has_open_exposure and len(realized_rows) == len(filled) else None
        return {
            **_scope_payload(symbol=symbol, strategy_revision=strategy_revision, lifecycle=lifecycle),
            "as_of": datetime.now(UTC),
            "source_watermark": watermark,
            "calculation_version": CALCULATION_VERSION,
            "currency": "USD",
            "accounting_basis": "app.paper_order plus deterministic app.trade_journal fills, fees, and multiplier evidence",
            "counts": {
                "total_orders": total,
                "filled_orders": len(filled),
                "closed_trades": len(closed),
                "open_trades": sum(row["lifecycle"] in {"open", "partial_exited"} for row in rows),
                "staged_orders": sum(row["lifecycle"] == "staged" for row in rows),
                "realized_pnl_known": len(realized_rows),
                "realized_pnl_unknown": max(len(filled) - len(realized_rows), 0),
            },
            "net_pnl": net_pnl,
            "realized_pnl": known_realized,
            "unrealized_pnl": None,
            "nav": None,
            "return_pct": None,
            "drawdown": None,
            "evidence_coverage": {
                "filled_orders": len(filled),
                "reconciled_orders": sum(row["reconciliation_status"] == "verified" for row in filled),
                "realized_pnl_coverage": len(realized_rows) / len(filled) if filled else None,
                "mark_coverage": 0.0,
            },
            "quality_status": "partial" if missing or total > MAX_PERFORMANCE_ROWS else "complete",
            "missing_evidence_reasons": sorted(set(missing)),
            "series": {
                "kind": "cumulative_verified_realized_net_pnl",
                "points": series,
                "gaps": [{"reason": "open_positions_without_verified_marks"}] if any(row["remaining_quantity"] > 0 for row in rows) else [],
                "statistics_basis": "full scoped paper-order population; no annualized statistics",
            },
        }

    def _rows(
        self,
        where: list[str],
        params: list[Any],
        *,
        limit: int,
        count_where: list[str] | None = None,
        count_params: list[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, datetime | None]:
        with self.runtime.read(JOB_PROFILE) as connection:
            count_row = connection.execute(
                f"""SELECT count(*) AS count,
                           count(*) FILTER (WHERE paper.status = ANY(%s::text[])) AS pending,
                           max(greatest(paper.created_at, coalesce(paper.updated_at, paper.created_at))) AS source_watermark
                    FROM app.paper_order paper
                    JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                    LEFT JOIN analysis.decision decision ON decision.id = paper.decision_id
                    {('WHERE ' + ' AND '.join(count_where if count_where is not None else where)) if (count_where or where) else ''}""",
                [["staged", "pending", "submitted", "cancelled", "rejected"], *(count_params if count_params is not None else params)],
            ).fetchone()
            rows = connection.execute(self._select_sql(where) + " ORDER BY paper.created_at DESC, paper.id DESC LIMIT %s", [*params, limit]).fetchall()
        return (
            [paper_trade_payload(dict(row)) for row in rows],
            int(count_row["count"]),
            int(count_row["pending"]),
            count_row["source_watermark"],
        )

    @staticmethod
    def _select_sql(where: list[str]) -> str:
        return f"""
            SELECT paper.id::text AS paper_order_id,
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
                   fills.entry_quantity,
                   fills.exit_quantity,
                   fills.entry_units,
                   fills.exit_units,
                   fills.actual_fees,
                   fills.entry_fees,
                   fills.exit_fees,
                   fills.missing_fees,
                       fills.invalid_fills,
                       fills.fill_multipliers_verified,
                       fills.latest_fill_at,
                       fills.journal_ids,
                   fills.fill_rows
            FROM app.paper_order paper
            JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
            LEFT JOIN analysis.decision decision ON decision.id = paper.decision_id
            LEFT JOIN analysis.strategy_revision revision ON revision.id = decision.strategy_revision_id
            LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = paper.decision_id
            LEFT JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
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
            LEFT JOIN LATERAL (
                SELECT coalesce(sum(journal.quantity) FILTER (WHERE journal.action = 'paper_entry'), 0) AS entry_quantity,
                       coalesce(sum(journal.quantity) FILTER (WHERE journal.action <> 'paper_entry'), 0) AS exit_quantity,
                       coalesce(sum(journal.quantity * journal.price) FILTER (WHERE journal.action = 'paper_entry'), 0) AS entry_units,
                       coalesce(sum(journal.quantity * journal.price) FILTER (WHERE journal.action <> 'paper_entry'), 0) AS exit_units,
                       coalesce(sum((journal.details->>'fees')::numeric) FILTER (WHERE journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0) AS actual_fees,
                       coalesce(sum((journal.details->>'fees')::numeric) FILTER (WHERE journal.action = 'paper_entry' AND journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0) AS entry_fees,
                       coalesce(sum((journal.details->>'fees')::numeric) FILTER (WHERE journal.action <> 'paper_entry' AND journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0) AS exit_fees,
                       count(*) FILTER (WHERE NOT coalesce(journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$', false)) AS missing_fees,
                       count(*) FILTER (WHERE NOT coalesce(journal.quantity > 0 AND journal.quantity < 'Infinity'::numeric AND journal.price >= 0 AND journal.price < 'Infinity'::numeric AND (journal.action <> 'paper_entry' OR journal.price > 0), false)) AS invalid_fills,
                       max(journal.created_at) AS latest_fill_at,
                       CASE WHEN count(journal.id) = 0 THEN NULL ELSE {PAPER_FILL_MULTIPLIERS_SQL.replace('action', 'journal.action').replace('details', 'journal.details').replace('paper.contract_multiplier', 'paper.contract_multiplier')} END AS fill_multipliers_verified,
                       coalesce(array_agg(journal.id::text ORDER BY journal.created_at, journal.id), ARRAY[]::text[]) AS journal_ids,
                       coalesce(jsonb_agg(jsonb_build_object(
                           'id', journal.id::text,
                           'action', journal.action,
                           'quantity', journal.quantity,
                           'price', journal.price,
                           'created_at', journal.created_at,
                           'fees', journal.details->'fees',
                           'contract_multiplier', journal.details->'contract_multiplier',
                           'entry_contract_multiplier', journal.details->'entry_contract_multiplier',
                           'exit_contract_multiplier', journal.details->'exit_contract_multiplier'
                       ) ORDER BY journal.created_at, journal.id) FILTER (WHERE journal.id IS NOT NULL), '[]'::jsonb) AS fill_rows
                FROM app.trade_journal journal
                WHERE journal.details->>'paper_order_id' = paper.id::text
                  AND journal.decision_id IS NOT DISTINCT FROM paper.decision_id
                  AND journal.instrument_id = paper.instrument_id
                  AND journal.rationale = 'deterministic_options_paper_execution'
                  AND (journal.action = 'paper_entry' OR journal.action = 'paper_exit' OR journal.action LIKE 'paper_exit:%%')
            ) fills ON TRUE
            {('WHERE ' + ' AND '.join(where)) if where else ''}
        """


def _where_clause(*, symbol: str | None, strategy_revision: int | None, lifecycle: str | None) -> tuple[list[str], list[Any]]:
    where = ["paper.paper_only IS TRUE"]
    params: list[Any] = []
    if symbol:
        where.append("instrument.symbol = %s")
        params.append(symbol.strip().upper())
    if strategy_revision is not None:
        where.append("decision.strategy_revision_id = %s")
        params.append(strategy_revision)
    if lifecycle:
        status_map = {
            "staged": ("staged", "pending", "submitted"),
            "open": ("open", "entered", "partial_exited"),
            "closed": ("closed", "exited", "invalidated"),
        }
        statuses = status_map.get(lifecycle)
        if statuses is None:
            raise ValueError("unsupported paper lifecycle")
        where.append("paper.status = ANY(%s::text[])")
        params.append(list(statuses))
    return where, params


def paper_trade_payload(row: dict[str, Any]) -> dict[str, Any]:
    entry_quantity = _decimal(row.get("entry_quantity")) or Decimal("0")
    exit_quantity = _decimal(row.get("exit_quantity")) or Decimal("0")
    entry_units = _decimal(row.get("entry_units")) or Decimal("0")
    exit_units = _decimal(row.get("exit_units")) or Decimal("0")
    filled = max(entry_quantity, Decimal("0"))
    remaining = max(filled - max(exit_quantity, Decimal("0")), Decimal("0"))
    has_fill = filled > 0
    entry_price = entry_units / filled if has_fill and entry_units >= 0 else None
    exit_price = exit_units / exit_quantity if exit_quantity > 0 and exit_units >= 0 else None
    structure = row.get("structure") or row.get("decision_structure") or ""
    is_option = row.get("contract_id") is not None or structure in {
        "cash_secured_put",
        "put_credit_spread",
        "call_credit_spread",
        "long_call",
        "long_put",
        "call_debit_spread",
        "put_debit_spread",
    } or row.get("order_multiplier") is not None
    fees_verified = has_fill and int(row.get("missing_fees") or 0) == 0 and int(row.get("invalid_fills") or 0) == 0
    multiplier_verified = (not is_option) or row.get("fill_multipliers_verified") is True
    evidence_reasons: list[str] = []
    if not has_fill:
        evidence_reasons.append("no_fill_journal")
    if has_fill and not fees_verified:
        evidence_reasons.append("fee_evidence_missing_or_invalid")
    if has_fill and is_option and not multiplier_verified:
        evidence_reasons.append("contract_multiplier_evidence_missing_or_conflicting")
    reconciliation = "verified" if has_fill and fees_verified and multiplier_verified else "unavailable" if not has_fill else "partial"
    credit = structure in {"cash_secured_put", "put_credit_spread", "call_credit_spread"} or str(row.get("side") or "").lower() == "sell"
    multiplier = Decimal("1") if not is_option else _decimal(row.get("order_multiplier"))
    realized_pnl = None
    if has_fill and fees_verified and multiplier_verified and exit_quantity > 0 and multiplier and multiplier > 0:
        entry_vwap = entry_units / filled
        exit_vwap = exit_units / exit_quantity
        entry_fees = _decimal(row.get("entry_fees")) or Decimal("0")
        exit_fees = _decimal(row.get("exit_fees")) or Decimal("0")
        gross = (entry_vwap - exit_vwap if credit else exit_vwap - entry_vwap) * multiplier * exit_quantity
        realized_pnl = _money_decimal(gross - entry_fees * exit_quantity / filled - exit_fees)
    lifecycle = "staged" if not has_fill else "closed" if remaining == 0 and exit_quantity > 0 else "partial_exited" if exit_quantity > 0 else "open"
    if row.get("paper_status") in {"cancelled", "rejected"} and not has_fill:
        lifecycle = "staged"
    net_pnl = realized_pnl if lifecycle == "closed" else None
    strategy = {
        "revision_id": str(row["strategy_revision_id"]) if row.get("strategy_revision_id") is not None else None,
        "key": row.get("strategy_key"),
        "revision": row.get("strategy_revision"),
        "name": row.get("strategy_name"),
        "family": row.get("strategy_family") or row.get("order_strategy_family"),
        "model_revision": row.get("model_version"),
    }
    return {
        "record_kind": "paper_trade",
        "paper_order_id": row.get("paper_order_id"),
        "symbol": row.get("symbol"),
        "instrument_kind": row.get("asset_class"),
        "decision_id": row.get("decision_id"),
        "strategy": strategy,
        "strategy_revision_id": strategy["revision_id"],
        "decision_at": row.get("decision_at"),
        "staged_at": row.get("staged_at"),
        "exit_at": row.get("paper_exit_at"),
        "lifecycle": lifecycle,
        "paper_status": row.get("paper_status"),
        "origin": "manually_staged_paper_order" if row.get("decision_id") else "paper_order",
        "authority": "canonical_paper_order_and_trade_journal",
        "requested_quantity": _number(row.get("requested_quantity")),
        "filled_quantity": _number(filled),
        "exited_quantity": _number(exit_quantity),
        "remaining_quantity": _number(remaining),
        "staged_limit_price": _number(row.get("staged_limit_price")),
        "entry_price": _number(entry_price),
        "exit_price": _number(exit_price),
        "realized_pnl": _number(realized_pnl),
        "unrealized_pnl": None,
        "net_pnl": _number(net_pnl),
        "initial_risk": _number(row.get("planned_loss") or row.get("max_loss") or row.get("decision_max_loss")),
        "reconciliation_status": reconciliation,
        "evidence_status": reconciliation,
        "evidence_reasons": evidence_reasons or ["fill_fee_and_multiplier_evidence_verified"],
        "mark_status": "unavailable",
        "mark_gap_reason": "verified_current_mark_unavailable" if remaining > 0 else None,
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
                "risk_adjusted_expectancy": _number(row.get("risk_adjusted_expectancy")),
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
            "fees": _number(row.get("actual_fees")) if has_fill and fees_verified else None,
            "multiplier": _number(multiplier),
            "multiplier_evidence": "verified" if multiplier_verified and has_fill else "unavailable",
            "ticket_snapshot": row.get("ticket_snapshot") or {},
            "policy_result": row.get("policy_result") or {},
            "fills": list(row.get("fill_rows") or []),
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
            "relationship": "same_decision_research_observation" if row.get("shadow_id") else None,
            "paper_book_inclusion": "paper_order_only",
        },
        "artifacts": {
            "decision_id": row.get("decision_id"),
            "decision_evidence": list(row.get("decision_evidence") or []),
            "ticket_snapshot_present": bool(row.get("ticket_snapshot")),
            "policy_snapshot_present": bool(row.get("policy_snapshot")),
            "source_ids": [item.get("reference_key") for item in row.get("decision_evidence") or [] if isinstance(item, dict)],
        },
    }


def _scope_payload(*, symbol: str | None, strategy_revision: int | None, lifecycle: str | None) -> dict[str, Any]:
    scope = {"symbol": symbol.strip().upper() if symbol else None, "strategy_revision": strategy_revision, "lifecycle": lifecycle}
    scope_id = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return {"scope": {**scope, "scope_id": scope_id}}


def _missing_reasons(rows: list[dict[str, Any]]) -> list[str]:
    reasons = set()
    for row in rows:
        reasons.update(row.get("evidence_reasons") or [])
        if row.get("remaining_quantity", 0) > 0:
            reasons.add("verified_current_mark_unavailable")
    return sorted(reasons)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


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
