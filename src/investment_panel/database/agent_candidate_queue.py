"""Read the current unique-symbol option candidates for agent work."""

from __future__ import annotations

from typing import Any

from investment_panel.database.analysis import current_option_publication_rows
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def current_candidate_payloads(runtime: DatabaseRuntime, *, limit: int) -> list[dict[str, Any]]:
    with runtime.read(JOB_PROFILE) as connection:
        rows = current_option_publication_rows(
            connection, scope="options-radar", model_name="option_radar_opportunity",
        )
    ranked: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row["payload"] or {})
        symbol = str(payload.get("ticker") or payload.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        current = ranked.get(symbol)
        research_rank = _integer(payload.get("research_rank"))
        rank_key = (
            research_rank is None,
            research_rank if research_rank is not None else 0,
            int(row.get("rank") or 0),
            str(row.get("stable_key") or ""),
        )
        if current is None or rank_key < current["rank_key"]:
            ranked[symbol] = {"payload": payload, "rank_key": rank_key}
    ordered = sorted(ranked.values(), key=lambda item: item["rank_key"])
    return [dict(item["payload"]) for item in ordered[:max(0, int(limit))]]


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
