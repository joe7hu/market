"""Join the static source catalog with live freshness/health status.

``build_source_catalog_health`` is the read model behind ``GET /api/source-catalog``.
It takes the declarative ``SOURCE_CATALOG`` (primary/fallback/cadence wiring) and
joins it against the live status the panel already computes — ``source_freshness``
(from ``build_source_freshness``), ``source_health``, ``broker_provider_status``,
and the ``source_runs`` rows produced by the live opencli ingestion — so the
Health page can render Family → Category → Primary/Fallback chains with live dots.
"""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import query_rows
from investment_panel.core.decision import build_source_freshness, parse_dt
from investment_panel.core.source_catalog import SOURCE_CATALOG, DataCategory
from investment_panel.core.source_status import normalize_source_status, source_status_severity

# Map a normalized provider status to a UI tone.
_SEVERITY_TONE = {"good": "good", "warn": "warn", "bad": "bad", "info": "neutral"}
# Category tone roll-up: worst wins.
_TONE_RANK = {"bad": 3, "warn": 2, "neutral": 1, "good": 0, "unknown": 1}


def build_source_catalog_health(con: Any) -> dict[str, Any]:
    """Return the catalog with live primary/fallback status per category."""

    freshness_rows = build_source_freshness(con)
    provider_index = _provider_status_index(freshness_rows)
    rate_limited = _rate_limited_providers(con)

    categories: list[dict[str, Any]] = []
    for category in SOURCE_CATALOG:
        primary = _provider_block(category.primary, provider_index, rate_limited)
        fallbacks = [_provider_block(name, provider_index, rate_limited) for name in category.fallback]
        tone = _category_tone(category, primary, fallbacks)
        categories.append(
            {
                "id": category.id,
                "label": category.label,
                "family": category.family,
                "cadence_label": category.cadence_label,
                "cadence_seconds": category.cadence_seconds,
                "refresh_job": category.refresh_job,
                "stale_after": category.stale_after,
                "source_types": list(category.source_types),
                "live_fetcher": category.live_fetcher,
                "tone": tone,
                "primary": primary,
                "fallback": fallbacks,
            }
        )

    families: dict[str, list[str]] = {}
    for entry in categories:
        families.setdefault(entry["family"], []).append(entry["id"])

    return {
        "categories": categories,
        "families": families,
        "generated_from": "source_catalog",
    }


def _provider_status_index(freshness_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse freshness rows to one best-known status per provider name."""

    index: dict[str, dict[str, Any]] = {}
    for row in freshness_rows:
        provider = str(row.get("provider") or "").strip()
        if not provider:
            continue
        observed = parse_dt(row.get("last_observed_at"))
        status = normalize_source_status(row.get("provider_status") or row.get("status"))
        entry = index.get(provider)
        symbol_count = 1 if _is_symbol_key(row.get("source_key")) else 0
        if entry is None:
            index[provider] = {
                "provider_status": status,
                "last_observed_at": observed,
                "stale_after": row.get("stale_after"),
                "freshness_status": row.get("freshness_status"),
                "detail": row.get("detail") or "",
                "symbol_count": symbol_count,
            }
            continue
        entry["symbol_count"] += symbol_count
        # Keep the most recent observation and its accompanying status.
        if observed is not None and (entry["last_observed_at"] is None or observed > entry["last_observed_at"]):
            entry["last_observed_at"] = observed
            entry["provider_status"] = status
            entry["freshness_status"] = row.get("freshness_status")
            entry["detail"] = row.get("detail") or ""
            entry["stale_after"] = row.get("stale_after")
    return index


def _provider_block(name: str, index: dict[str, dict[str, Any]], rate_limited: set[str]) -> dict[str, Any]:
    entry = index.get(name)
    is_rate_limited = name in rate_limited
    if entry is None:
        status = "rate_limited" if is_rate_limited else "unknown"
        return {
            "provider": name,
            "status": status,
            "tone": "warn" if is_rate_limited else "unknown",
            "provider_status": status,
            "last_observed_at": None,
            "stale_after": "",
            "symbol_count": 0,
            "rate_limited": is_rate_limited,
            "freshness_status": "unknown",
            "detail": "",
        }
    provider_status = "rate_limited" if is_rate_limited else entry["provider_status"]
    tone = "warn" if is_rate_limited else _SEVERITY_TONE.get(source_status_severity(entry["provider_status"]), "unknown")
    return {
        "provider": name,
        "status": provider_status,
        "tone": tone,
        "provider_status": entry["provider_status"],
        "last_observed_at": entry["last_observed_at"],
        "stale_after": entry.get("stale_after") or "",
        "symbol_count": int(entry.get("symbol_count") or 0),
        "rate_limited": is_rate_limited,
        "freshness_status": entry.get("freshness_status") or "unknown",
        "detail": entry.get("detail") or "",
    }


def _category_tone(category: DataCategory, primary: dict[str, Any], fallbacks: list[dict[str, Any]]) -> str:
    if not category.live_fetcher and category.id in {"daily", "podcasts", "ingestion_runs"}:
        # Catalog-only / internally computed: don't paint these red for "no provider".
        if category.id == "podcasts":
            return "neutral"
    # A category is healthy if any link in its chain is good.
    blocks = [primary, *fallbacks]
    tones = [block["tone"] for block in blocks]
    if "good" in tones:
        return "good"
    return max(tones, key=lambda tone: _TONE_RANK.get(tone, 1))


def _rate_limited_providers(con: Any) -> set[str]:
    """Providers whose most recent source_run was rate_limited."""

    rows = query_rows(
        con,
        """
        SELECT source_id, status
        FROM source_runs
        QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC NULLS LAST) = 1
        """,
    )
    return {str(row.get("source_id")) for row in rows if str(row.get("status") or "").lower() == "rate_limited"}


def _is_symbol_key(source_key: Any) -> bool:
    key = str(source_key or "")
    tail = key.split(":")[-1]
    return bool(tail) and tail.isupper() and tail.isalnum()
