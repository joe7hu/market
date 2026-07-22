"""Verification identity helpers for option relative-value packages."""

from __future__ import annotations

from typing import Any


def candidate_finding(findings: list[dict[str, Any]], contract_id: int) -> dict[str, Any] | None:
    matches = [finding for finding in findings if contract_id in {int(value) for value in finding.get("contract_ids", [])}]
    if not matches:
        return None
    return max(matches, key=lambda finding: float(finding.get("edge") or 0))


def same_finding_identity(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    expected_identity = dict(expected.get("package_identity") or {})
    observed_identity = dict(observed.get("package_identity") or {})
    expected_kind = expected_identity.get("kind") or expected.get("kind")
    observed_kind = observed_identity.get("kind") or observed.get("kind")
    expected_contracts = expected_identity.get("ordered_contract_ids") or expected.get("contract_ids")
    observed_contracts = observed_identity.get("ordered_contract_ids") or observed.get("contract_ids")
    expected_sides = expected_identity.get("leg_sides") or expected.get("leg_sides")
    observed_sides = observed_identity.get("leg_sides") or observed.get("leg_sides")
    return (
        expected_kind == observed_kind
        and [int(value) for value in expected_contracts or []] == [int(value) for value in observed_contracts or []]
        and list(expected_sides or []) == list(observed_sides or [])
    )
