"""Strict structured-output contracts for the research-only option agent."""

from __future__ import annotations

from typing import Any


THESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ticker", "direction", "bull_target_price", "bull_target_date",
        "base_target_price", "bear_target_price", "scenario_probabilities",
        "preferred_structures", "core_thesis", "required_proofs", "catalysts",
        "invalidation", "bear_case", "confidence", "evidence_refs",
    ],
    "properties": {
        "ticker": {"type": "string"},
        "direction": {"type": "string", "enum": ["long", "short"]},
        "bull_target_price": {"type": "number"},
        "bull_target_date": {"type": "string"},
        "base_target_price": {"type": "number"},
        "bear_target_price": {"type": "number"},
        "scenario_probabilities": {
            "type": "object", "additionalProperties": False,
            "required": ["base", "bull", "bear"],
            "properties": {
                "base": {"type": "number"}, "bull": {"type": "number"}, "bear": {"type": "number"},
            },
        },
        "preferred_structures": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["long_call", "long_put", "call_debit_spread", "put_debit_spread", "cash_secured_put"],
            },
        },
        "core_thesis": {"type": "string"},
        "required_proofs": {"type": "array", "items": {"type": "string"}},
        "catalysts": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["type", "expected_window", "what_to_watch"],
                "properties": {
                    "type": {"type": "string"},
                    "expected_window": {"type": "string"},
                    "what_to_watch": {"type": "string"},
                },
            },
        },
        "invalidation": {"type": "array", "items": {"type": "string"}},
        "bear_case": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_refs": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["type", "id"],
                "properties": {"type": {"type": "string"}, "id": {"type": "string"}},
            },
        },
    },
}


POSTMORTEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ticker", "strategy_version", "source_type", "source_id", "outcome_type",
        "failure_type", "evidence", "proposed_rule_change", "proposed_parameter_changes",
        "expected_effect", "risk", "confidence", "evidence_refs",
    ],
    "properties": {
        "ticker": {"type": "string"},
        "strategy_version": {"type": "string"},
        "source_type": {"type": "string"},
        "source_id": {"type": "string"},
        "outcome_type": {"type": "string"},
        "failure_type": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "proposed_rule_change": {"type": "string"},
        "proposed_parameter_changes": {
            "type": "object", "additionalProperties": False,
            "required": [
                "delta_min", "delta_max", "dte_min", "dte_max", "max_spread_pct",
                "reject_spread_pct", "min_open_interest", "min_volume",
                "max_required_move_pct", "max_iv_percentile", "reject_iv_percentile",
                "require_price_above_ma50", "require_rs_improving", "candidate_note",
                "filter_reason", "setup_type",
            ],
            "properties": {
                "delta_min": {"type": ["number", "null"]},
                "delta_max": {"type": ["number", "null"]},
                "dte_min": {"type": ["number", "null"]},
                "dte_max": {"type": ["number", "null"]},
                "max_spread_pct": {"type": ["number", "null"]},
                "reject_spread_pct": {"type": ["number", "null"]},
                "min_open_interest": {"type": ["number", "null"]},
                "min_volume": {"type": ["number", "null"]},
                "max_required_move_pct": {"type": ["number", "null"]},
                "max_iv_percentile": {"type": ["number", "null"]},
                "reject_iv_percentile": {"type": ["number", "null"]},
                "require_price_above_ma50": {"type": ["boolean", "null"]},
                "require_rs_improving": {"type": ["boolean", "null"]},
                "candidate_note": {"type": ["string", "null"]},
                "filter_reason": {"type": ["string", "null"]},
                "setup_type": {"type": ["string", "null"]},
            },
        },
        "expected_effect": {"type": "string"},
        "risk": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_refs": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["type", "id"],
                "properties": {"type": {"type": "string"}, "id": {"type": "string"}},
            },
        },
    },
}
