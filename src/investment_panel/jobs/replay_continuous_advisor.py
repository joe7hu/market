"""Resolve continuous-advisor claims only after their declared horizons."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any

from investment_panel.settings import load_config
from investment_panel.core.continuous_advisor import packet_is_replay_safe, resolve_claim
from investment_panel.domain.decision import MARKET_TZ, is_us_market_day, market_session_bounds
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.continuous_advisor import ContinuousAdvisorRepository


_INVALIDATION_RE = re.compile(
    r"\b(below|under|above|over)\s+\$?([0-9]+(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)


def run(config_path: str | None = None, *, now: datetime | None = None, limit: int = 500) -> dict[str, Any]:
    config = load_config(config_path)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("replay now must be timezone-aware")
    reference = reference.astimezone(UTC)
    repository = ContinuousAdvisorRepository(runtime_for_config(config))
    resolved = quarantined = waiting = 0
    results: list[dict[str, Any]] = []
    for item in repository.unresolved_claims(limit=limit):
        packet = dict(item.get("packet") or {})
        claim = {
            **dict(item.get("claim") or {}),
            "claim_key": item.get("claim_key"),
            "claim_kind": item.get("claim_kind"),
            "horizon": item.get("horizon"),
            "direction": item.get("direction"),
            "probability": item.get("probability"),
        }
        if not packet_is_replay_safe(packet):
            repository.record_outcome(
                str(item["claim_id"]),
                {"status": "quarantined", "evidence_valid": False, "metadata": {"reason": "packet_not_replay_safe"}},
            )
            quarantined += 1
            results.append({"claim_id": item["claim_id"], "status": "quarantined", "reason": "packet_not_replay_safe"})
            continue
        cutoff = _parse_datetime(packet.get("cutoff"))
        horizon = _horizon_delta(str(item.get("horizon") or ""))
        if cutoff is None or horizon is None:
            repository.record_outcome(
                str(item["claim_id"]),
                {"status": "unresolvable", "evidence_valid": False, "metadata": {"reason": "unsupported_horizon"}},
            )
            resolved += 1
            results.append({"claim_id": item["claim_id"], "status": "unresolvable", "reason": "unsupported_horizon"})
            continue
        measured_from = cutoff + horizon
        observation_end = _observation_window_end(measured_from)
        if measured_from > reference or (observation_end is not None and observation_end > reference):
            repository.record_resolution_attempt(
                str(item["claim_id"]),
                {
                    "status": "waiting",
                    "reason": "horizon_not_reached",
                    "metadata": {
                        "target_at": measured_from,
                        "observation_window_end": observation_end,
                    },
                },
            )
            waiting += 1
            results.append({"claim_id": item["claim_id"], "status": "waiting", "reason": "horizon_not_reached"})
            continue
        base_price = (((packet.get("evidence") or {}).get("prices") or {}).get("price"))
        quote = repository.quote_at_or_after(
            str(item["symbol"]), measured_from, available_by=reference, observed_to=observation_end
        ) if observation_end is not None else None
        if base_price is None or not quote or quote.get("price") is None or float(base_price) <= 0:
            reason = "base_price_unavailable" if base_price is None or float(base_price) <= 0 else "price_outcome_unavailable"
            repository.record_resolution_attempt(
                str(item["claim_id"]),
                {
                    "status": "waiting",
                    "reason": reason,
                    "metadata": {
                        "target_at": measured_from,
                        "observation_window_end": observation_end,
                        "available_by": reference,
                    },
                },
            )
            waiting += 1
            results.append({"claim_id": item["claim_id"], "status": "waiting", "reason": reason})
            continue
        actual_return = float(quote["price"]) / float(base_price) - 1.0
        excess_return = _excess_return(repository, packet, measured_from, actual_return, reference, observation_end)
        invalidation_rule = _invalidation_rule(claim)
        invalidation_quote = (
            repository.quote_crossing_at_or_after(
                str(item["symbol"]), cutoff,
                observed_to=observation_end, available_by=reference,
                below=invalidation_rule[0], threshold=invalidation_rule[1],
            )
            if invalidation_rule is not None and observation_end is not None
            else None
        )
        invalidated = (
            invalidation_quote is not None
            if str(claim.get("claim_kind") or "") == "invalidation" and invalidation_rule is not None
            else _price_invalidation(claim, float(quote["price"]))
        )
        if claim.get("claim_kind") == "invalidation" and invalidated is None:
            outcome = {
                "status": "unresolvable",
                "evidence_valid": False,
                "measured_through": quote.get("observed_at"),
                "metadata": {"reason": "invalidation_condition_not_machine_resolvable"},
            }
        else:
            outcome = resolve_claim(
                claim,
                actual_return=actual_return,
                excess_return=excess_return,
                invalidated=invalidated,
                measured_through=quote.get("observed_at"),
                evidence_valid=True,
            )
        outcome.setdefault("metadata", {})
        outcome["metadata"].update({
            "outcome_quote_observed_at": quote.get("observed_at"),
            "packet_cutoff": packet.get("cutoff"),
            "observation_window_end": observation_end,
        })
        if invalidation_quote is not None:
            outcome["metadata"]["invalidation_quote_observed_at"] = invalidation_quote.get("observed_at")
        repository.record_outcome(str(item["claim_id"]), outcome)
        if outcome["status"] == "quarantined":
            quarantined += 1
        else:
            resolved += 1
        results.append({"claim_id": item["claim_id"], "status": outcome["status"], "actual_return": outcome.get("actual_return")})
    return {"status": "ok", "resolved": resolved, "quarantined": quarantined, "waiting": waiting, "results": results}


def _horizon_delta(value: str) -> timedelta | None:
    match = re.fullmatch(r"\s*(\d+)\s*(m|min|mins|minute|minutes|h|hour|hours|mo|month|months|d|day|days|w|week|weeks)\s*", value.lower())
    if not match:
        return None
    count = int(match.group(1))
    if count <= 0:
        return None
    unit = match.group(2)
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return timedelta(minutes=count)
    if unit in {"h", "hour", "hours"}:
        return timedelta(hours=count)
    if unit in {"mo", "month", "months"}:
        return timedelta(days=count * 30)
    if unit in {"w", "week", "weeks"}:
        return timedelta(days=count * 7)
    return timedelta(days=count)


def _observation_window_end(target: datetime) -> datetime | None:
    """End at the first market-session close on or after the target horizon."""

    local_target = target.astimezone(MARKET_TZ)
    day = local_target.date()
    for _ in range(32):
        if is_us_market_day(day):
            _open_at, close_at = market_session_bounds(day)
            close_utc = close_at.astimezone(UTC)
            if close_utc >= target:
                return close_utc
        day += timedelta(days=1)
    return None


def _price_invalidation(claim: dict[str, Any], price: float) -> bool | None:
    if str(claim.get("claim_kind") or "") != "invalidation":
        return None
    rule = _invalidation_rule(claim)
    if rule is None:
        return None
    below, threshold = rule
    return price < threshold if below else price > threshold


def _invalidation_rule(claim: dict[str, Any]) -> tuple[bool, float] | None:
    match = _INVALIDATION_RE.search(str(claim.get("condition") or claim.get("statement") or ""))
    if not match:
        return None
    return match.group(1).lower() in {"below", "under"}, float(match.group(2))


def _excess_return(
    repository: ContinuousAdvisorRepository,
    packet: dict[str, Any],
    measured_from: datetime,
    actual_return: float,
    reference: datetime,
    observation_end: datetime | None,
) -> float | None:
    benchmark = str(
        (((packet.get("evidence") or {}).get("macro_regime") or {}).get("benchmark_symbol") or "SPY")
    ).strip().upper()
    if not benchmark:
        return None
    cutoff = _parse_datetime(packet.get("cutoff"))
    if cutoff is None:
        return None
    base = repository.quote_at_or_before(benchmark, cutoff, available_by=cutoff)
    outcome = repository.quote_at_or_after(
        benchmark, measured_from, available_by=reference, observed_to=observation_end
    ) if observation_end is not None else None
    if not base or not outcome or base.get("price") is None or outcome.get("price") is None:
        return None
    base_price = float(base["price"])
    if base_price <= 0:
        return None
    return actual_return - (float(outcome["price"]) / base_price - 1.0)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    print(json.dumps(run(args.config, limit=args.limit), indent=2, default=str))


if __name__ == "__main__":
    main()


__all__ = ["run"]
