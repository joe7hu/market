"""PostgreSQL mutations for journals, alerts, and strategy promotion."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_parameters import (
    EVALUABLE_GATES,
    canonical_gate_name,
    normalize_gates,
)
from investment_panel.core.option_trade_ticket import TICKET_VERSION, execution_policy
from investment_panel.core.decision import is_market_open
from investment_panel.database.options_paper_ledger import (
    acquire_shared_sleeve_lock,
    active_paper_exposure,
    shared_sleeve_blockers,
)
from investment_panel.database.source_health import source_health_blockers


def _v3_paper_readiness(payload: dict[str, Any], evaluated_at: datetime) -> str:
    """Grade an immutable v3 quote package without turning it into a live order."""

    from investment_panel.database.options_publication import as_datetime

    quote_at = as_datetime(payload.get("quote_observed_at"))
    if quote_at is None or (evaluated_at - quote_at).total_seconds() > 5 * 60:
        return "C"
    legs = list(payload.get("leg_quotes") or [])
    if not legs:
        return "C"
    timestamps = []
    for leg in legs:
        bid, ask = leg.get("bid"), leg.get("ask")
        if bid is None or ask is None or float(ask) < float(bid) or leg.get("size_available") is not True:
            return "C"
        observed_at = as_datetime(leg.get("observed_at"))
        if observed_at is not None:
            timestamps.append(observed_at)
            available_at = as_datetime(leg.get("available_at")) or quote_at
            if available_at is None:
                return "C"
            age = (available_at - observed_at).total_seconds()
            if age < 0 or age > 180:
                return "C"
    if len(timestamps) != len(legs):
        return "C"
    if timestamps and (max(timestamps) - min(timestamps)).total_seconds() > 5:
        return "C"
    return "A"


class ActionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def record_trade_journal(
        self,
        *,
        ticker: str,
        contract_id: str,
        event_id: str | None,
        strategy_version: str,
        opportunity: dict[str, Any],
        notes: str,
        action: str = "accepted",
        idempotency_key: str | None = None,
        publication_id: str | None = None,
        expected_contract_version: int | None = None,
    ) -> str:
        with self.runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, ticker, category="journal")
            decision_id = _uuid_or_none(event_id or opportunity.get("opportunity_id"))
            if decision_id is None:
                decision = connection.execute(
                    "SELECT id FROM analysis.decision WHERE instrument_id = %s ORDER BY as_of DESC LIMIT 1",
                    [instrument_id],
                ).fetchone()
                decision_id = decision["id"] if decision else None
            if expected_contract_version is not None and expected_contract_version != 3:
                raise ValueError("stale options-radar contract version")
            if idempotency_key:
                prior = connection.execute(
                    "SELECT id FROM app.trade_journal WHERE details->>'idempotency_key' = %s LIMIT 1",
                    [idempotency_key],
                ).fetchone()
                if prior:
                    return str(prior["id"])
            row = connection.execute(
                """
                INSERT INTO app.trade_journal (decision_id, instrument_id, action, price, rationale, details)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                [
                    decision_id, instrument_id, action,
                    opportunity.get("entry_price") or opportunity.get("premium_mid") or opportunity.get("entry_premium"), notes,
                    Jsonb({
                        "contract_id": contract_id,
                        "strategy_version": strategy_version,
                        "publication_id": publication_id,
                        "contract_version": expected_contract_version,
                        "idempotency_key": idempotency_key,
                        "opportunity": opportunity,
                    }),
                ],
            ).fetchone()
        return str(row["id"])

    def acknowledge_alert(self, alert_id: str) -> bool:
        with self.runtime.transaction() as connection:
            result = connection.execute(
                "UPDATE app.alert SET acknowledged_at = now() WHERE id = %s AND acknowledged_at IS NULL",
                [alert_id],
            )
        return result.rowcount == 1

    def stage_option_paper_entry(
        self,
        *,
        decision_id: UUID,
        idempotency_key: str,
        ticket_version: int,
        quantity: int,
        limit_price: float,
        current_options_risk_sleeve_capital: float | None,
        policy_version: str | None = None,
        daily_loss_halt_pct: float | None = None,
        max_open_positions: int | None = None,
    ) -> dict[str, Any]:
        if ticket_version != TICKET_VERSION:
            raise ValueError("stale option trade ticket version")
        if quantity <= 0:
            raise ValueError("paper quantity must be positive")
        if not isfinite(limit_price) or limit_price <= 0:
            raise ValueError("paper limit price must be positive")
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency key is required")
        with self.runtime.transaction() as connection:
            now = datetime.now(UTC)
            acquire_shared_sleeve_lock(connection)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["paper-order:options-radar"],
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["publication:options-radar"],
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["publication:options-decision-system"],
            )
            prior = connection.execute(
                """
                SELECT id, decision_id, status, reserved_collateral, quantity, limit_price
                FROM app.paper_order WHERE idempotency_key = %s
                """,
                [key],
            ).fetchone()
            if prior:
                same_request = (
                    str(prior["decision_id"]) == str(decision_id)
                    and int(prior["quantity"]) == quantity
                    and abs(float(prior["limit_price"] or 0) - limit_price) <= 1e-6
                )
                if not same_request:
                    raise ValueError("idempotency key was already used for a different paper order request")
                return {
                    "status": str(prior["status"]),
                    "paper_order_id": str(prior["id"]),
                    "reserved_collateral": float(prior["reserved_collateral"] or 0),
                    "quantity": int(prior["quantity"]),
                    "decision_id": str(prior["decision_id"]),
                    "policy_version": policy_version or None,
                    "idempotent_replay": True,
                }
            signal = connection.execute(
                """
                SELECT decision.instrument_id, decision.state, option_decision.paper_state, option_decision.structure,
                       option_decision.entry_price, option_decision.secured_cash,
                       option_decision.max_loss, option_decision.details,
                       publication.id::text AS publication_id, publication.scope AS publication_scope,
                       publication.published_at AS publication_published_at,
                       publication.payload AS publication_payload,
                       coalesce((publication.payload->>'execution_ready')::boolean, false) AS currently_published
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                LEFT JOIN LATERAL (
                    SELECT publication.id, publication.scope, publication.published_at, item.payload
                    FROM app.publication publication
                    JOIN app.publication_content_item item ON item.publication_id = publication.id
                    WHERE publication.status = 'published'
                      AND publication.published_at <= %s
                      AND item.payload->>'decision_id' = decision.id::text
                      AND (
                        (publication.scope = 'options-radar' AND item.model_name = 'option_radar_opportunity')
                        OR (publication.scope = 'options-decision-system' AND item.model_name = 'options_decision_candidate')
                      )
                    ORDER BY publication.published_at DESC NULLS LAST, publication.created_at DESC
                    LIMIT 1
                ) publication ON true
                WHERE decision.id = %s FOR UPDATE OF decision
                """,
                [now, decision_id],
            ).fetchone()
            if signal is None:
                raise ValueError("options-radar signal not found")
            if signal["paper_state"] is not None and str(signal["paper_state"]) != "PAPER_READY":
                raise ValueError("decision is not PAPER_READY")
            if not signal["currently_published"]:
                raise ValueError("signal is stale or not execution-ready in the current publication")
            if str(signal["state"]) != "READY":
                raise ValueError("signal decision state is not READY")
            shadow = connection.execute(
                "SELECT status, entry_at FROM analysis.shadow_trade WHERE decision_id = %s FOR UPDATE",
                [decision_id],
            ).fetchone()
            if signal["paper_state"] is not None:
                if shadow is None or str(shadow["status"]) != "pending":
                    raise ValueError("pending system shadow is required before paper staging")
                if shadow["entry_at"] is not None:
                    raise ValueError("paper entry is stale because the shadow already entered")
            from investment_panel.database.options_publication import contract_readiness

            publication_payload = dict(signal["publication_payload"] or {})
            ticket = dict(publication_payload.get("ticket") or {})
            if not ticket:
                raise ValueError("current publication has no option trade ticket")
            if int(ticket.get("ticket_version") or 0) != ticket_version:
                raise ValueError("option trade ticket was superseded")
            if str(ticket.get("decision_id") or "") != str(decision_id):
                raise ValueError("option trade ticket decision mismatch")
            ticket_policy_version = str(
                ticket.get("policy_version") or ticket.get("risk_policy_version") or ""
            )
            resolution = dict(ticket.get("resolution") or {})
            resolution_policy_version = str(resolution.get("policy_version") or "")
            resolution_revision = str(resolution.get("decision_revision") or "")
            ticket_revision = str(ticket.get("decision_revision") or "")
            if policy_version and policy_version != ticket_policy_version:
                raise ValueError("option trade ticket policy is stale")
            if not ticket_policy_version or resolution_policy_version not in {"", ticket_policy_version}:
                raise ValueError("option trade ticket policy snapshot is inconsistent")
            if ticket_revision and resolution_revision and ticket_revision != resolution_revision:
                raise ValueError("option trade ticket decision revision is inconsistent")
            if str(resolution.get("eligibility") or "").upper() == "BLOCKED":
                raise ValueError("option trade ticket resolution is blocked")
            if str(ticket.get("state") or "") == "READY" and str(resolution.get("eligibility") or "").upper() != "ACTIONABLE":
                raise ValueError("ready option trade ticket resolution is not actionable")
            if str(ticket.get("state") or "") != "READY" or ticket.get("blockers"):
                raise ValueError("option trade ticket is not READY")
            source_ids = {
                str(value).strip()
                for value in (
                    publication_payload.get("data_source"),
                    publication_payload.get("source_id"),
                    publication_payload.get("quote_source"),
                    (ticket.get("provenance") or {}).get("quote_source"),
                )
                if str(value or "").strip()
            }
            if not source_ids:
                raise ValueError("paper action blocked: publication source identity is missing")
            health_blockers = source_health_blockers(self.runtime, sorted(source_ids), evaluated_at=now)
            if health_blockers:
                details = "; ".join(
                    f"{source_id}={','.join(reasons)}"
                    for source_id, reasons in sorted(health_blockers.items())
                )
                raise ValueError(f"paper action blocked by active source health: {details}")
            execution_ready_at = _ticket_timestamp(ticket.get("execution_ready_at"))
            expires_at = _ticket_timestamp(
                ticket.get("expires_at") or (ticket.get("entry") or {}).get("valid_until")
            )
            if execution_ready_at is None or execution_ready_at > now:
                raise ValueError("option trade ticket is not yet execution-ready")
            if expires_at is None or expires_at <= now:
                raise ValueError("option trade ticket has expired")
            ticket = {
                **ticket,
                "publication_lineage": {
                    **dict(ticket.get("publication_lineage") or {}),
                    "publication_id": str(signal["publication_id"]),
                    "publication_scope": str(signal["publication_scope"]),
                    "published_at": (
                        signal["publication_published_at"].isoformat()
                        if signal["publication_published_at"] is not None
                        else None
                    ),
                },
            }
            ticket_risk = dict(ticket.get("risk") or {})
            configured_sleeve = (
                float(current_options_risk_sleeve_capital)
                if current_options_risk_sleeve_capital is not None
                else 0.0
            )
            ticket_sleeve = float(ticket_risk.get("sleeve_capital") or 0.0)
            if (
                not isfinite(configured_sleeve)
                or configured_sleeve <= 0
                or abs(configured_sleeve - ticket_sleeve) > 0.01
            ):
                raise ValueError("option trade ticket does not match the current options risk sleeve")
            conservative_expectancy = float(
                (ticket.get("forecast") or {}).get("lower_confidence_expected_value")
                or 0.0
            )
            if not isfinite(conservative_expectancy) or conservative_expectancy <= 0:
                raise ValueError("positive lower-confidence expectancy is required for paper staging")
            recommended_quantity = int(ticket_risk.get("recommended_quantity") or 0)
            if quantity > recommended_quantity:
                raise ValueError("requested quantity exceeds the ticket recommendation")
            staged_quantity = connection.execute(
                """
                SELECT coalesce(sum(
                  CASE
                    WHEN status IN ('staged', 'open') THEN quantity
                    WHEN status IN ('entered', 'partial_exited') THEN greatest(
                      coalesce(filled_quantity, quantity) - coalesce(exited_quantity, 0), 0
                    )
                    ELSE 0
                  END
                ), 0) AS quantity
                FROM app.paper_order
                WHERE decision_id = %s
                  AND status IN ('staged', 'open', 'entered', 'partial_exited')
                """,
                [decision_id],
            ).fetchone()["quantity"]
            if int(staged_quantity or 0) + quantity > recommended_quantity:
                raise ValueError("active paper quantity would exceed the ticket recommendation")
            ticket_entry = dict(ticket.get("entry") or {})
            ticket_structure = str(ticket.get("structure") or signal["structure"] or "")
            ticket_lane = str(ticket.get("lane") or ("qqq" if str(ticket.get("symbol") or "").upper() == "QQQ" else "radar")).lower()
            if ticket_lane not in {"radar", "qqq"}:
                raise ValueError("only radar and qqq tickets may use this paper-entry path")
            shared_blockers = shared_sleeve_blockers(
                connection,
                now=now,
                lane=ticket_lane,
                sleeve_capital=configured_sleeve,
                daily_loss_halt_pct=daily_loss_halt_pct,
                max_open_positions=max_open_positions,
            )
            if shared_blockers:
                raise ValueError("; ".join(shared_blockers))
            if ticket_structure == "cash_secured_put":
                minimum_credit = float(ticket_entry.get("minimum_credit") or ticket_entry.get("limit_price") or 0)
                if minimum_credit <= 0 or limit_price < minimum_credit:
                    raise ValueError("limit price is below the ticket minimum credit")
            else:
                maximum_chase = float(ticket_entry.get("maximum_chase_price") or 0)
                if maximum_chase <= 0 or limit_price > maximum_chase:
                    raise ValueError("limit price exceeds the ticket maximum chase price")
            current_execution = execution_policy(
                [dict(leg) for leg in ticket.get("legs") or []],
                structure=ticket_structure,
                entry_price=float(ticket_entry.get("limit_price") or signal["entry_price"] or 0),
                market_session="regular" if is_market_open(now) else "closed",
                evaluated_at=now,
            )
            if current_execution["blockers"]:
                raise ValueError("option trade ticket quote package is stale or no longer executable")
            if str(signal["publication_scope"] or "") == "options-decision-system":
                readiness = _v3_paper_readiness(publication_payload, now)
            else:
                readiness = contract_readiness(publication_payload, now)
            if readiness != "A":
                raise ValueError("signal quote is no longer execution-grade")
            structure = str(signal["structure"] or "long_option")
            unit_risk = float(
                ticket_risk.get("one_unit_collateral")
                if structure == "cash_secured_put"
                else ticket_risk.get("one_unit_max_loss")
                or 0
            )
            total_risk = unit_risk * quantity
            collateral = total_risk if structure == "cash_secured_put" else 0.0
            account = connection.execute(
                "SELECT net_liquidation, cash_balance, buying_power, observed_at "
                "FROM raw.broker_account_snapshot ORDER BY observed_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if account is None or account["net_liquidation"] is None:
                raise ValueError("fresh broker NAV and account constraints are required")
            observed_at = account["observed_at"]
            if observed_at is None or abs((now - observed_at).total_seconds()) > 5 * 60:
                raise ValueError("broker account constraints are stale")
            sleeve_capital = configured_sleeve
            if sleeve_capital <= 0:
                raise ValueError("options risk sleeve is not configured")
            nav = float(account["net_liquidation"])
            if not isfinite(nav) or nav <= 0:
                raise ValueError("fresh finite broker NAV is required")
            if sleeve_capital > nav:
                raise ValueError("broker NAV vetoes the configured options sleeve")
            raw_capacity_values = (account["buying_power"], account["cash_balance"])
            if any(value is None for value in raw_capacity_values):
                raise ValueError("finite broker buying power and cash constraints are required")
            account_capacity_values = [float(value) for value in raw_capacity_values]
            if not all(
                isfinite(value) and value >= 0 for value in account_capacity_values
            ):
                raise ValueError("finite broker buying power and cash constraints are required")
            available_account_capital = min(account_capacity_values) if account_capacity_values else 0.0
            symbol = connection.execute(
                "SELECT symbol FROM catalog.instrument WHERE id = %s",
                [signal["instrument_id"]],
            ).fetchone()["symbol"]
            exposures = active_paper_exposure(
                connection,
                symbol=str(symbol),
                instrument_id=int(signal["instrument_id"]),
            )
            if int(exposures["unvalued_commitments"] or 0) > 0:
                raise ValueError("active paper commitment has no authoritative valuation")
            committed_capital = float(exposures["total_committed"] or 0)
            if structure == "cash_secured_put":
                if account["cash_balance"] is None:
                    raise ValueError("current broker cash and NAV are required for a cash-secured put")
                buying_power = (
                    float(account["buying_power"])
                    if account["buying_power"] is not None
                    else float(account["cash_balance"])
                )
                available_cash = min(float(account["cash_balance"]), buying_power)
                reserved = float(exposures["total_csp_collateral"] or 0)
                symbol_reserved = float(exposures["symbol_csp_collateral"] or 0)
                if unit_risk <= 0:
                    raise ValueError("cash-secured-put collateral is unavailable")
                if symbol_reserved + collateral > sleeve_capital * 0.05:
                    raise ValueError("paper quantity exceeds the 5% sleeve symbol collateral limit")
                if reserved + collateral > sleeve_capital * 0.15:
                    raise ValueError("aggregate cash-secured-put collateral would exceed 15% of the sleeve")
                if committed_capital + collateral > available_cash:
                    raise ValueError("insufficient unreserved cash collateral")
            else:
                symbol_risk = float(exposures["symbol_risk"] or 0)
                aggregate_risk = float(exposures["total_risk"] or 0)
                if total_risk > sleeve_capital * 0.0025:
                    raise ValueError("paper quantity exceeds the 0.25% sleeve per-trade limit")
                if symbol_risk + total_risk > sleeve_capital * 0.005:
                    raise ValueError("paper quantity exceeds the 0.50% sleeve symbol-risk limit")
                if aggregate_risk + total_risk > sleeve_capital * 0.01:
                    raise ValueError("paper quantity exceeds the 1.00% sleeve aggregate-risk limit")
                if committed_capital + total_risk > available_account_capital:
                    raise ValueError("current broker buying power vetoes the paper quantity")
            side = "sell" if structure == "cash_secured_put" else "buy"
            policy = {
                "ticket_version": ticket_version,
                "lane": ticket_lane,
                "structure": structure,
                "policy_version": ticket_policy_version,
                "decision_revision": ticket_revision or resolution_revision,
                "resolution": resolution,
                "risk_policy_snapshot": ticket_risk.get("policy_snapshot") or {},
                "fully_cash_secured": structure == "cash_secured_put",
                "live_order_submission": False,
            }
            order_ticket = _ordered_ticket_snapshot(ticket, quantity=quantity, total_risk=total_risk)
            row = connection.execute(
                """
                INSERT INTO app.paper_order
                    (decision_id, instrument_id, side, quantity, limit_price, status,
                     policy_result, policy_snapshot, lane, structure, reserved_collateral, idempotency_key,
                     ticket_version, ticket_snapshot, intended_limit_price)
                VALUES (%s, %s, %s, %s, %s, 'staged', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    decision_id, signal["instrument_id"], side, quantity,
                    limit_price, Jsonb(policy), Jsonb(policy), ticket_lane, structure, collateral or None, key,
                    ticket_version, Jsonb(order_ticket), limit_price,
                ],
            ).fetchone()
            for index, leg in enumerate(ticket.get("legs") or []):
                connection.execute(
                    """
                    INSERT INTO app.paper_order_leg
                        (paper_order_id, leg_index, contract_id, option_type, side, strike,
                         bid, ask, bid_size, ask_size, quote_time, open_interest, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        row["id"], index, int(leg["contract_id"]), leg["option_type"], leg["side"],
                        leg["strike"], leg["bid"], leg["ask"], leg["bid_size"], leg["ask_size"],
                        leg["quote_time"], leg.get("open_interest"), leg.get("volume"),
                    ],
                )
        return {
            "status": "staged",
            "paper_order_id": str(row["id"]),
            "decision_id": str(decision_id),
            "lane": ticket_lane,
            "structure": structure,
            "reserved_collateral": collateral,
            "quantity": quantity,
            "total_risk": total_risk,
            "ticket_version": ticket_version,
            "policy_version": ticket_policy_version,
            "live_order_submission": False,
            "idempotent_replay": False,
        }

    def promote_strategy_proposal(self, proposal_id: str, *, approved_by: str) -> str:
        approver = approved_by.strip()
        if not approver:
            raise ValueError("human approval is required")
        with self.runtime.transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["publication:options-radar"],
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["strategy:options-radar-core"],
            )
            task = connection.execute(
                """
                SELECT id, result FROM analysis.agent_task
                WHERE task_kind IN ('legacy_strategy_mutation_proposal', 'strategy_mutation_proposal')
                  AND (request->>'legacy_id' = %s OR id::text = %s)
                LIMIT 1 FOR UPDATE
                """,
                [proposal_id, proposal_id],
            ).fetchone()
            if task is None:
                raise ValueError(f"strategy proposal not found: {proposal_id}")
            proposal = dict(task["result"] or {})
            proposal_status = str(proposal.get("status") or "").lower()
            if proposal_status == "backtest_required":
                raise ValueError("strategy proposal requires a passing backtest")
            if proposal_status == "forward_test_required":
                raise ValueError("strategy proposal requires a passing forward shadow test")
            if proposal_status not in {"approved", "ready", "forward_test_passed"}:
                raise ValueError("strategy proposal has not passed deterministic approval gates")
            key = str(proposal.get("proposed_strategy_version") or f"proposal-{proposal_id}")
            parameters = proposal.get("proposed_parameter_changes") or {}
            candidate_rows = connection.execute(
                "SELECT id, parameters, supersedes_id, authority_group "
                "FROM analysis.strategy_revision "
                "WHERE strategy_key = %s AND status IN ('candidate', 'testing', 'approved') "
                "ORDER BY revision DESC FOR UPDATE",
                [key],
            ).fetchall()
            requested_candidate_id = proposal.get("candidate_revision_id")
            candidate = next(
                (
                    row for row in candidate_rows
                    if (requested_candidate_id is None or int(row["id"]) == int(requested_candidate_id))
                    and _candidate_contains_changes(dict(row["parameters"] or {}), dict(parameters))
                ),
                None,
            )
            if candidate is None and not candidate_rows:
                raise ValueError("strategy proposal requires a persisted candidate revision")
            if candidate is None:
                raise ValueError("strategy proposal parameters do not match the evaluated candidate revision")
            if candidate["authority_group"] != "options-radar-core":
                raise ValueError("strategy candidate is outside the options-radar-core lineage")
            lineage = connection.execute(
                "SELECT id, status FROM analysis.strategy_revision "
                "WHERE authority_group = 'options-radar-core' FOR UPDATE"
            ).fetchall()
            statuses = {row["id"]: row["status"] for row in lineage}
            parent_id = candidate["supersedes_id"]
            if parent_id is None or statuses.get(parent_id) != "active":
                raise ValueError("strategy candidate base is no longer active; reevaluation is required")
            evaluations = connection.execute(
                "SELECT evaluation_type, verdict FROM analysis.strategy_evaluation "
                "WHERE strategy_revision_id = %s ORDER BY evaluated_at DESC",
                [candidate["id"]],
            ).fetchall()
            passed = {
                str(row["evaluation_type"]).lower()
                for row in evaluations
                if str(row["verdict"] or "").lower() in {"pass", "passed", "approved"}
            }
            if "backtest" not in passed:
                raise ValueError("strategy proposal requires a persisted passing backtest")
            if not passed.intersection({"forward_test", "forward_shadow_test"}):
                raise ValueError("strategy proposal requires a persisted passing forward shadow test")
            active_ids = [
                revision_id for revision_id, status in statuses.items()
                if status == "active" and revision_id != candidate["id"]
            ]
            if active_ids:
                connection.execute(
                    "UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = ANY(%s)",
                    [active_ids],
                )
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'active', promoted_at = now() WHERE id = %s",
                [candidate["id"]],
            )
            connection.execute(
                "UPDATE app.publication SET status = 'superseded' "
                "WHERE scope = 'options-radar' AND status = 'published'"
            )
            connection.execute("DELETE FROM app.current_publication_item WHERE scope = 'options-radar'")
            connection.execute(
                "UPDATE analysis.agent_task SET validation = %s, updated_at = now() WHERE id = %s",
                [Jsonb({"status": "promoted", "approved_by": approver}), task["id"]],
            )
        return key


