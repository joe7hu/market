"""Shared source/provider status vocabulary."""

from __future__ import annotations

from typing import Any


DOCUMENTATION_STATUSES = {"verified_docs", "documentation", "docs_only"}
OK_STATUSES = {"ok", "loaded", "checked", "success", "succeeded", "fresh"}
DISABLED_STATUSES = {"disabled", "not_configured"}
DEGRADED_STATUSES = {"partial", "degraded", "not_loaded", "stale", "unreachable", "configured", "missing"}
FAILED_STATUSES = {"error", "failed", "failure", "missing_dependency", "gateway_offline", "repair_required"}


def normalize_source_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    if raw in DOCUMENTATION_STATUSES:
        return "documentation"
    if raw in OK_STATUSES:
        return "ok"
    if raw in DISABLED_STATUSES:
        return "disabled"
    if raw in FAILED_STATUSES or raw.startswith("http_"):
        return "failed"
    if raw in DEGRADED_STATUSES:
        return "degraded"
    return "unknown" if not raw else raw


def source_status_severity(status: Any) -> str:
    normalized = normalize_source_status(status)
    if normalized == "failed":
        return "bad"
    if normalized in {"degraded", "unknown"}:
        return "warn"
    if normalized in {"documentation", "disabled"}:
        return "info"
    return "good"
