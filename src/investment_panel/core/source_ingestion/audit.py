"""Ingestion correctness checks for active data sources."""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import query_rows
from investment_panel.core.source_ingestion.canonical import sync_canonical_sources
from investment_panel.core.source_ingestion.read_models import source_registry_rows


LOGIN_GATED_BROKERS = {"ibkr", "moomoo"}
EXPECTED_LOGIN_STATUSES = {
    "disabled",
    "missing",
    "missing_dependency",
    "offline",
    "auth_required",
    "login_required",
    "degraded",
    "unreachable",
}


def source_ingestion_audit(con: Any) -> dict[str, Any]:
    """Return a strict audit of active source ingestion.

    Disabled registry candidates are informational, not failures. IBKR and
    moomoo are allowed to be empty when login/session prerequisites are absent.
    """

    sync_canonical_sources(con)
    source_rows = source_registry_rows(con)
    source_failures = []
    for row in source_rows:
        failure = source_failure(row)
        if failure is not None:
            source_failures.append(failure)
    broker_rows = broker_audit_rows(con)
    broker_failures = [row for row in broker_rows if row["status"] == "failure"]
    failures = source_failures + broker_failures
    return {
        "status": "ok" if not failures else "failed",
        "active_sources": sum(1 for row in source_rows if row.get("enabled") is True),
        "disabled_sources": sum(1 for row in source_rows if row.get("enabled") is not True),
        "source_failures": source_failures,
        "broker_rows": broker_rows,
        "failures": failures,
    }


def source_failure(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("enabled") is not True:
        return None
    source_id = str(row.get("source_id") or "")
    latest_status = str(row.get("latest_run_status") or "").lower()
    item_count = int(row.get("items_count") or 0)
    signal_count = int(row.get("signals_count") or 0)
    if source_id in LOGIN_GATED_BROKERS and latest_status in EXPECTED_LOGIN_STATUSES:
        return None
    if latest_status in {"failed", "error"}:
        return {
            "source_id": source_id,
            "status": latest_status,
            "detail": row.get("latest_failure_detail") or "Latest source run failed.",
        }
    if item_count == 0 and signal_count == 0:
        return {
            "source_id": source_id,
            "status": "not_ingested",
            "detail": "Enabled source has no canonical items or ticker signals.",
        }
    return None


def broker_audit_rows(con: Any) -> list[dict[str, Any]]:
    status_by_provider = {
        str(row.get("provider") or ""): row
        for row in query_rows(con, "SELECT provider, status, detail, checked_at, last_data_at FROM broker_provider_status")
    }
    rows: list[dict[str, Any]] = []
    for provider in sorted(LOGIN_GATED_BROKERS):
        row = status_by_provider.get(provider)
        if not row:
            rows.append(
                {
                    "provider": provider,
                    "status": "expected_login_required",
                    "provider_status": "missing",
                    "detail": f"{provider} has no local login/session sync; this is expected until credentials and a live session are available.",
                }
            )
            continue
        provider_status = str(row.get("status") or "missing").lower()
        if provider_status == "ok":
            status = "ok"
        elif provider_status in EXPECTED_LOGIN_STATUSES:
            status = "expected_login_required"
        else:
            status = "failure"
        rows.append(
            {
                "provider": provider,
                "status": status,
                "provider_status": provider_status,
                "detail": row.get("detail") or "",
                "checked_at": row.get("checked_at"),
                "last_data_at": row.get("last_data_at"),
            }
        )
    return rows
