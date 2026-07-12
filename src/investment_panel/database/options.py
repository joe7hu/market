"""PostgreSQL authority helpers shared by option-chain collectors."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository


def option_universe(config: Any, *, limit: int) -> list[str]:
    repository = IngestionRepository(runtime_for_config(config))
    configured = list(getattr(config, "watchlist", None) or [])
    return repository.option_universe(configured)[:limit]


def incremental_option_symbols(
    config: Any,
    source_id: str,
    symbols: Sequence[str],
    *,
    limit: int,
    stale_before: datetime,
) -> list[str]:
    normalized = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    latest = IngestionRepository(runtime_for_config(config)).latest_option_snapshot_by_symbol(source_id, normalized)
    ranked: list[tuple[int, datetime, int, str]] = []
    for index, symbol in enumerate(normalized):
        observed_at = latest.get(symbol)
        if observed_at is not None and observed_at >= stale_before:
            continue
        ranked.append((0 if observed_at is not None else 1, observed_at or datetime.min.replace(tzinfo=UTC), index, symbol))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [symbol for _bucket, _observed, _index, symbol in ranked[:limit]]


def persist_collected_option_chains(
    config: Any,
    source_id: str,
    collected: dict[str, Any],
) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        source_id,
        name=source_id.upper(),
        family="broker" if source_id in {"robinhood", "ibkr"} else "market_data",
        kind="option_chain",
        capabilities={"option_quotes": True},
    )
    observed_at = _coerce_observed_at(collected.get("observed_at"))
    flattened = [
        {"underlying_symbol": symbol, **row}
        for symbol, rows in (collected.get("rows") or {}).items()
        for row in rows
    ]
    run_id = repository.start_run(source_id, "option_quotes", started_at=observed_at)
    try:
        quote_count = repository.store_quotes(run_id, source_id, collected.get("quotes") or [])
        snapshot = repository.store_option_snapshot(
            run_id,
            source_id=source_id,
            observed_at=observed_at,
            market_session=_market_session(observed_at),
            universe="owned+watchlist",
            rows=flattened,
            completeness=_completeness(collected),
        )
        errors = list(collected.get("errors") or [])
        repository.finish_run(
            run_id,
            "partial" if errors else "succeeded",
            item_count=len(flattened),
            instrument_count=len(collected.get("rows") or {}),
            failure_detail="; ".join(map(str, errors[:25])) or None,
            summary={
                "quote_count": quote_count,
                "market_data": collected.get("market_data"),
                "symbols_requested": list(collected.get("symbols_requested") or (collected.get("rows") or {}).keys()),
                "errors": list(collected.get("errors") or [])[:25],
            },
        )
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        raise
    return {**snapshot, "quote_count": quote_count, "run_id": str(run_id)}


def _coerce_observed_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or datetime.now(UTC).isoformat()).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _market_session(observed_at: datetime) -> str:
    local = observed_at.astimezone(ZoneInfo("America/New_York"))
    if local.weekday() >= 5:
        return "closed"
    clock = local.time().replace(tzinfo=None)
    if time(4) <= clock < time(9, 30):
        return "premarket"
    # Listed equity options quote through 16:15 ET; a weekend pull reports the
    # provider's last quote timestamp and must remain eligible as the last RTH window.
    if time(9, 30) <= clock < time(16, 15):
        return "regular"
    if time(16) <= clock < time(20):
        return "afterhours"
    return "closed"


def _completeness(collected: dict[str, Any]) -> float | None:
    total = sum(len(rows) for rows in (collected.get("rows") or {}).values())
    if not total:
        return None
    quoted = sum(
        1
        for rows in (collected.get("rows") or {}).values()
        for row in rows
        if (row.get("bid") or 0) > 0 or (row.get("ask") or 0) > 0
    )
    return quoted / total
