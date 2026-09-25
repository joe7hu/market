"""Quote already-owned contracts by immutable provider ID, not radar sampling."""
from __future__ import annotations

from typing import Any, Callable
from datetime import datetime
import time


def collect_active_quotes(client: Any, contracts: list[dict[str, Any]], *,
                          batch_size: int, deadline: float,
                          payload_rows: Callable[[Any], list[dict[str, Any]]],
                          normalize_quote: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None],
                          ) -> dict[str, Any]:
    # The collector owns provider normalization; this helper owns only bounded
    # identity-based capture. Inject the two transformations, not its module.

    rows: dict[str, list[dict[str, Any]]] = {}
    diagnostics: list[dict[str, Any]] = []
    errors: list[str] = []
    attempted: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for contract in contracts:
        provider_id = str(contract.get("provider_instrument_id") or "").strip()
        trace = {"contract_id": contract["contract_id"], "symbol": contract["symbol"],
                 "provider_instrument_id": provider_id, "stage": "queued", "reason": None}
        diagnostics.append(trace)
        # One native ID cannot prove two distinct catalog/deliverable identities.
        if provider_id in by_id:
            trace.update(stage="identity", reason="duplicate_provider_identity")
            by_id[provider_id]["trace"].update(stage="identity", reason="duplicate_provider_identity")
        else:
            by_id[provider_id] = {"contract": contract, "trace": trace}
    for offset in range(0, len(by_id), batch_size):
        batch = list(by_id)[offset:offset + batch_size]
        batch = [key for key in batch if by_id[key]["trace"]["reason"] is None]
        if not batch:
            continue
        if time.monotonic() >= deadline:
            for key in batch:
                by_id[key]["trace"].update(stage="queued", reason="collection_deadline_exceeded")
            continue
        for key in batch:
            symbol = by_id[key]["contract"]["symbol"]
            if symbol not in attempted:
                attempted.append(symbol)
        try:
            payload = client.get_option_quotes(batch)
        except Exception as exc:
            for key in batch:
                by_id[key]["trace"].update(stage="provider_request", reason=f"provider_error:{type(exc).__name__}")
            continue
        received: dict[str, dict[str, Any]] = {}
        duplicate_ids: set[str] = set()
        for result in payload_rows(payload):
            quote = dict(result.get("quote") or {})
            key = str(quote.get("instrument_id") or result.get("instrument_id") or "")
            if key not in batch:
                continue
            if key in received:
                duplicate_ids.add(key)
            received[key] = quote
        for key in batch:
            source, trace = by_id[key]["contract"], by_id[key]["trace"]
            if key in duplicate_ids:
                trace.update(stage="provider_response", reason="duplicate_provider_quote")
                continue
            if key not in received:
                trace.update(stage="provider_response", reason="provider_omitted_contract")
                continue
            try:
                observed = datetime.fromisoformat(str(received[key].get("updated_at") or "").replace("Z", "+00:00"))
            except ValueError:
                observed = None
            if observed is None or observed.tzinfo is None:
                trace.update(stage="normalization", reason="provider_quote_time_unconfirmed")
                continue
            quote = normalize_quote({"id": key, "expiration_date": source["expiration"],
                                      "strike_price": source["strike"], "type": source["option_type"]}, received[key])
            if quote is None:
                trace.update(stage="normalization", reason="invalid_contract_fields")
                continue
            # These are persisted catalog terms from the original provider
            # capture. Never reclassify by strike or silently change deliverable.
            for name in ("deliverable_key", "multiplier", "style", "settlement", "standard_contract_verified"):
                quote[name] = source.get(name)
            quote["provider_payload"] = {"requested_contract_id": source["contract_id"],
                                         "identity_source": "persisted_catalog", "quote": received[key]}
            rows.setdefault(source["symbol"], []).append(quote)
            trace.update(stage="normalized", reason=None)
    for trace in diagnostics:
        if trace["reason"]:
            errors.append(f"{trace['symbol']}:contract={trace['contract_id']}:{trace['reason']}")
    return {"rows": rows, "errors": errors, "symbols_attempted": attempted, "contract_diagnostics": diagnostics}
