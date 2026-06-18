"""Discovered-universe accumulation and ranking."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.instruments import infer_asset_class, normalize_symbol

from investment_panel.core.decision.constants import STATIC_SOURCES, SYMBOL_RE
from investment_panel.core.decision.coerce import parse_dt, recency_points
from investment_panel.core.decision.freshness import eligibility_detail


class DiscoveredUniverseAccumulator:
    """Collect source evidence for symbols, then emit ranked universe rows."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._now = now or datetime.now(UTC)

    def add(
        self,
        symbol: Any,
        source: str,
        reason: str,
        observed_at: Any = None,
        name: str | None = None,
        asset_class: str | None = None,
        strength: float = 1.0,
        event_at: Any = None,
    ) -> None:
        normalized = normalize_symbol(str(symbol or ""))
        if not normalized or not SYMBOL_RE.match(normalized):
            return
        row = self._items.setdefault(
            normalized,
            {
                "symbol": normalized,
                "name": name or normalized,
                "asset_class": asset_class or infer_asset_class(normalized),
                "reasons": set(),
                "source_counts": defaultdict(int),
                "latest_source_timestamp": None,
                "latest_observed_at": None,
                "next_event_at": None,
                "evidence_score": 0.0,
                "liquidity_score": 0.0,
            },
        )
        if name and row["name"] == normalized:
            row["name"] = name
        if asset_class and not row.get("asset_class"):
            row["asset_class"] = asset_class
        row["reasons"].add(reason)
        row["source_counts"][source] += 1
        row["evidence_score"] += strength
        observed = parse_dt(observed_at)
        if observed and (row["latest_observed_at"] is None or observed > row["latest_observed_at"]):
            row["latest_observed_at"] = observed
            row["latest_source_timestamp"] = observed
        event = parse_dt(event_at)
        if event and event >= self._now and (row["next_event_at"] is None or event < row["next_event_at"]):
            row["next_event_at"] = event

    def rows(self, liquidity_by_symbol: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for symbol, row in self._items.items():
            counts = dict(row["source_counts"])
            latest = row["latest_observed_at"]
            next_event = row["next_event_at"]
            liq = liquidity_by_symbol.get(symbol, {})
            dollar_volume = float(liq.get("avg_dollar_volume") or 0)
            liquidity_score = min(100.0, dollar_volume / 1_000_000)
            total_source_count = sum(counts.values())
            source_count = sum(value for key, value in counts.items() if key not in STATIC_SOURCES)
            recency_score = recency_points(latest) if latest else 0.0
            tradable_asset = row.get("asset_class") in {"equity", "etf", "crypto"}
            eligibility_status = "eligible" if tradable_asset and source_count > 0 else "source_thin" if tradable_asset else "ineligible"
            evidence_score = float(row["evidence_score"]) + min(source_count, 10)
            discovery_score = evidence_score + liquidity_score * 0.2 + recency_score * 0.25
            output.append(
                {
                    "symbol": symbol,
                    "name": row["name"],
                    "asset_class": row["asset_class"],
                    "inclusion_reasons": sorted(row["reasons"]),
                    "source_counts": counts,
                    "source_count": source_count,
                    "total_source_count": total_source_count,
                    "latest_source_timestamp": latest,
                    "latest_source_at": latest,
                    "latest_observed_at": latest,
                    "next_event_at": next_event,
                    "eligibility_status": eligibility_status,
                    "eligibility_detail": eligibility_detail(eligibility_status),
                    "evidence_score": round(evidence_score, 2),
                    "discovery_score": round(discovery_score, 2),
                    "liquidity_score": round(liquidity_score, 2),
                    "recency_score": round(recency_score, 2),
                }
            )
        output.sort(
            key=lambda item: (
                item["eligibility_status"] == "eligible",
                item["recency_score"],
                item["source_count"],
                item["evidence_score"],
                item["liquidity_score"],
            ),
            reverse=True,
        )
        eligible_rank = 0
        for index, row in enumerate(output, start=1):
            if row["eligibility_status"] == "eligible":
                eligible_rank += 1
                row["universe_rank"] = eligible_rank
                row["decision_universe_member"] = eligible_rank <= 250
            else:
                row["universe_rank"] = index
                row["decision_universe_member"] = False
            row["updated_at"] = self._now
        return output
