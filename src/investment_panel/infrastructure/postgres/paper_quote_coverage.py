"""Reconcile targeted provider replies against exact persisted ticket contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def reconcile_capture(runtime: DatabaseRuntime, *, snapshot_id: int,
                      requested: list[dict[str, Any]], received_at: datetime,
                      diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    identities = {int(row["contract_id"]): row for row in requested}
    traces = {int(row["contract_id"]): dict(row) for row in diagnostics}
    with runtime.read() as connection:
        persisted = {int(row["contract_id"]): row for row in connection.execute(
            """SELECT quote.contract_id, quote.observed_at, quote.available_at,
                      quote.provider_observed_at, quote.bid, quote.ask,
                      snapshot.capture_state, generation.capture_state AS generation_state
               FROM raw.option_quote quote
               JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
               LEFT JOIN raw.option_capture_generation generation ON generation.id = quote.capture_generation_id
               WHERE quote.snapshot_id = %s AND quote.contract_id = ANY(%s)""",
            [snapshot_id, list(identities)],
        ).fetchall()}
    matched: list[int] = []
    errors: list[str] = []
    local_day = received_at.astimezone(ZoneInfo("America/New_York")).date()
    for contract_id, contract in identities.items():
        trace = traces.setdefault(contract_id, {"contract_id": contract_id, "symbol": contract["symbol"],
                                                "provider_instrument_id": contract.get("provider_instrument_id")})
        quote = persisted.get(contract_id)
        reason = None
        if quote is None:
            reason = trace.get("reason") or "requested_contract_not_persisted"
        elif quote["capture_state"] != "complete" or quote["generation_state"] not in (None, "complete"):
            reason = "persisted_capture_incomplete"
        elif quote["provider_observed_at"] is None:
            reason = "provider_timestamp_missing"
        elif quote["observed_at"] > received_at or quote["available_at"] > received_at:
            reason = "provider_quote_from_future"
        elif quote["observed_at"].astimezone(ZoneInfo("America/New_York")).date() != local_day:
            reason = "provider_quote_from_prior_session"
        elif quote["bid"] is None or quote["ask"] is None or not (0 < quote["bid"] <= quote["ask"]):
            reason = "provider_quote_not_executable"
        if reason:
            if quote is not None or not trace.get("reason"):
                trace["stage"] = "persistence" if quote is None else "quote_validation"
            trace["reason"] = reason
            errors.append(f"{contract['symbol']}:contract={contract_id}:{reason}")
        else:
            matched.append(contract_id)
            trace.update(stage="persisted", reason=None,
                         observed_at=quote["observed_at"].isoformat(), available_at=quote["available_at"].isoformat())
    return {"matched_contract_ids": sorted(matched),
            "contract_diagnostics": [traces[key] for key in identities], "coverage_errors": errors}
