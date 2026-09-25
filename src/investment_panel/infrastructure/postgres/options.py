"""PostgreSQL authority helpers shared by option-chain collectors."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository


def option_universe(config: AppConfig, *, limit: int) -> list[str]:
    repository = IngestionRepository(runtime_for_config(config))
    configured = list(config.watchlist)
    return repository.option_universe(configured)[:limit]


def incremental_option_symbols(
    config: AppConfig,
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


def active_paper_contracts(config: AppConfig, source_id: str) -> list[dict[str, Any]]:
    """Keep immutable paper ticket legs in the collector's sampling universe."""
    with runtime_for_config(config).read() as connection:
        return [dict(row) for row in connection.execute(
            """WITH active AS (
                   SELECT leg.contract_id AS contract_id
                   FROM app.paper_order paper
                   JOIN app.paper_order_leg leg ON leg.paper_order_id = paper.id
                   WHERE paper.status NOT IN ('exited', 'invalidated', 'unfilled', 'rejected', 'unmeasurable')
                   UNION
                   SELECT CASE WHEN leg->>'contract_id' ~ '^[0-9]{1,18}$' THEN (leg->>'contract_id')::bigint END
                   FROM analysis.shadow_trade shadow
                   CROSS JOIN LATERAL jsonb_array_elements(CASE
                       WHEN jsonb_typeof(shadow.metrics->'ticket'->'legs') = 'array'
                       THEN shadow.metrics->'ticket'->'legs' ELSE '[]'::jsonb END) leg
                   WHERE shadow.source_kind = 'options_paper_experiment'
                     AND shadow.status IN ('pending', 'entered') AND shadow.metrics->>'source_id' = %s
               ), recent_attempts AS MATERIALIZED (
                   SELECT summary, finished_at FROM ops.job_run
                   WHERE job_name = 'refresh_paper_quotes' AND finished_at <= now()
                     AND summary->>'source_id' = %s
                   ORDER BY finished_at DESC LIMIT 100
               ), attempts AS (
                   SELECT symbol, max(finished_at) AS last_attempt_at FROM recent_attempts
                   CROSS JOIN LATERAL jsonb_array_elements_text(CASE
                       WHEN jsonb_typeof(summary->'symbols_attempted') = 'array'
                       THEN summary->'symbols_attempted' ELSE '[]'::jsonb END) AS attempted(symbol)
                   GROUP BY symbol
               )
               SELECT instrument.symbol, contract.expiration::text AS expiration,
                      contract.id AS contract_id, contract.option_type, contract.strike::double precision AS strike,
                      contract.provider_symbols->>%s AS provider_instrument_id, contract.deliverable_key,
                      contract.multiplier, contract.style, contract.settlement, contract.standard_contract_verified
               FROM active
               JOIN catalog.option_contract contract ON contract.id = active.contract_id
               JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
               LEFT JOIN attempts ON attempts.symbol = instrument.symbol
               LEFT JOIN LATERAL (
                   SELECT max(quote.observed_at) AS observed_at FROM raw.option_quote quote
                   JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
                   WHERE quote.contract_id = contract.id AND quote.available_at <= now() AND quote.observed_at <= now()
                     AND snapshot.source_id = %s AND snapshot.capture_state = 'complete'
               ) latest ON true
               WHERE contract.expiration >= (now() AT TIME ZONE 'America/New_York')::date
               ORDER BY attempts.last_attempt_at NULLS FIRST,
                        bool_or(latest.observed_at IS NULL) OVER (PARTITION BY instrument.symbol) DESC,
                        min(latest.observed_at) OVER (PARTITION BY instrument.symbol) NULLS FIRST,
                        instrument.symbol, contract.expiration, contract.option_type, contract.strike""",
            [source_id, source_id, source_id, source_id],
        ).fetchall()]


def persist_collected_option_chains(
    config: AppConfig,
    source_id: str,
    collected: dict[str, Any],
    *,
    universe: str = "owned+watchlist",
) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    register_option_source(repository, source_id, capabilities={"option_quotes": True})
    observed_at = _coerce_observed_at(collected.get("observed_at"))
    flattened = [
        {"underlying_symbol": symbol, **row}
        for symbol, rows in (collected.get("rows") or {}).items()
        for row in rows
    ]
    with repository.run(source_id, "option_quotes", started_at=observed_at) as run:
        quote_count = repository.store_quotes(run.id, source_id, collected.get("quotes") or [])
        snapshot = repository.store_option_snapshot(
            run.id,
            source_id=source_id,
            observed_at=observed_at,
            market_session=_market_session(observed_at),
            universe=universe,
            rows=flattened,
            completeness=_completeness(collected),
        )
        coverage: dict[str, Any] = {}
        if "required_contracts" in collected:
            from investment_panel.infrastructure.postgres.paper_quote_coverage import reconcile_capture
            coverage = reconcile_capture(runtime, snapshot_id=snapshot["snapshot_id"],
                                         requested=collected["required_contracts"], received_at=observed_at,
                                         diagnostics=collected.get("contract_diagnostics") or [])
        errors = list(dict.fromkeys([*(collected.get("errors") or []), *coverage.get("coverage_errors", [])]))
        run.finish(
            "partial" if errors else "succeeded",
            item_count=len(flattened),
            instrument_count=len(collected.get("rows") or {}),
            failure_detail="; ".join(map(str, errors[:25])) or None,
            summary={
                "quote_count": quote_count,
                "market_data": collected.get("market_data"),
                "symbols_requested": list(collected.get("symbols_requested") or (collected.get("rows") or {}).keys()),
                "errors": errors[:100],
                **coverage,
            },
        )
    return {**snapshot, "quote_count": quote_count, "run_id": str(run.id), **coverage}


def register_option_source(
    repository: IngestionRepository,
    source_id: str,
    *,
    capabilities: dict[str, Any],
) -> None:
    """Reuse the provider identity across sampled, history, and equity collectors."""
    with repository.runtime.read() as connection:
        existing = connection.execute(
            "SELECT family, kind, origin, capabilities FROM ingest.source WHERE id = %s",
            [source_id],
        ).fetchone()
    broker = source_id in {"robinhood", "ibkr"}
    repository.register_source(
        source_id,
        name=source_id.upper(),
        family=existing["family"] if existing else ("broker" if broker else "market_data"),
        kind=existing["kind"] if existing else ("market_data" if broker else "option_chain"),
        origin=existing["origin"] if existing else None,
        capabilities={**(existing["capabilities"] or {}), **capabilities} if existing else capabilities,
    )


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


market_session = _market_session
