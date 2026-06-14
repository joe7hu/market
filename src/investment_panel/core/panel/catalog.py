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


# Catalog provider names whose live status lives in source_runs (live opencli
# ingestion) rather than the per-symbol freshness index. Values match a
# source_runs.source_id exactly, or as a prefix for dynamic ids (blog_<host>).
_RUN_SOURCE_ALIASES: dict[str, tuple[str, bool]] = {
    "x_list": ("birdclaw_primary_tweets", False),
    "x_account": ("birdclaw_primary_tweets", False),
    "arco_birdclaw": ("birdclaw_primary_tweets", False),
    "substack": ("blog_", True),
    "web_rss": ("blog_", True),
}

# Catalog provider names that aggregate several freshness providers. Filings come
# in under source_type="filing" keyed by the disclosure source_type (13f, House
# CSV, etc.), not by "sec_edgar"/"house_disclosures", so map those names to the
# concrete freshness provider keys they represent.
_FRESHNESS_PROVIDER_ALIASES: dict[str, list[str]] = {
    "sec_edgar": ["13f", "disclosure"],
    "house_disclosures": ["public_disclosure_transaction", "pelositracker_portfolio", "trader_portfolio_model"],
}


def build_source_catalog_health(con: Any) -> dict[str, Any]:
    """Return the catalog with live primary/fallback status per category."""

    freshness_rows = build_source_freshness(con)
    provider_index = _provider_status_index(freshness_rows)
    run_index = _run_status_index(con)
    rate_limited = _rate_limited_providers(con)

    categories: list[dict[str, Any]] = []
    for category in SOURCE_CATALOG:
        primary = _provider_block(category.primary, provider_index, rate_limited, run_index)
        fallbacks = [_provider_block(name, provider_index, rate_limited, run_index) for name in category.fallback]
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


def _resolve_freshness_entry(name: str, index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Look up a provider's freshness entry, aggregating aliased providers.

    Most catalog providers map 1:1 to a freshness ``provider`` value. Filings are
    the exception: many disclosure source_types roll up under one catalog name
    (sec_edgar / house_disclosures), so merge their entries.
    """

    direct = index.get(name)
    if direct is not None:
        return direct
    keys = _FRESHNESS_PROVIDER_ALIASES.get(name)
    if not keys:
        return None
    entries = [index[key] for key in keys if key in index]
    return _merge_freshness_entries(entries) if entries else None


def _merge_freshness_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge several provider entries: most-recent observation wins, counts sum."""

    merged = dict(entries[0])
    merged["symbol_count"] = sum(int(entry.get("symbol_count") or 0) for entry in entries)
    best = None
    for entry in entries:
        observed = entry.get("last_observed_at")
        if observed is not None and (best is None or observed > best):
            best = observed
            merged["last_observed_at"] = observed
            merged["provider_status"] = entry["provider_status"]
            merged["freshness_status"] = entry.get("freshness_status")
            merged["stale_after"] = entry.get("stale_after")
            merged["detail"] = entry.get("detail") or ""
    return merged


def _provider_block(
    name: str,
    index: dict[str, dict[str, Any]],
    rate_limited: set[str],
    run_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry = _resolve_freshness_entry(name, index)
    is_rate_limited = name in rate_limited or _alias_rate_limited(name, rate_limited)
    if entry is None:
        # Live opencli sources (X/blogs) report status via source_runs, not the
        # per-symbol freshness index — resolve those here before giving up.
        run = _alias_run_status(name, run_index or {})
        if run is not None:
            severity = source_status_severity(run["status"])
            return {
                "provider": name,
                "status": "rate_limited" if is_rate_limited else run["status"],
                "tone": "warn" if is_rate_limited else _SEVERITY_TONE.get(severity, "unknown"),
                "provider_status": run["status"],
                "last_observed_at": run.get("finished_at"),
                "stale_after": "",
                "symbol_count": 0,
                "rate_limited": is_rate_limited,
                "freshness_status": run["status"],
                "detail": run.get("detail") or "",
            }
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


def _run_status_index(con: Any) -> dict[str, dict[str, Any]]:
    """Latest source_run per source_id (status + finished_at) for live sources."""

    rows = query_rows(
        con,
        """
        SELECT source_id, status, finished_at, failure_detail
        FROM source_runs
        QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC NULLS LAST) = 1
        """,
    )
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = str(row.get("source_id") or "")
        if not source_id:
            continue
        index[source_id] = {
            "status": normalize_source_status(row.get("status")),
            "finished_at": parse_dt(row.get("finished_at")),
            "detail": row.get("failure_detail") or "",
        }
    return index


def _alias_source_ids(name: str, run_index: dict[str, dict[str, Any]]) -> list[str]:
    alias = _RUN_SOURCE_ALIASES.get(name)
    if alias is None:
        return [name] if name in run_index else []
    target, is_prefix = alias
    if not is_prefix:
        return [target] if target in run_index else []
    return [source_id for source_id in run_index if source_id.startswith(target)]


def _alias_run_status(name: str, run_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [run_index[source_id] for source_id in _alias_source_ids(name, run_index)]
    if not candidates:
        return None
    # Most recent run wins (None finished_at sorts last).
    return max(candidates, key=lambda run: (run.get("finished_at") is not None, run.get("finished_at")))


def _alias_rate_limited(name: str, rate_limited: set[str]) -> bool:
    alias = _RUN_SOURCE_ALIASES.get(name)
    if alias is None:
        return False
    target, is_prefix = alias
    if is_prefix:
        return any(source_id.startswith(target) for source_id in rate_limited)
    return target in rate_limited


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
