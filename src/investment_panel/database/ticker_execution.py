"""Fail-closed paper staging for the shared ticker decision."""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import floor, isfinite
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.config import AppConfig
from investment_panel.core.decision import (
    CapitalActionType,
    ExpressionKind,
    MarketStateSnapshot,
    OpportunityEpisode,
    PortfolioImpact,
    RiskPolicySnapshot,
    TradePlan,
    TickerDecision,
    trade_expression_identity,
)
from investment_panel.core.options_recovery import FEE_PER_CONTRACT_LEG
from investment_panel.database.options_paper_quotes import is_credit_structure, latest_option_legs, package_price
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


OPTION_EXPRESSIONS = frozenset({
    ExpressionKind.CALL,
    ExpressionKind.PUT,
    ExpressionKind.DEBIT_SPREAD,
    ExpressionKind.CASH_SECURED_PUT,
})
ENTRY_ACTIONS = frozenset({
    CapitalActionType.BUY,
    CapitalActionType.ADD,
    CapitalActionType.HEDGE,
    CapitalActionType.WAIT_FOR_PRICE,
})
EXIT_ACTIONS = frozenset({CapitalActionType.TRIM, CapitalActionType.EXIT})
TERMINAL_STATUSES = frozenset({"exited", "invalidated", "unfilled", "rejected", "unmeasurable"})