def _ordered_ticket_snapshot(
    ticket: dict[str, Any],
    *,
    quantity: int,
    total_risk: float,
) -> dict[str, Any]:
    """Return the immutable ticket snapshot adjusted to the submitted quantity."""
    ticket_risk = dict(ticket.get("risk") or {})
    recommended_risk = float(ticket_risk.get("total_risk") or 0.0)
    symbol_before = max(
        float(ticket_risk.get("symbol_exposure_after_entry") or 0.0) - recommended_risk,
        0.0,
    )
    total_before = max(
        float(ticket_risk.get("total_options_exposure_after_entry") or 0.0)
        - recommended_risk,
        0.0,
    )
    return {
        **ticket,
        "risk": {
            **ticket_risk,
            "ordered_quantity": quantity,
            "total_risk": round(total_risk, 2),
            "symbol_exposure_after_entry": round(symbol_before + total_risk, 2),
            "total_options_exposure_after_entry": round(total_before + total_risk, 2),
        },
    }


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _ticket_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _candidate_contains_changes(candidate: dict[str, Any], changes: dict[str, Any]) -> bool:
    gates = normalize_gates(candidate)
    for key, value in changes.items():
        canonical = canonical_gate_name(key)
        actual = gates.get(canonical) if canonical in EVALUABLE_GATES else candidate.get(key)
        if actual != value:
            return False
    return True


ordered_ticket_snapshot = _ordered_ticket_snapshot
v3_paper_readiness = _v3_paper_readiness
