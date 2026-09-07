"""Prospective observations for a core candidate without active trade authority."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.decision import is_market_open
from investment_panel.core.option_trade_ticket import execution_policy, exit_reason
from investment_panel.core.options_recovery import FEE_PER_CONTRACT_LEG
from investment_panel.database.options_paper_quotes import latest_option_legs, package_price
from investment_panel.database.runtime import JOB_PROFILE
from investment_panel.database.strategy_parameters import merge_strategy_parameters, mutation_capability


EXPERIMENT_VERSION = "option-paper-experiment-v1"
EXPERIMENT_KIND = "options-paper-experiment"
SHADOW_SOURCE = "options_paper_experiment"


def experiment_candidate(connection: Any, candidate_id: int, *, as_of: datetime, require_shadow: bool = False, for_entry: bool = True) -> dict[str, Any]:
    row = connection.execute(
        """SELECT candidate.id, candidate.parameters, candidate.created_at, candidate.supersedes_id,
                  candidate.status, candidate.authority_group, parent.parameters AS parent_parameters,
                  parent.status AS parent_status, parent.authority_group AS parent_authority_group,
                  proposal.id AS proposal_id, proposal.created_at AS proposal_created_at, proposal.result,
                  (SELECT count(*) FROM analysis.strategy_revision
                   WHERE authority_group = 'options-radar-core' AND status = 'active') AS active_count
           FROM analysis.strategy_revision candidate
           JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id
           JOIN LATERAL (
               SELECT id, result, created_at FROM analysis.agent_task
               WHERE task_kind = 'strategy_mutation_proposal' AND status = 'completed'
                 AND result->>'candidate_revision_id' = candidate.id::text AND created_at <= %s
               ORDER BY created_at DESC, id DESC LIMIT 1
           ) proposal ON true
           WHERE candidate.id = %s AND candidate.created_at <= %s""",
        [as_of, candidate_id, as_of],
    ).fetchone()
    if row is None or row["authority_group"] != "options-radar-core" or row["status"] not in {"candidate", "testing", "approved"}:
        raise ValueError("core candidate proposal required")
    if row["parent_status"] != "active" or row["parent_authority_group"] != "options-radar-core" or row["active_count"] != 1:
        raise ValueError("candidate parent is no longer the unique active strategy")
    base = dict(row["parent_parameters"] or {})
    changes = dict((row["result"] or {}).get("proposed_parameter_changes") or {})
    expected = merge_strategy_parameters(base, changes)
    capability = mutation_capability(base, changes)
    if capability["blocking_verdict"]:
        raise ValueError(str(capability["blocking_verdict"]))
    if expected != dict(row["parameters"] or {}) or int(base.get("contract_version") or 0) < 3:
        raise ValueError("candidate parameter or feature lineage is inconsistent")
    required = {"walk_forward", "shadow"} if require_shadow else {"walk_forward"}
    entry_evaluations = {"shadow", "execution_grade_paper"}
    verdicts = connection.execute(
        """SELECT DISTINCT ON (evaluation_type) evaluation_type, verdict
           FROM analysis.strategy_evaluation
           WHERE strategy_revision_id = %s AND evaluation_type = ANY(%s)
             AND evaluated_at <= %s AND available_at <= %s
             AND (%s OR verdict <> 'blocked_terminal_evidence')
           ORDER BY evaluation_type, evaluated_at DESC, id DESC""",
        [candidate_id, sorted(required | entry_evaluations), as_of, as_of, for_entry],
    ).fetchall()
    if for_entry and any(item["evaluation_type"] in entry_evaluations and item["verdict"] == "blocked_terminal_evidence" for item in verdicts):
        raise ValueError("candidate entry blocked_terminal_evidence")
    passed = {item["evaluation_type"] for item in verdicts if item["verdict"] == "pass"}
    if required - passed:
        raise ValueError("candidate requires passing " + ", ".join(sorted(required - passed)))
    return dict(row)


def experiment_publication_row(
    connection: Any, publication_id: str, decision_id: str, *, as_of: datetime,
    allow_superseded: bool = False, require_shadow: bool = True, for_entry: bool = True,
) -> dict[str, Any]:
    row = connection.execute(
        """SELECT publication.id::text AS publication_id, publication.scope, publication.published_at,
                  publication.status, item.payload, item.rank, run.id::text AS run_id, run.run_type AS kind,
                  run.inputs, run.input_cutoff, run.strategy_revision_id
           FROM app.publication publication
           JOIN analysis.run run ON run.id = publication.analysis_run_id AND run.status = 'succeeded'
           JOIN app.publication_content_item item ON item.publication_id = publication.id
           JOIN analysis.decision decision ON decision.run_id = run.id
                AND decision.id::text = item.payload->>'decision_id'
                AND decision.strategy_revision_id = run.strategy_revision_id
           WHERE publication.id = %s::uuid AND item.model_name = 'option_paper_experiment'
             AND decision.id = %s::uuid AND publication.published_at <= %s""",
        [publication_id, decision_id, as_of],
    ).fetchone()
    if row is None or row["status"] not in ({"published", "superseded"} if allow_superseded else {"published"}):
        raise ValueError("experiment publication authority is missing")
    observed = dict((row["payload"] or {}).get("experiment") or {})
    if observed.get("incumbent_revision_id"):
        if require_shadow:
            raise ValueError("incumbent shadow observations are not candidate paper authority")
        expected = incumbent_identity(int(row["strategy_revision_id"]), row["run_id"])
        if (
            row["kind"] != "options-radar" or row["scope"] != f"options-paper-incumbent:{row['strategy_revision_id']}"
            or (row["inputs"] or {}).get("observation") != incumbent_identity(int(row["strategy_revision_id"]))
            or observed != expected or ((row["payload"] or {}).get("ticket") or {}).get("experiment") != expected
            or row["input_cutoff"] > as_of
        ):
            raise ValueError("incumbent observation lineage is inconsistent")
        return dict(row)
    candidate = experiment_candidate(connection, int(row["strategy_revision_id"]), as_of=as_of, require_shadow=require_shadow, for_entry=for_entry)
    expected = experiment_identity(candidate, str(row["run_id"]))
    if (
        row["kind"] != EXPERIMENT_KIND
        or row["scope"] != f"{EXPERIMENT_KIND}:{candidate['id']}"
        or (row["inputs"] or {}).get("experiment") != {key: value for key, value in expected.items() if key != "run_id"}
        or (row["payload"] or {}).get("experiment") != expected
        or ((row["payload"] or {}).get("ticket") or {}).get("experiment") != expected
        or not max(candidate["created_at"], candidate["proposal_created_at"]) <= row["input_cutoff"] <= as_of
    ):
        raise ValueError("experiment candidate, run, and publication lineage is inconsistent")
    return dict(row)


def experiment_identity(candidate: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    return {
        "version": EXPERIMENT_VERSION, "paper_only": True,
        "candidate_revision_id": int(candidate["id"]), "parent_revision_id": int(candidate["supersedes_id"]),
        "proposal_id": str(candidate["proposal_id"]),
        **({"run_id": run_id} if run_id else {}),
    }


def incumbent_identity(strategy_id: int, run_id: str | None = None) -> dict[str, Any]:
    return {"version": EXPERIMENT_VERSION, "paper_only": True, "incumbent_revision_id": strategy_id,
            **({"run_id": run_id} if run_id else {})}


def seed_experiment_shadows(runtime: Any, rows: list[dict[str, Any]], *, publication_id: str) -> int:
    count = 0
    with runtime.transaction(JOB_PROFILE) as connection:
        for row in rows:
            if row.get("structure") not in {"long_call", "long_put"}:
                continue
            ticket = dict(row.get("ticket") or {})
            experiment = dict(row.get("experiment") or {})
            revision_id = experiment.get("candidate_revision_id") or experiment["incumbent_revision_id"]
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"experiment:{revision_id}"])
            inserted = connection.execute(
                """INSERT INTO analysis.shadow_trade
                       (decision_id, status, source_kind, pending_entry_reason, structure, metrics)
                   SELECT decision.id, CASE WHEN decision.sample_eligible AND decision.state IN ('WATCH', 'SETUP', 'READY') THEN 'pending' ELSE 'rejected' END,
                          %s, CASE WHEN decision.sample_eligible AND decision.state IN ('WATCH', 'SETUP', 'READY') THEN 'later_quote_required' ELSE 'candidate_gate_rejected' END,
                          %s, %s || jsonb_build_object('entry_deadline', decision.as_of + interval '30 minutes')
                   FROM analysis.decision decision
                   WHERE decision.id = %s::uuid AND decision.strategy_revision_id = %s
                     AND decision.run_id = %s::uuid
                     AND decision.id = (
                         SELECT first.id FROM analysis.decision first
                         WHERE first.run_id = decision.run_id AND first.episode_key = decision.episode_key
                         ORDER BY (first.state = 'REJECTED'), first.score DESC NULLS LAST, first.decision_key
                         LIMIT 1
                     )
                     AND EXISTS (
                         SELECT 1 FROM app.publication publication
                         JOIN app.publication_content_item item ON item.publication_id = publication.id
                         WHERE publication.id = %s::uuid AND publication.analysis_run_id = decision.run_id
                           AND item.model_name = 'option_paper_experiment'
                           AND item.payload->>'decision_id' = decision.id::text
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM analysis.shadow_trade prior
                         JOIN analysis.decision previous ON previous.id = prior.decision_id
                         WHERE prior.source_kind = %s AND previous.strategy_revision_id = decision.strategy_revision_id
                           AND previous.episode_key = decision.episode_key
                     )
                   ON CONFLICT (decision_id) DO NOTHING""",
                [SHADOW_SOURCE, row.get("structure"), _jsonb({
                    "experiment": experiment, "publication_id": publication_id, "ticket": ticket,
                    "source_id": row.get("data_source"), "observation_only": True,
                }), row["decision_id"], revision_id, experiment.get("run_id"), publication_id, SHADOW_SOURCE],
            )
            count += int(inserted.rowcount)
    return count


def advance_experiment_shadows(runtime: Any, *, now: datetime, limit: int = 50) -> dict[str, int]:
    """Observe one unit with later complete quotes, then record after-cost outcomes."""
    counts = {"entered": 0, "closed": 0, "unfilled": 0, "unmeasurable": 0, "rejected": 0}
    with runtime.transaction(JOB_PROFILE) as connection:
        rows = connection.execute(
            """SELECT shadow.*, decision.as_of, decision.strategy_revision_id, decision.episode_key,
                      decision.calibration_cohort, decision.run_id::text AS run_id,
                      decision.state AS decision_state, decision.sample_eligible AS decision_sample_eligible
               FROM analysis.shadow_trade shadow JOIN analysis.decision decision ON decision.id = shadow.decision_id
               WHERE shadow.source_kind = %s AND shadow.status IN ('pending', 'entered')
               ORDER BY shadow.metrics->>'last_checked_at' NULLS FIRST, shadow.created_at, shadow.id
               LIMIT %s FOR UPDATE OF shadow SKIP LOCKED""",
            [SHADOW_SOURCE, max(1, min(limit, 100))],
        ).fetchall()
        for source in rows:
            row, metrics = dict(source), dict(source["metrics"] or {})
            metrics["last_checked_at"] = now.isoformat()
            connection.execute("UPDATE analysis.shadow_trade SET metrics = %s WHERE id = %s", [_jsonb(metrics), row["id"]])
            ticket = dict(metrics.get("ticket") or {})
            try:
                publication = experiment_publication_row(connection, metrics["publication_id"], str(row["decision_id"]), as_of=now, allow_superseded=True, require_shadow=False, for_entry=row["status"] == "pending")
                if metrics.get("experiment") != publication["payload"].get("experiment") or ticket != publication["payload"].get("ticket"):
                    raise ValueError("experiment shadow lineage is inconsistent")
            except ValueError:
                status = "unfilled" if row["status"] == "pending" else "unmeasurable"
                connection.execute("UPDATE analysis.shadow_trade SET status = %s, pending_entry_reason = 'candidate_authority_changed' WHERE id = %s", [status, row["id"]])
                counts[status] += 1
                continue
            if not row["decision_sample_eligible"] or row["decision_state"] not in {"WATCH", "SETUP", "READY"}:
                # Older pending rows can predate the admission guard. A valid
                # score rejection is CASH; an already-filled invalid row is not.
                rejected = row["status"] == "pending" and row["decision_state"] == "REJECTED" and all(
                    row.get(name) is None for name in ("entry_at", "entry_price", "exit_at", "exit_price")
                )
                status = "rejected" if rejected else "unmeasurable"
                connection.execute(
                    "UPDATE analysis.shadow_trade SET status = %s, pending_entry_reason = %s WHERE id = %s",
                    [status, "candidate_gate_rejected" if rejected else "decision_not_admitted", row["id"]],
                )
                counts[status] += 1
                continue
            expiry = datetime.fromisoformat(str(metrics.get("entry_deadline"))) if metrics.get("entry_deadline") else None
            legs = latest_option_legs(
                connection, ticket_legs=list(ticket.get("legs") or []), as_of=now,
                source_id=str(metrics.get("source_id") or ""), complete_capture_only=True,
            )
            later_than = row["entry_at"] or max(row["as_of"], row["created_at"])
            policy = execution_policy(legs, structure=str(row["structure"]), entry_price=ticket.get("entry", {}).get("limit_price"), market_session="regular" if is_market_open(now) else "closed", evaluated_at=now)
            ordered = bool(legs) and all(
                leg.get("observed_at") is not None and later_than < leg["observed_at"] <= leg["quote_time"] <= now
                and leg.get("multiplier") == 100 for leg in legs
            )
            if row["status"] == "pending":
                if expiry is None or expiry <= now:
                    connection.execute("UPDATE analysis.shadow_trade SET status = 'unfilled', pending_entry_reason = 'entry_window_elapsed' WHERE id = %s", [row["id"]])
                    counts["unfilled"] += 1
                elif ordered and not policy["blockers"]:
                    price = package_price(legs, phase="entry")
                    limit_price = (ticket.get("entry") or {}).get("maximum_chase_price")
                    if price is None or limit_price is None or price > float(limit_price):
                        continue
                    metrics.update({"entry_quotes": legs, "entry_fees": FEE_PER_CONTRACT_LEG * len(legs), "entry_slippage": sum((leg["ask"] - (leg["bid"] + leg["ask"]) / 2) * 100 for leg in legs)})
                    liquidation = package_price(legs, phase="exit")
                    if liquidation is not None:
                        initial_return = ((liquidation - price) * 100 - 2 * metrics["entry_fees"]) / (price * 100)
                        _mark_shadow_return(metrics, net_return=initial_return, quotes=legs, observed_at=now)
                    connection.execute("UPDATE analysis.shadow_trade SET status = 'entered', entry_at = %s, entry_price = %s, fill_basis = 'later_complete_quote_worst_side', metrics = %s WHERE id = %s", [now, price, _jsonb(metrics), row["id"]])
                    counts["entered"] += 1
                continue
            if not ordered or policy["blockers"]:
                expiration = ticket.get("expiration")
                if expiration and now.date().isoformat() > str(expiration)[:10]:
                    connection.execute("UPDATE analysis.shadow_trade SET status = 'unmeasurable', pending_entry_reason = 'no_executable_exit_before_expiry' WHERE id = %s", [row["id"]])
                    counts["unmeasurable"] += 1
                continue
            price = package_price(legs, phase="exit")
            if price is None:
                continue
            fees = float(metrics["entry_fees"]) + FEE_PER_CONTRACT_LEG * len(legs)
            net_return = ((price - float(row["entry_price"])) * 100 - fees) / (float(row["entry_price"]) * 100)
            _mark_shadow_return(metrics, net_return=net_return, quotes=legs, observed_at=now)
            reason = exit_reason(ticket=ticket, exits=dict(ticket.get("exits") or {}), credit=False, entry_price=float(row["entry_price"]), exit_price=price, execution_blockers=[], now=now)
            if not reason:
                connection.execute("UPDATE analysis.shadow_trade SET metrics = %s WHERE id = %s", [_jsonb(metrics), row["id"]])
                continue
            metrics.update({"exit_quotes": legs, "exit_reason": reason, "fees": fees, "exit_slippage": sum(((leg["bid"] + leg["ask"]) / 2 - leg["bid"]) * 100 for leg in legs)})
            connection.execute("UPDATE analysis.shadow_trade SET status = 'closed', exit_at = %s, exit_price = %s, metrics = %s WHERE id = %s", [now, price, _jsonb(metrics), row["id"]])
            connection.execute(
                """INSERT INTO analysis.option_outcome
                       (decision_id, maturity_state, observed_through, current_return, peak_return, max_drawdown,
                        realized_exit_return, realized_exit_basis, outcome_source, shadow_trade_id, objective_version,
                        outcome_classification, promotion_eligible, entry_fill_at, entry_fill_price, exit_fill_at,
                        exit_fill_price, exit_reason, fee_total, slippage_total, lane, episode_key,
                        sample_eligible, calibration_cohort)
                   VALUES (%s, 'mature', %s, %s, %s, %s, %s, 'prospective_shadow_after_costs', 'generic', %s, %s,
                           'captured', false, %s, %s, %s, %s, %s, %s, %s, 'radar', %s, true, %s)
                   ON CONFLICT (decision_id) DO NOTHING""",
                [row["decision_id"], now, net_return, metrics["peak_return"], metrics["max_drawdown"], net_return,
                 row["id"], EXPERIMENT_VERSION, row["entry_at"], row["entry_price"], now, price, reason, fees,
                 float(metrics["entry_slippage"]) + metrics["exit_slippage"], row["episode_key"], row["calibration_cohort"]],
            )
            counts["closed"] += 1
    return counts


def _mark_shadow_return(metrics: dict[str, Any], *, net_return: float, quotes: list[dict[str, Any]], observed_at: datetime) -> None:
    """Include entry capital and keep observed peak-to-trough wealth evidence."""
    previous_peak = float(metrics.get("peak_return") or 0)
    peak = max(0.0, previous_peak, net_return)
    previous_drawdown = float(metrics.get("max_drawdown") or 0)
    drawdown = (1 + net_return) / (1 + peak) - 1
    if "peak_at" not in metrics or peak > previous_peak:
        metrics.update({"peak_at": observed_at.isoformat(), "peak_quotes": quotes,
                        "peak_basis": "observed_liquidation" if peak > 0 else "initial_entry_capital"})
    metrics.update({"initial_wealth": 1.0, "current_return": net_return, "peak_return": peak,
                    "max_drawdown": min(previous_drawdown, drawdown),
                    "drawdown_basis": "observed_peak_wealth_v1", "observed_at": observed_at.isoformat(),
                    "observed_quotes": quotes})
    if drawdown <= previous_drawdown:
        metrics.update({"drawdown_peak_at": metrics["peak_at"], "drawdown_peak_quotes": metrics["peak_quotes"],
                        "drawdown_trough_at": observed_at.isoformat(), "drawdown_trough_quotes": quotes})


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=lambda item: json.dumps(item, default=str))