class TickerPaperExecutionRepository:
    """Own the paper-only write path for stock and option expressions.

    This repository never imports a broker client and never calls a live order
    API. Existing option routes can delegate here after they resolve their
    legacy decision ID, but they cannot bypass the ticker risk budget.
    """

    def __init__(self, runtime: DatabaseRuntime, config: AppConfig) -> None:
        self.runtime = runtime
        self.config = config

    def stage(
        self,
        *,
        ticker: str,
        decision: TickerDecision,
        expression_kind: str,
        idempotency_key: str,
        quantity: int | None = None,
        limit_price: float | None = None,
        policy_version: str | None = None,
        trade_plan_id: str | None = None,
    ) -> dict[str, Any]:
        symbol = ticker.strip().upper()
        if decision.ticker != symbol:
            raise ValueError("ticker decision does not match the requested ticker")
        if policy_version and policy_version != decision.policy_version:
            raise ValueError("ticker decision policy is stale")
        if decision.resolution is not None:
            if decision.resolution.decision_revision != decision.decision_revision:
                raise ValueError("ticker decision resolution revision is inconsistent")
            if decision.resolution.policy_version != decision.policy_version:
                raise ValueError("ticker decision resolution policy is inconsistent")
            if decision.resolution.is_blocked:
                raise ValueError("ticker decision is blocked")
            if not decision.resolution.is_actionable:
                raise ValueError("ticker decision is not actionable")
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        plan = decision.trade_plan
        if plan is None or not trade_plan_id or trade_plan_id.strip() != plan.trade_plan_id:
            raise ValueError("ticker trade plan is missing or stale")
        if plan.eligibility != "ACTIONABLE" or plan.selected_expression_kind is ExpressionKind.CASH:
            raise ValueError("ticker trade plan is blocked")
        try:
            kind = ExpressionKind(str(expression_kind).upper())
        except ValueError as exc:
            raise ValueError("unsupported ticker expression") from exc
        if kind is ExpressionKind.CASH:
            raise ValueError("cash is a competing expression, not a paper order")
        if kind is not plan.selected_expression_kind:
            raise ValueError("requested expression does not match the trade plan")
        self._validate_decision_context(decision, kind)
        self._check_switches(kind)
        expression = plan.selected_expression
        if expression is None:
            raise ValueError("trade plan expression is missing")
        if decision.selected_expression is None or decision.selected_expression.kind is not kind:
            raise ValueError("requested expression is not the selected ticker expression")
        if expression.model_dump(mode="json") != decision.selected_expression.model_dump(mode="json"):
            raise ValueError("trade plan expression is stale")
        if expression.status != "eligible":
            raise ValueError("requested expression is not eligible in the current ticker decision")
        if kind in OPTION_EXPRESSIONS and not _complete_option_legs(expression.legs):
            raise ValueError("a complete executable option leg package is required before paper staging")
        try:
            action = CapitalActionType(plan.action)
        except ValueError as exc:
            raise ValueError("trade plan action is not a supported paper action") from exc
        if action not in ENTRY_ACTIONS | EXIT_ACTIONS:
            raise ValueError(f"capital action {action.value} does not stage an order")
        requested_revision = decision.decision_revision
        if quantity is not None and quantity != plan.quantity:
            raise ValueError("requested quantity does not match the trade plan")
        requested_quantity = plan.quantity
        if requested_quantity is None:
            raise ValueError("trade plan quantity is unavailable")
        if requested_quantity <= 0 or requested_quantity > int(expression.quantity or 0):
            raise ValueError("requested quantity exceeds the current ticker expression authorization")
        if limit_price is not None and (plan.entry_limit is None or float(limit_price) != float(plan.entry_limit)):
            raise ValueError("requested limit price does not match the trade plan")
        limit = plan.entry_limit
        if limit is None or not isfinite(float(limit)) or float(limit) <= 0:
            raise ValueError("trade plan entry limit is unavailable")
        max_loss = plan.max_loss_per_unit
        if max_loss is None or max_loss <= 0:
            raise ValueError("trade plan maximum loss is unavailable")
        planned_loss = float(plan.planned_loss or 0.0) if action in ENTRY_ACTIONS else 0.0
        now = datetime.now(UTC)
        expires_at = plan.expiry
        nav = decision.risk_policy.loss_budget / decision.risk_policy.loss_budget_pct if decision.risk_policy.loss_budget is not None else None
        if nav is None or nav <= 0:
            raise ValueError("fresh broker NAV is required before paper staging")
        if planned_loss > nav * decision.risk_policy.max_ticker_loss_pct:
            raise ValueError("ticker paper order exceeds the combined ticker loss budget")
        side = "sell" if kind is ExpressionKind.CASH_SECURED_PUT else "buy" if action in ENTRY_ACTIONS else "sell"
        structure = _option_structure(kind) if kind in OPTION_EXPRESSIONS else None

        with self.runtime.transaction() as connection:
            instrument = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = %s AND asset_class IN ('equity', 'etf') LIMIT 1",
                [symbol],
            ).fetchone()
            if instrument is None:
                raise ValueError("ticker instrument is not in the catalog")
            ticker_decision = connection.execute(
                """
                SELECT id::text, decision_revision, policy_version, resolution,
                       opportunity_episode_id, opportunity_cutoff, opportunity_episode,
                       market_state_publication_id::text, market_state_snapshot,
                       portfolio_impacts, risk_policy_snapshot
                FROM analysis.ticker_decision
                WHERE instrument_id = %s AND decision_revision = %s
                LIMIT 1
                """,
                [instrument["id"], requested_revision],
            ).fetchone()
            if ticker_decision is None:
                raise ValueError("ticker decision context is missing in PostgreSQL")
            if str(ticker_decision["decision_revision"]) != requested_revision:
                raise ValueError("ticker decision revision is stale in PostgreSQL")
            if str(ticker_decision["policy_version"] or "") != decision.policy_version:
                raise ValueError("ticker decision policy is stale in PostgreSQL")
            persisted_resolution = dict(ticker_decision["resolution"] or {})
            if persisted_resolution and (
                str(persisted_resolution.get("decision_revision") or "") != requested_revision
                or str(persisted_resolution.get("policy_version") or "") != decision.policy_version
            ):
                raise ValueError("ticker decision resolution is stale in PostgreSQL")
            if str(persisted_resolution.get("trade_plan_id") or "") != plan.trade_plan_id:
                raise ValueError("ticker decision trade plan is stale in PostgreSQL")
            rank_evidence = self._validate_persisted_context(
                connection, ticker_decision, decision, kind, plan.trade_plan_id,
            )
            if action in EXIT_ACTIONS:
                if not decision.capital_action.owned:
                    raise ValueError("TRIM and EXIT require an existing paper position")
                active = connection.execute(
                    """
                    SELECT coalesce(sum(
                        CASE
                          WHEN status IN ('staged', 'open') THEN quantity
                          WHEN status IN ('entered', 'partial_exited')
                            THEN greatest(coalesce(filled_quantity, quantity) - coalesce(exited_quantity, 0), 0)
                          ELSE 0
                        END
                    ), 0) AS quantity
                    FROM app.paper_order
                    WHERE instrument_id = %s
                      AND paper_only = TRUE
                      AND status IN ('staged', 'open', 'entered', 'partial_exited')
                      AND side = 'buy'
                    """,
                    [instrument["id"]],
                ).fetchone()
                if float(active["quantity"] or 0) < requested_quantity:
                    raise ValueError("TRIM and EXIT quantity exceeds the existing paper position")
            prior = connection.execute(
                """
                SELECT id, status, quantity, limit_price, ticker_decision_revision,
                       expression_kind, planned_loss, policy_result
                FROM app.paper_order
                WHERE lane = 'ticker'
                  AND (idempotency_key = %s OR policy_result->>'trade_plan_id' = %s)
                ORDER BY created_at, id
                LIMIT 1
                """,
                [plan.trade_plan_id, plan.trade_plan_id],
            ).fetchone()
            if prior:
                same = (
                    str(prior["ticker_decision_revision"] or "") == requested_revision
                    and str(prior["expression_kind"] or "") == kind.value
                    and int(prior["quantity"] or 0) == requested_quantity
                    and abs(float(prior["limit_price"] or 0) - float(limit)) <= 1e-6
                )
                if not same:
                    raise ValueError("idempotency key was already used for a different ticker paper request")
                return {
                    "status": str(prior["status"]),
                    "paper_order_id": str(prior["id"]),
                    "ticker": symbol,
                    "expression_kind": kind.value,
                    "quantity": int(prior["quantity"]),
                    "planned_loss": float(prior["planned_loss"] or planned_loss),
                    "decision_revision": requested_revision,
                    "policy_version": decision.policy_version,
                    "trade_plan_id": plan.trade_plan_id,
                    "trade_plan_publication_id": rank_evidence["plan_publication_id"],
                    "idempotent_replay": True,
                    "paper_only": True,
                }
            open_risk = connection.execute(
                """
                SELECT coalesce(sum(planned_loss), 0) AS planned_loss
                FROM app.paper_order
                WHERE instrument_id = %s
                  AND status IN ('staged', 'open', 'entered', 'partial_exited')
                """,
                [instrument["id"]],
            ).fetchone()
            if float(open_risk["planned_loss"] or 0) + planned_loss > nav * decision.risk_policy.max_ticker_loss_pct + 1e-6:
                raise ValueError("open paper orders already consume the ticker loss budget")
            total_risk = connection.execute(
                """
                SELECT coalesce(sum(planned_loss), 0) AS planned_loss
                FROM app.paper_order
                WHERE status IN ('staged', 'open', 'entered', 'partial_exited')
                """
            ).fetchone()
            if float(total_risk["planned_loss"] or 0) + planned_loss > nav * decision.risk_policy.max_total_open_planned_loss_pct + 1e-6:
                raise ValueError("open paper orders already consume the total planned-loss limit")
            policy = {
                "owner": "ticker-first",
                "decision_revision": requested_revision,
                "policy_version": decision.policy_version,
                "input_hash": decision.input_manifest.input_hash,
                "capital_action": decision.capital_action.action.value,
                "expression_kind": kind.value,
                "max_loss_per_unit": max_loss,
                "planned_loss": planned_loss,
                "nav": nav,
                "ranking_publication_id": rank_evidence["publication_id"],
                "opportunity_rank": rank_evidence["payload"],
                "trade_rank": rank_evidence["payload"].get("trade_rank"),
                "trade_utility": rank_evidence["payload"].get("trade_utility"),
                "trade_plan_id": plan.trade_plan_id,
                "trade_plan_publication_id": rank_evidence["plan_publication_id"],
                "trade_plan": rank_evidence["plan_payload"],
                "caller_idempotency_key": idempotency_key.strip(),
                "live_order_submission": False,
                "entry_fill_count": 0,
                "exit_fill_count": 0,
            }
            snapshot = decision.model_dump(mode="json")
            snapshot.update({
                "ranking_publication_id": rank_evidence["publication_id"],
                "opportunity_rank": rank_evidence["payload"],
                "trade_plan_publication_id": rank_evidence["plan_publication_id"],
                "trade_plan": rank_evidence["plan_payload"],
            })
            row = connection.execute(
                """
                INSERT INTO app.paper_order (
                    decision_id, ticker_decision_id, instrument_id, created_at, side, quantity, limit_price,
                    status, policy_result, policy_snapshot, lane, idempotency_key, ticker_decision_revision,
                    expression_kind, max_loss, planned_loss, expires_at, thesis_snapshot,
                    structure, ticket_snapshot, intended_limit_price, paper_only
                ) VALUES (
                    NULL, %s::uuid, %s, %s, %s, %s, %s, 'staged', %s, %s, 'ticker', %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, TRUE
                )
                RETURNING id
                """,
                [
                    ticker_decision["id"] if ticker_decision else None,
                    instrument["id"], now, side, requested_quantity, float(limit),
                    Jsonb(policy), Jsonb(policy), plan.trade_plan_id, requested_revision, kind.value,
                    float(max_loss), planned_loss, expires_at, Jsonb(snapshot),
                    structure, Jsonb(snapshot), float(limit),
                ],
            ).fetchone()
            if kind in OPTION_EXPRESSIONS:
                for index, leg in enumerate(expression.legs):
                    connection.execute(
                        """
                        INSERT INTO app.paper_order_leg
                            (paper_order_id, leg_index, contract_id, option_type, side, strike,
                             bid, ask, bid_size, ask_size, quote_time, open_interest, volume)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            row["id"], index, int(leg["contract_id"]), str(leg.get("option_type") or ""),
                            str(leg.get("side") or "long"), float(leg.get("strike") or 0),
                            float(leg["bid"]), float(leg["ask"]), int(leg["bid_size"]), int(leg["ask_size"]),
                            _timestamp(leg.get("quote_time") or leg.get("observed_at")),
                            leg.get("open_interest"), leg.get("volume"),
                        ],
                    )
        return {
            "status": "staged",
            "paper_order_id": str(row["id"]),
            "ticker": symbol,
            "expression_kind": kind.value,
            "quantity": requested_quantity,
            "limit_price": float(limit),
            "planned_loss": planned_loss,
            "decision_revision": requested_revision,
            "policy_version": decision.policy_version,
            "trade_plan_id": plan.trade_plan_id,
            "trade_plan_publication_id": rank_evidence["plan_publication_id"],
            "paper_only": True,
            "live_order_submission": False,
        }

    @staticmethod
    def _validate_decision_context(decision: TickerDecision, kind: ExpressionKind) -> None:
        now = datetime.now(UTC)
        if decision.cutoff > now:
            raise ValueError("ticker decision cutoff is in the future")
        if decision.context_blockers:
            raise ValueError("ticker decision context is blocked: " + ", ".join(decision.context_blockers))
        snapshot = decision.market_state_snapshot
        if snapshot is None or not decision.market_state_publication_id:
            raise ValueError("market snapshot publication context is required")
        impact = decision.portfolio_impacts.get(kind)
        if impact is None:
            raise ValueError("selected expression portfolio impact is missing")
        expression = decision.expressions.get(kind)
        if expression is None or impact.expression_identity != trade_expression_identity(expression):
            raise ValueError("selected expression portfolio impact identity is stale")
        if impact.market_snapshot_id != snapshot.snapshot_id:
            raise ValueError("selected expression market snapshot is stale")
        if impact.market_state_publication_id != decision.market_state_publication_id:
            raise ValueError("selected expression market publication is stale")
        if impact.cutoff != decision.cutoff:
            raise ValueError("selected expression portfolio impact cutoff is stale")
        if impact.availability != "available" or impact.blockers:
            raise ValueError("selected expression portfolio impact is blocked")

    @staticmethod
    def _validate_persisted_context(
        connection: Any,
        row: Any,
        decision: TickerDecision,
        kind: ExpressionKind,
        trade_plan_id: str,
    ) -> dict[str, Any]:
        if not row["opportunity_episode"] or str(row["opportunity_episode_id"] or "") != decision.opportunity_episode_id:
            raise ValueError("ticker opportunity episode context is missing in PostgreSQL")
        if row["opportunity_cutoff"] is None or _utc(row["opportunity_cutoff"]) != decision.cutoff:
            raise ValueError("ticker opportunity cutoff is stale in PostgreSQL")
        persisted_episode = OpportunityEpisode.model_validate(row["opportunity_episode"])
        if persisted_episode.model_dump(mode="json") != decision.opportunity_episode.model_dump(mode="json"):
            raise ValueError("ticker opportunity episode is stale in PostgreSQL")
        if str(row["market_state_publication_id"] or "") != decision.market_state_publication_id:
            raise ValueError("ticker market publication is stale in PostgreSQL")
        if not row["market_state_snapshot"] or not row["portfolio_impacts"] or not row["risk_policy_snapshot"]:
            raise ValueError("ticker market and portfolio context is missing in PostgreSQL")
        persisted_snapshot = MarketStateSnapshot.model_validate(row["market_state_snapshot"])
        if (
            persisted_snapshot.model_dump(mode="json")
            != decision.market_state_snapshot.model_dump(mode="json")
        ):
            raise ValueError("ticker market snapshot is stale in PostgreSQL")
        persisted_impacts = dict(row["portfolio_impacts"] or {})
        persisted_impact = persisted_impacts.get(kind.value) or persisted_impacts.get(str(kind))
        if persisted_impact is None:
            raise ValueError("ticker portfolio impact is missing in PostgreSQL")
        if PortfolioImpact.model_validate(persisted_impact).model_dump(mode="json") != decision.portfolio_impacts[kind].model_dump(mode="json"):
            raise ValueError("ticker portfolio impact is stale in PostgreSQL")
        persisted_policy = RiskPolicySnapshot.model_validate(row["risk_policy_snapshot"])
        if (
            persisted_policy.model_dump(mode="json")
            != decision.risk_policy_snapshot.model_dump(mode="json")
        ):
            raise ValueError("ticker risk policy snapshot is stale in PostgreSQL")
        publication = connection.execute(
            """
            SELECT publication.id::text, publication.scope, publication.status,
                   publication.published_at, run.input_cutoff
            FROM app.publication publication
            JOIN analysis.run run ON run.id = publication.analysis_run_id
            WHERE publication.id = %s::uuid
            """,
            [decision.market_state_publication_id],
        ).fetchone()
        if (
            publication is None
            or publication["scope"] != "market"
            or publication["status"] not in {"published", "superseded"}
            or publication["published_at"] is None
            or _utc(publication["published_at"]) > decision.cutoff
        ):
            raise ValueError("ticker market publication is unavailable in PostgreSQL")
        if publication["input_cutoff"] is None or _utc(publication["input_cutoff"]) > decision.cutoff:
            raise ValueError("ticker market publication is newer than the decision cutoff")
        authority_rows = connection.execute(
            """
            SELECT item.model_name, item.publication_id::text AS publication_id,
                   payload.payload, publication.published_at, run.input_cutoff
            FROM app.current_publication_item item
            JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
            JOIN app.publication publication ON publication.id = item.publication_id
            JOIN analysis.run run ON run.id = publication.analysis_run_id
            WHERE item.scope = 'ticker-opportunity-ranking'
              AND item.model_name = ANY(%s)
              AND publication.status = 'published'
              AND payload.payload->>'ticker' = %s
              AND payload.payload->>'decision_revision' = %s
              AND payload.payload->>'opportunity_episode_id' = %s
            ORDER BY item.model_name, publication.published_at DESC, item.rank
            """,
            [["opportunity_rank", "trade_plan", "alpha_signal"], decision.ticker,
             decision.decision_revision, decision.opportunity_episode_id],
        ).fetchall()
        grouped: dict[str, list[Any]] = {}
        for item in authority_rows:
            grouped.setdefault(str(item["model_name"]), []).append(item)
        if len(grouped.get("opportunity_rank", [])) != 1:
            raise ValueError("ticker opportunity rank is missing or duplicated in current PostgreSQL publication")
        if len(grouped.get("trade_plan", [])) != 1:
            raise ValueError("ticker trade plan is missing or duplicated in current PostgreSQL publication")
        rank = grouped["opportunity_rank"][0]
        plan_row = grouped["trade_plan"][0]
        rank_payload = dict(rank["payload"] or {})
        plan_payload = dict(plan_row["payload"] or {})
        if rank["published_at"] is None or _utc(rank["published_at"]) > decision.cutoff:
            raise ValueError("ticker opportunity rank is newer than the decision cutoff")
        if rank["input_cutoff"] is None or _utc(rank["input_cutoff"]) > decision.cutoff:
            raise ValueError("ticker opportunity rank input cutoff is stale in PostgreSQL")
        if plan_row["published_at"] is None or _utc(plan_row["published_at"]) > decision.cutoff:
            raise ValueError("ticker trade plan is newer than the decision cutoff")
        if plan_row["input_cutoff"] is None or _utc(plan_row["input_cutoff"]) > decision.cutoff:
            raise ValueError("ticker trade plan input cutoff is stale in PostgreSQL")
        if str(rank["publication_id"]) != str(plan_row["publication_id"]):
            raise ValueError("ticker opportunity rank and trade plan publications differ")
        plan_payload["publication_id"] = str(plan_row["publication_id"])
        persisted_plan = TradePlan.model_validate(plan_payload)
        if persisted_plan.trade_plan_id != trade_plan_id:
            raise ValueError("ticker trade plan is stale in PostgreSQL")
        if persisted_plan.publication_id != str(plan_row["publication_id"]):
            raise ValueError("ticker trade plan publication identity is stale")
        if persisted_plan.ticker != decision.ticker or persisted_plan.opportunity_episode_id != decision.opportunity_episode_id:
            raise ValueError("ticker trade plan identity is stale in PostgreSQL")
        if persisted_plan.decision_revision != decision.decision_revision or persisted_plan.policy_version != decision.policy_version:
            raise ValueError("ticker trade plan revision or policy is stale in PostgreSQL")
        if persisted_plan.cutoff != decision.cutoff:
            raise ValueError("ticker trade plan cutoff is stale in PostgreSQL")
        if persisted_plan.selected_expression_kind is not kind:
            raise ValueError("ticker trade plan expression kind is stale in PostgreSQL")
        if persisted_plan.selected_expression.model_dump(mode="json") != decision.selected_expression.model_dump(mode="json"):
            raise ValueError("ticker trade plan expression is stale in PostgreSQL")
        if persisted_plan.market_snapshot_id != decision.market_state_snapshot.snapshot_id:
            raise ValueError("ticker trade plan market snapshot is stale in PostgreSQL")
        if persisted_plan.market_state_publication_id != decision.market_state_publication_id:
            raise ValueError("ticker trade plan market publication is stale in PostgreSQL")
        if persisted_plan.portfolio_impact_id != decision.portfolio_impacts[kind].impact_id:
            raise ValueError("ticker trade plan portfolio impact is stale in PostgreSQL")
        if persisted_plan.input_lineage != tuple(decision.input_lineage):
            raise ValueError("ticker trade plan lineage is stale in PostgreSQL")
        if (
            persisted_plan.rank_id != str(rank_payload.get("rank_id") or "")
            or persisted_plan.selected_expression_kind.value != str(rank_payload.get("selected_expression_kind") or "")
            or persisted_plan.selected_expression_identity != str(rank_payload.get("selected_expression_identity") or "")
            or persisted_plan.alpha_signal_id != str(rank_payload.get("alpha_signal_id") or "")
            or persisted_plan.portfolio_impact_id != str(rank_payload.get("portfolio_impact_id") or "")
            or persisted_plan.market_snapshot_id != str(rank_payload.get("market_snapshot_id") or "")
            or persisted_plan.market_state_publication_id != str(rank_payload.get("market_state_publication_id") or "")
        ):
            raise ValueError("ticker trade plan is not bound to the current opportunity rank")
        signal_matches = [
            item for item in grouped.get("alpha_signal", [])
            if str((item["payload"] or {}).get("signal_id") or "") == str(persisted_plan.alpha_signal_id or "")
        ]
        if persisted_plan.alpha_signal_id is None or len(signal_matches) != 1:
            raise ValueError("ticker trade plan alpha signal is missing or stale in PostgreSQL")
        selected = decision.selected_expression
        selected_identity = trade_expression_identity(selected) if selected is not None else ""
        rank_utility = rank_payload.get("trade_utility")
        try:
            rank_utility_value = float(rank_utility)
            rank_value = int(rank_payload.get("trade_rank"))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("ticker opportunity rank is unavailable")
        if (
            rank_payload.get("trade_rank_unavailable_reason")
            or rank_value <= 0
            or not isfinite(rank_utility_value)
            or rank_utility_value <= 0
            or not bool(rank_payload.get("evaluated_universe_complete"))
        ):
            raise ValueError(
                "ticker opportunity rank is unavailable: "
                + str(rank_payload.get("trade_rank_unavailable_reason") or "positive_current_rank_required")
            )
        exact_pairs = {
            "selected_expression_identity": selected_identity,
            "selected_expression_kind": selected.kind.value if selected is not None else "",
            "policy_version": decision.policy_version,
            "market_snapshot_id": decision.market_state_snapshot.snapshot_id if decision.market_state_snapshot else "",
            "market_state_publication_id": decision.market_state_publication_id,
            "portfolio_impact_id": decision.portfolio_impacts[selected.kind].impact_id if selected is not None else "",
        }
        for field, expected in exact_pairs.items():
            if str(rank_payload.get(field) or "") != str(expected or ""):
                raise ValueError("ticker opportunity rank is stale in PostgreSQL")
        if str(rank_payload.get("risk_policy_version") or "") != decision.policy_version:
            raise ValueError("ticker opportunity rank policy is stale in PostgreSQL")
        return {
            "publication_id": str(rank["publication_id"]),
            "payload": rank_payload,
            "plan_publication_id": str(plan_row["publication_id"]),
            "plan_payload": plan_payload,
        }

    def process(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Manage existing ticker paper orders without touching a broker."""

        reference = _utc(now)
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT id::text
                FROM app.paper_order
                WHERE lane = 'ticker'
                  AND status NOT IN ('exited', 'invalidated', 'unfilled', 'rejected', 'unmeasurable')
                ORDER BY created_at, id
                LIMIT %s
                """,
                [max(1, min(int(limit), 500))],
            ).fetchall()
        managed = [
            result
            for row in rows
            if (result := self._manage_one(str(row["id"]), reference)) is not None
        ]
        return {
            "status": "ok",
            "paper_only": True,
            "live_brokerage_submission": False,
            "managed": managed,
            "count": len(managed),
        }

    manage_orders = process

    def _manage_one(self, paper_order_id: str, now: datetime) -> dict[str, Any] | None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            order = connection.execute(
                """
                SELECT paper.id::text, paper.instrument_id, paper.status, paper.side,
                       paper.quantity, paper.limit_price, paper.actual_fill_price,
                       paper.filled_at, paper.submitted_at, paper.filled_quantity,
                       paper.exited_quantity, paper.fees, paper.expires_at,
                       paper.expression_kind, paper.structure, paper.policy_result, paper.thesis_snapshot,
                       instrument.symbol
                FROM app.paper_order paper
                JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                WHERE paper.id = %s::uuid AND paper.lane = 'ticker'
                FOR UPDATE OF paper
                """,
                [paper_order_id],
            ).fetchone()
            if order is None:
                return None
            item = dict(order)
            status = str(item.get("status") or "")
            if status in TERMINAL_STATUSES:
                return None
            quantity = _quantity(item.get("quantity"))
            filled = _quantity(item.get("filled_quantity"))
            exited = _quantity(item.get("exited_quantity"))
            remaining_entry = max(0.0, quantity - filled)
            if remaining_entry > 0:
                return self._manage_entry(connection, item, now, remaining_entry)
            return self._manage_open(connection, item, now, filled, exited)

    def _manage_entry(
        self,
        connection: Any,
        order: dict[str, Any],
        now: datetime,
        remaining: float,
    ) -> dict[str, Any]:
        expires_at = _timestamp(order.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            filled = _quantity(order.get("filled_quantity"))
            if filled <= 0:
                connection.execute(
                    """
                    UPDATE app.paper_order
                    SET status = 'unfilled', unfilled_reason = %s, updated_at = %s
                    WHERE id = %s::uuid
                    """,
                    ["entry_limit_expired", now, order["id"]],
                )
                return {"paper_order_id": str(order["id"]), "status": "unfilled", "reason": "entry_limit_expired"}
            connection.execute(
                "UPDATE app.paper_order SET status = 'entered', unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                ["entry_limit_expired_after_partial_fill", now, order["id"]],
            )
            return {
                "paper_order_id": str(order["id"]),
                "status": "entered",
                "reason": "entry_limit_expired_after_partial_fill",
                "filled_quantity": filled,
            }

        expression_kind = str(order.get("expression_kind") or "").upper()
        if expression_kind in {kind.value for kind in OPTION_EXPRESSIONS}:
            return self._manage_option_entry(connection, order, now, remaining)
        if expression_kind != ExpressionKind.STOCK.value:
            connection.execute(
                """
                UPDATE app.paper_order
                SET submitted_at = coalesce(submitted_at, %s),
                    unfilled_reason = %s, updated_at = %s
                WHERE id = %s::uuid
                """,
                [now, "executable_option_quote_required", now, order["id"]],
            )
            return {
                "paper_order_id": str(order["id"]),
                "status": "submitted",
                "reason": "executable_option_quote_required",
            }

        quote = connection.execute(
            """
            SELECT price, observed_at, available_at
            FROM raw.confirmed_quote
            WHERE instrument_id = %s
              AND observed_at <= %s
              AND available_at <= %s
            ORDER BY observed_at DESC, available_at DESC
            LIMIT 1
            """,
            [order["instrument_id"], now, now],
        ).fetchone()
        if quote is None or _number(quote["price"]) is None:
            connection.execute(
                """
                UPDATE app.paper_order
                SET submitted_at = coalesce(submitted_at, %s),
                    unfilled_reason = %s, updated_at = %s
                WHERE id = %s::uuid
                """,
                [now, "fresh_confirmed_quote_required", now, order["id"]],
            )
            return {
                "paper_order_id": str(order["id"]),
                "status": "submitted",
                "reason": "fresh_confirmed_quote_required",
            }

        market_price = float(quote["price"])
        limit_price = _number(order.get("limit_price"))
        side = str(order.get("side") or "buy").lower()
        if limit_price is None or not _limit_reached(side, market_price, limit_price):
            connection.execute(
                """
                UPDATE app.paper_order
                SET submitted_at = coalesce(submitted_at, %s),
                    unfilled_reason = %s, updated_at = %s
                WHERE id = %s::uuid
                """,
                [now, "limit_not_reached", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "submitted", "reason": "limit_not_reached"}

        policy = dict(order.get("policy_result") or {})
        available = _number(policy.get("available_quantity"))
        fill_quantity = min(remaining, max(0.0, available if available is not None else remaining))
        if fill_quantity <= 0:
            return {"paper_order_id": str(order["id"]), "status": "submitted", "reason": "displayed_size_unavailable"}
        prior_filled = _quantity(order.get("filled_quantity"))
        new_filled = prior_filled + fill_quantity
        complete = new_filled >= _quantity(order.get("quantity"))
        fees = fill_quantity * max(0.0, _number(policy.get("fee_per_unit")) or 0.0)
        slippage = abs(market_price - (limit_price or market_price))
        new_status = "entered" if complete else "open"
        policy["entry_fill_count"] = int(_number(policy.get("entry_fill_count")) or 0) + 1
        connection.execute(
            """
            UPDATE app.paper_order
            SET status = %s, actual_fill_price = coalesce(actual_fill_price, %s),
                filled_at = coalesce(filled_at, %s), submitted_at = coalesce(submitted_at, %s),
                filled_quantity = %s, fees = coalesce(fees, 0) + %s,
                entry_slippage = %s, unfilled_reason = CASE WHEN %s THEN NULL ELSE %s END,
                policy_result = %s, updated_at = %s
            WHERE id = %s::uuid
            """,
            [
                new_status, market_price, now, now, new_filled, fees, slippage,
                complete, "partial_fill", Jsonb(policy), now, order["id"],
            ],
        )
        from investment_panel.database.portfolio import PortfolioLoopRepository

        PortfolioLoopRepository(self.runtime).record_existing_paper_order_fill(
            connection, paper_order_id=str(order["id"]), observed_at=now,
            status="entered" if complete else "open",
        )
        return {
            "paper_order_id": str(order["id"]),
            "status": "filled" if complete else "partial",
            "event_status": "entered" if complete else None,
            "filled_quantity": new_filled,
            "fill_price": market_price,
            "fees": fees,
            "slippage": slippage,
        }

    def _manage_option_entry(
        self,
        connection: Any,
        order: dict[str, Any],
        now: datetime,
        remaining: float,
    ) -> dict[str, Any]:
        legs = self._stored_option_legs(connection, order["id"])
        quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now) if legs else []
        if not quoted:
            connection.execute(
                "UPDATE app.paper_order SET submitted_at = coalesce(submitted_at, %s), unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [now, "fresh_executable_option_quote_required", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "submitted", "reason": "fresh_executable_option_quote_required"}
        structure = str(order.get("structure") or "")
        credit = is_credit_structure(structure)
        market_price = package_price(quoted, phase="entry")
        limit_price = _number(order.get("limit_price"))
        if market_price is None or limit_price is None or (market_price < limit_price if credit else market_price > limit_price):
            connection.execute(
                "UPDATE app.paper_order SET submitted_at = coalesce(submitted_at, %s), unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [now, "limit_not_reached", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "submitted", "reason": "limit_not_reached"}
        fill_quantity = min(remaining, _option_available_quantity(quoted, remaining, phase="entry"))
        if fill_quantity <= 0:
            return {"paper_order_id": str(order["id"]), "status": "submitted", "reason": "displayed_option_size_unavailable"}
        prior_filled = _quantity(order.get("filled_quantity"))
        new_filled = prior_filled + fill_quantity
        complete = new_filled >= _quantity(order.get("quantity"))
        fees = FEE_PER_CONTRACT_LEG * len(quoted) * fill_quantity
        midpoint = _option_midpoint(quoted)
        slippage = abs(market_price - midpoint) if midpoint is not None else None
        policy = dict(order.get("policy_result") or {})
        policy["entry_fill_count"] = int(_number(policy.get("entry_fill_count")) or 0) + 1
        connection.execute(
            """
            UPDATE app.paper_order
            SET status = %s, actual_fill_price = coalesce(actual_fill_price, %s),
                filled_at = coalesce(filled_at, %s), submitted_at = coalesce(submitted_at, %s),
                filled_quantity = %s, fees = coalesce(fees, 0) + %s,
                entry_slippage = %s, unfilled_reason = CASE WHEN %s THEN NULL ELSE %s END,
                policy_result = %s, updated_at = %s
            WHERE id = %s::uuid
            """,
            ["entered" if complete else "open", market_price, now, now, new_filled, fees, slippage,
             complete, "partial_fill", Jsonb(policy), now, order["id"]],
        )
        return {
            "paper_order_id": str(order["id"]),
            "status": "filled" if complete else "partial",
            "event_status": "entered" if complete else None,
            "filled_quantity": new_filled,
            "fill_price": market_price,
            "fees": fees,
            "slippage": slippage,
        }

    def _manage_open(
        self,
        connection: Any,
        order: dict[str, Any],
        now: datetime,
        filled: float,
        exited: float,
    ) -> dict[str, Any]:
        remaining = max(0.0, filled - exited)
        if remaining <= 0:
            return {"paper_order_id": str(order["id"]), "status": "closed", "reason": "no_remaining_quantity"}
        if str(order.get("expression_kind") or "").upper() in {kind.value for kind in OPTION_EXPRESSIONS}:
            return self._manage_option_open(connection, order, now, remaining)
        if str(order.get("side") or "buy").lower() != "buy":
            return self._close_at_market(connection, order, now, remaining, "exit_order_filled")
        quote = connection.execute(
            """
            SELECT price, observed_at, available_at
            FROM raw.confirmed_quote
            WHERE instrument_id = %s
              AND observed_at <= %s
              AND available_at <= %s
            ORDER BY observed_at DESC, available_at DESC
            LIMIT 1
            """,
            [order["instrument_id"], now, now],
        ).fetchone()
        if quote is None or _number(quote["price"]) is None:
            return {"paper_order_id": str(order["id"]), "status": "entered", "reason": "fresh_confirmed_quote_required_for_exit"}
        price = float(quote["price"])
        snapshot = dict(order.get("thesis_snapshot") or {})
        selected = dict(snapshot.get("selected_expression") or {})
        invalidation = dict(selected.get("invalidation") or {})
        target = dict(selected.get("target_range") or {})
        invalidation_price = _number(invalidation.get("value")) if invalidation.get("kind") == "price" else None
        target_price = _number(target.get("high"))
        expires_at = _timestamp(order.get("expires_at"))
        reason = None
        if invalidation_price is not None and price <= invalidation_price:
            reason = "invalidation"
        elif target_price is not None and price >= target_price:
            reason = "target_reached"
        elif expires_at is not None and expires_at <= now:
            reason = "decision_expired"
        if reason is None:
            return {"paper_order_id": str(order["id"]), "status": "entered", "reason": "exit_not_triggered"}
        return self._close_at_market(connection, order, now, remaining, reason, price=price)

    def _manage_option_open(
        self,
        connection: Any,
        order: dict[str, Any],
        now: datetime,
        remaining: float,
    ) -> dict[str, Any]:
        snapshot = dict(order.get("thesis_snapshot") or {})
        selected = dict(snapshot.get("selected_expression") or {})
        stance = str(selected.get("stance") or "BULLISH").upper()
        invalidation = dict(selected.get("invalidation") or {})
        target = dict(selected.get("target_range") or {})
        quote = connection.execute(
            """
            SELECT price FROM raw.confirmed_quote
            WHERE instrument_id = %s AND observed_at <= %s AND available_at <= %s
            ORDER BY observed_at DESC, available_at DESC LIMIT 1
            """,
            [order["instrument_id"], now, now],
        ).fetchone()
        underlying_price = _number(quote["price"]) if quote is not None else None
        invalidation_price = _number(invalidation.get("value")) if invalidation.get("kind") == "price" else None
        target_low = _number(target.get("low"))
        target_high = _number(target.get("high"))
        reason = None
        if _timestamp(order.get("expires_at")) is not None and _timestamp(order.get("expires_at")) <= now:
            reason = "decision_expired"
        elif stance == "BEARISH" and invalidation_price is not None and underlying_price is not None and underlying_price >= invalidation_price:
            reason = "invalidation"
        elif stance != "BEARISH" and invalidation_price is not None and underlying_price is not None and underlying_price <= invalidation_price:
            reason = "invalidation"
        elif stance == "BEARISH" and target_low is not None and underlying_price is not None and underlying_price <= target_low:
            reason = "target_reached"
        elif stance != "BEARISH" and target_high is not None and underlying_price is not None and underlying_price >= target_high:
            reason = "target_reached"

        legs = self._stored_option_legs(connection, order["id"])
        expiration = _option_expiration(selected, legs)
        structure = str(order.get("structure") or _option_structure(ExpressionKind(str(order.get("expression_kind").upper()))))
        if expiration is not None and expiration <= now.date():
            if structure == "cash_secured_put" and underlying_price is not None and legs:
                strike = _number(legs[0].get("strike"))
                if strike is not None and underlying_price <= strike:
                    policy = dict(order.get("policy_result") or {})
                    multiplier = int(legs[0].get("multiplier") or 0)
                    if multiplier <= 0:
                        return {"paper_order_id": str(order["id"]), "status": "entered", "reason": "assignment_multiplier_missing"}
                    contract_count = _quantity(order.get("quantity"))
                    assignment_fee = FEE_PER_CONTRACT_LEG * len(legs) * contract_count
                    settlement_value = (strike - underlying_price) * multiplier * contract_count
                    policy["assignment"] = {
                        "status": "assigned", "strike": strike, "underlying_price": underlying_price,
                        "multiplier": multiplier, "contract_count": contract_count, "settlement_value": settlement_value,
                        "settled_at": now, "assignment_fee": assignment_fee,
                    }
                    policy["exit_fill_count"] = int(_number(policy.get("exit_fill_count")) or 0) + 1
                    connection.execute(
                        """
                        UPDATE app.paper_order
                        SET status = 'exited', exited_quantity = %s, exit_price = %s, exit_at = %s,
                            fees = coalesce(fees, 0) + %s, policy_result = %s,
                            unfilled_reason = %s, updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        [order["quantity"], max(strike - underlying_price, 0.0), now,
                         assignment_fee, Jsonb(policy), "assigned_at_expiration", now, order["id"]],
                    )
                    from investment_panel.database.portfolio import PortfolioLoopRepository

                    PortfolioLoopRepository(self.runtime).record_existing_paper_order_fill(
                        connection, paper_order_id=str(order["id"]), observed_at=now, status="exited",
                    )
                    return {"paper_order_id": str(order["id"]), "status": "closed", "event_status": "exited", "reason": "assignment", "assigned_strike": strike}
            reason = reason or "expiration"
        if reason is None:
            return {"paper_order_id": str(order["id"]), "status": "entered", "reason": "exit_not_triggered"}
        quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now) if legs else []
        exit_price = package_price(quoted, phase="exit") if quoted else None
        if exit_price is None:
            connection.execute(
                "UPDATE app.paper_order SET unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [f"{reason}: fresh_executable_exit_quote_required", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "entered", "reason": f"{reason}_pending_executable_quote"}
        exit_quantity = min(remaining, _option_available_quantity(quoted, remaining, phase="exit"))
        if exit_quantity <= 0:
            connection.execute(
                "UPDATE app.paper_order SET unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [f"{reason}: displayed_size_unavailable", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "entered", "reason": f"{reason}_pending_size"}
        new_exited = _quantity(order.get("exited_quantity")) + exit_quantity
        terminal = new_exited >= _quantity(order.get("filled_quantity") or order.get("quantity"))
        fees = FEE_PER_CONTRACT_LEG * len(quoted) * exit_quantity
        midpoint = _option_midpoint(quoted)
        slippage = abs(exit_price - midpoint) if midpoint is not None else None
        policy = dict(order.get("policy_result") or {})
        policy["exit_fill_count"] = int(_number(policy.get("exit_fill_count")) or 0) + 1
        connection.execute(
            """
            UPDATE app.paper_order
            SET status = %s, exited_quantity = %s, exit_price = %s, exit_at = %s,
                fees = coalesce(fees, 0) + %s, exit_slippage = %s,
                unfilled_reason = NULL, policy_result = %s, updated_at = %s
            WHERE id = %s::uuid
            """,
            ["exited" if terminal else "partial_exited", new_exited, exit_price, now, fees, slippage, Jsonb(policy), now, order["id"]],
        )
        from investment_panel.database.portfolio import PortfolioLoopRepository

        PortfolioLoopRepository(self.runtime).record_existing_paper_order_fill(
            connection, paper_order_id=str(order["id"]), observed_at=now,
            status="exited" if terminal else "partial_exited",
        )
        return {
            "paper_order_id": str(order["id"]),
            "status": "closed" if terminal else "partial",
            "event_status": "exited" if terminal else None,
            "reason": reason,
            "exit_quantity": exit_quantity,
            "exit_price": exit_price,
            "fees": fees,
            "slippage": slippage,
        }

    def _stored_option_legs(self, connection: Any, paper_order_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT leg.contract_id, leg.option_type, leg.side, leg.strike, leg.bid, leg.ask,
                   leg.bid_size, leg.ask_size, leg.quote_time, leg.open_interest, leg.volume,
                   contract.expiration, contract.multiplier
            FROM app.paper_order_leg leg
            JOIN catalog.option_contract contract ON contract.id = leg.contract_id
            WHERE leg.paper_order_id = %s::uuid
            ORDER BY leg.leg_index
            """,
            [paper_order_id],
        ).fetchall()
        return [dict(row) for row in rows]

    def _close_at_market(
        self,
        connection: Any,
        order: dict[str, Any],
        now: datetime,
        quantity: float,
        reason: str,
        *,
        price: float | None = None,
    ) -> dict[str, Any]:
        policy = dict(order.get("policy_result") or {})
        fee_per_unit = max(0.0, _number(policy.get("fee_per_unit")) or 0.0)
        fees = quantity * fee_per_unit
        new_exited = _quantity(order.get("exited_quantity")) + quantity
        policy["exit_fill_count"] = int(_number(policy.get("exit_fill_count")) or 0) + 1
        connection.execute(
            """
            UPDATE app.paper_order
            SET status = 'exited', exited_quantity = %s, exit_price = %s,
                exit_at = %s, fees = coalesce(fees, 0) + %s,
                updated_at = %s, unfilled_reason = NULL, policy_result = %s
            WHERE id = %s::uuid
            """,
            [new_exited, price, now, fees, now, Jsonb(policy), order["id"]],
        )
        from investment_panel.database.portfolio import PortfolioLoopRepository

        PortfolioLoopRepository(self.runtime).record_existing_paper_order_fill(
            connection, paper_order_id=str(order["id"]), observed_at=now, status="exited",
        )
        return {
            "paper_order_id": str(order["id"]),
            "status": "closed",
            "event_status": "exited",
            "reason": reason,
            "exit_quantity": quantity,
            "exit_price": price,
            "fees": fees,
        }

    def _check_switches(self, kind: ExpressionKind) -> None:
        settings = self.config.analysis.options_decision_system
        if settings.mode != "paper":
            raise ValueError("ticker paper execution requires analysis mode=paper")
        if not settings.ticker_paper_actions_enabled:
            raise ValueError("ticker paper actions kill switch is disabled")
        if kind is ExpressionKind.STOCK and not settings.stock_paper_actions_enabled:
            raise ValueError("stock paper actions kill switch is disabled")
        if kind in OPTION_EXPRESSIONS and not settings.options_paper_actions_enabled:
            raise ValueError("options paper actions kill switch is disabled")


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and isfinite(result) else None


def _quantity(value: Any) -> float:
    return max(0.0, _number(value) or 0.0)


def _limit_reached(side: str, market_price: float, limit_price: float) -> bool:
    return market_price <= limit_price if side == "buy" else market_price >= limit_price


def _complete_option_legs(legs: list[dict[str, Any]]) -> bool:
    return bool(legs) and all(
        leg.get("contract_id")
        and leg.get("option_type")
        and leg.get("side")
        and leg.get("strike") is not None
        and _number(leg.get("bid")) is not None
        and _number(leg.get("ask")) is not None
        and _number(leg.get("bid_size")) is not None
        and _number(leg.get("ask_size")) is not None
        and _timestamp(leg.get("quote_time") or leg.get("observed_at")) is not None
        for leg in legs
    )


def _option_structure(kind: ExpressionKind) -> str:
    return {
        ExpressionKind.CALL: "long_call",
        ExpressionKind.PUT: "long_put",
        ExpressionKind.DEBIT_SPREAD: "debit_spread",
        ExpressionKind.CASH_SECURED_PUT: "cash_secured_put",
    }[kind]


def _option_available_quantity(legs: list[dict[str, Any]], requested: float, *, phase: str) -> float:
    sizes: list[int] = []
    for leg in legs:
        short = str(leg.get("side") or "").lower() in {"short", "sell"}
        key = "bid_size" if (phase == "entry" and short) or (phase == "exit" and not short) else "ask_size"
        size = _number(leg.get(key))
        if size is None or size <= 0:
            return 0.0
        sizes.append(floor(size))
    return float(min(floor(requested), min(sizes))) if sizes else 0.0


def _option_midpoint(legs: list[dict[str, Any]]) -> float | None:
    signed = 0.0
    for leg in legs:
        bid, ask = _number(leg.get("bid")), _number(leg.get("ask"))
        if bid is None or ask is None or bid < 0 or ask < bid:
            return None
        midpoint = (bid + ask) / 2
        signed += -midpoint if str(leg.get("side") or "").lower() in {"short", "sell"} else midpoint
    return abs(signed) if legs else None


def _option_expiration(selected: dict[str, Any], legs: list[dict[str, Any]]) -> date | None:
    value = selected.get("expiration")
    if value is None:
        selected_legs = selected.get("legs")
        if isinstance(selected_legs, list) and selected_legs:
            value = selected_legs[0].get("expiration")
    if value is None and legs:
        value = legs[0].get("expiration")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except (TypeError, ValueError):
        return None


__all__ = ["TickerPaperExecutionRepository"]
