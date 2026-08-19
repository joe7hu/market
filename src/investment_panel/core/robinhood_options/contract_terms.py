"""Fail-closed Robinhood option-contract term classification."""

from __future__ import annotations

from typing import Any

from investment_panel.core.coercion import to_finite_float as as_float


def verified_robinhood_contract_terms(instrument: dict[str, Any]) -> dict[str, Any]:
    """Classify only provider-proven standard US equity option contracts."""

    chain = instrument.get("_chain_metadata")
    requested_symbol = str(instrument.get("_requested_symbol") or "").upper().strip()
    chain_id = str(instrument.get("chain_id") or "").strip()
    identity = str(instrument.get("deliverable_key") or chain_id).strip() or None
    if not isinstance(chain, dict):
        return {
            "style": instrument.get("exercise_style") or instrument.get("style"),
            "settlement": instrument.get("settlement_type") or instrument.get("settlement"),
            "deliverable_key": identity,
            "standard_contract_verified": False,
        }
    chain_symbol = str(chain.get("symbol") or "").upper().strip()
    instrument_symbol = str(instrument.get("chain_symbol") or "").upper().strip()
    underlying_type = str(instrument.get("underlying_type") or "").lower().strip()
    underlyings = chain.get("underlying_instruments")
    trade_multiplier = as_float(
        instrument.get("trade_value_multiplier") or chain.get("trade_value_multiplier")
    )
    cash_component = chain.get("cash_component")
    cash_is_absent = cash_component is None or as_float(cash_component) == 0
    one_underlying = (
        isinstance(underlyings, list)
        and len(underlyings) == 1
        and isinstance(underlyings[0], dict)
        and bool(str(underlyings[0].get("instrument") or "").strip())
    )
    verified = (
        bool(chain_id)
        and chain_id == str(chain.get("id") or "").strip()
        and bool(requested_symbol)
        and chain_symbol == requested_symbol
        and instrument_symbol == requested_symbol
        and underlying_type == "equity"
        and trade_multiplier == 100
        and cash_is_absent
        and one_underlying
        and chain.get("settle_on_open") is False
    )
    return {
        "style": "american" if verified else None,
        "settlement": "physical" if verified else None,
        "deliverable_key": f"robinhood-chain:{chain_id}" if chain_id else identity,
        "standard_contract_verified": verified,
    }


def attach_chain_metadata(
    instruments: list[dict[str, Any]], chain: dict[str, Any], requested_symbol: str,
) -> list[dict[str, Any]]:
    """Attach chain evidence for normalization without changing provider identity."""

    for instrument in instruments:
        instrument["_chain_metadata"] = chain
        instrument["_requested_symbol"] = requested_symbol
    return instruments
