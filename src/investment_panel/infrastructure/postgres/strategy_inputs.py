"""Resolve the bounded, point-in-time inputs required by strategy revisions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Sequence

from investment_panel.domain.strategies.catalog import StrategySpec, union_input_requirements
from investment_panel.infrastructure.postgres.confirmed_daily_prices import (
    completed_trading_dates,
    confirmed_daily_bars,
)


def load_strategy_inputs(
    connection: Any,
    specs: Sequence[StrategySpec],
    *,
    as_of: datetime,
    symbols: Sequence[str] | None = None,
    benchmark_symbols: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load one exact input snapshot per symbol for the resolved definitions."""

    reference = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    requirements = union_input_requirements(specs)
    requested_symbols = tuple(sorted({str(item).upper() for item in symbols or () if str(item).strip()}))
    benchmark_names = tuple(sorted({str(item).upper() for item in benchmark_symbols or () if str(item).strip()}))
    rows = connection.execute(
        """SELECT id, upper(symbol) AS symbol
            FROM catalog.instrument
            WHERE (%s::text[] IS NULL OR upper(symbol) = ANY(%s::text[]))
              AND created_at <= %s
            ORDER BY symbol LIMIT 10000""",
        [list(requested_symbols) or None, list(requested_symbols) or None, reference],
    ).fetchall()
    benchmark_rows = connection.execute(
        """SELECT id, upper(symbol) AS symbol
            FROM catalog.instrument
            WHERE (%s::text[] IS NULL OR upper(symbol) = ANY(%s::text[]))
              AND created_at <= %s
            ORDER BY symbol LIMIT 256""",
        [list(benchmark_names) or None, list(benchmark_names) or None, reference],
    ).fetchall() if benchmark_names else []

    price_requirement = next((item for item in requirements if item.dataset == "confirmed_daily_bars"), None)
    required_dates = (
        tuple(reversed(completed_trading_dates(reference, count=price_requirement.sessions)))
        if price_requirement and price_requirement.sessions else ()
    )
    ids = [int(row["id"]) for row in rows]
    benchmark_ids = [int(row["id"]) for row in benchmark_rows]
    bars = confirmed_daily_bars(
        connection,
        [*ids, *benchmark_ids],
        as_of=reference,
        max_bars=len(required_dates) or None,
        trading_dates=required_dates if required_dates else None,
        require_session_close=bool(required_dates),
        require_point_in_time_source_state=True,
    ) if price_requirement else {}

    benchmark = _benchmark_input(benchmark_rows, bars, required_dates)
    event_by_symbol = _load_events(connection, [str(row["symbol"]) for row in rows], reference) \
        if any(item.dataset == "market_event" for item in requirements) else {}
    option_by_symbol = _load_option_inputs(connection, [dict(row) for row in rows], reference) \
        if any(item.dataset == "option_snapshot" for item in requirements) else {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        instrument_id = int(row["id"])
        symbol = str(row["symbol"])
        selected = bars.get(instrument_id, [])
        result[symbol] = {
            "input_cutoff": reference.isoformat(),
            "input_snapshot_identity": f"instrument:{instrument_id}:strategy-inputs:{reference.isoformat()}",
            "required_trading_dates": tuple(item.isoformat() for item in required_dates),
            "calendar_policy": "us_equity_completed_sessions" if required_dates else "not_applicable",
            "daily_bars": [
                {**bar, "status": "confirmed", "confirmed": True, "disabled": False}
                for bar in selected
            ],
            "evidence_refs": tuple(
                f"{bar.get('fact_table', 'raw.price_bar')}:{bar.get('fact_id')}"
                for bar in selected if bar.get("fact_id") is not None
            ),
            "source_versions": {"confirmed_daily_bars": "postgresql.point_in_time.v1"},
        }
        if benchmark:
            result[symbol].update(benchmark)
        if symbol in event_by_symbol:
            result[symbol]["event"] = event_by_symbol[symbol]
        if symbol in option_by_symbol:
            option_inputs = option_by_symbol[symbol]
            result[symbol].update(option_inputs)
            result[symbol]["evidence_refs"] = tuple({
                *result[symbol]["evidence_refs"],
                *option_inputs.get("option_evidence_refs", ()),
            })
            result[symbol]["source_versions"].update({
                "option_snapshot": "postgresql.point_in_time.v1",
            })
    return result


def _benchmark_input(
    rows: Sequence[Any],
    bars: dict[int, list[dict[str, Any]]],
    required_dates: Sequence[date],
) -> dict[str, Any]:
    if not rows:
        return {}
    selected = bars.get(int(rows[0]["id"]), [])
    by_date = {bar.get("trading_date"): bar for bar in selected}
    return {
        "benchmark_symbol": str(rows[0]["symbol"]),
        "benchmark_closes": [by_date.get(day, {}).get("close") for day in required_dates],
        "benchmark_close_dates": [day.isoformat() for day in required_dates],
        "benchmark_evidence_refs": tuple(
            f"{bar.get('fact_table', 'raw.price_bar')}:{bar.get('fact_id')}"
            for bar in selected if bar.get("fact_id") is not None
        ),
    }


def _load_events(connection: Any, symbols: Sequence[str], reference: datetime) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    rows = connection.execute(
        """SELECT DISTINCT ON (instrument.id) upper(instrument.symbol) AS symbol,
                          version.id AS observation_id,
                          CASE WHEN version.details->>'actual' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                               THEN (version.details->>'actual')::double precision END AS actual,
                          CASE WHEN version.details->>'consensus' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                               THEN (version.details->>'consensus')::double precision END AS consensus,
                          version.starts_at AS release_at, version.starts_at AS observed_at,
                          version.available_at, version.verification_status AS status,
                          version.source_id, version.ingest_run_id::text AS source_version
             FROM raw.market_event_version version
             JOIN catalog.instrument instrument ON instrument.id = version.instrument_id
             JOIN ingest.run ingest_run ON ingest_run.id = version.ingest_run_id
             JOIN ingest.source source ON source.id = version.source_id
             JOIN LATERAL (
                 SELECT history.enabled, history.operational_state
                   FROM ingest.source_lifecycle_history history
                  WHERE history.source_id = source.id AND history.effective_at <= %s
                  ORDER BY history.effective_at DESC, history.id DESC LIMIT 1
             ) lifecycle ON lifecycle.enabled AND lifecycle.operational_state = 'active'
            WHERE upper(instrument.symbol) = ANY(%s::text[])
              AND version.starts_at <= %s AND version.available_at <= %s
              AND ingest_run.status IN ('succeeded', 'partial')
              AND ingest_run.finished_at IS NOT NULL AND ingest_run.finished_at <= %s
              AND source.created_at <= %s
              AND lower(coalesce(version.verification_status, '')) IN ('confirmed', 'verified', 'available')
            ORDER BY instrument.id, version.available_at DESC, version.id DESC""",
        [reference, list(symbols), reference, reference, reference, reference],
    ).fetchall()
    return {
        str(row["symbol"]): {
            "status": "confirmed" if str(row.get("status", "")).upper() in {"AVAILABLE", "CONFIRMED", "VERIFIED"} else "unavailable",
            "confirmed": str(row.get("status", "")).upper() in {"AVAILABLE", "CONFIRMED", "VERIFIED"},
            "disabled": False,
            "actual": row.get("actual"),
            "consensus": row.get("consensus"),
            "release_at": row.get("release_at"),
            "observed_at": row.get("observed_at"),
            "available_at": row.get("available_at"),
            "source_id": row.get("source_id"),
            "source_version": row.get("source_version"),
            "observation_id": row.get("observation_id"),
        }
        for row in rows
    }


def _load_option_inputs(
    connection: Any, instruments: Sequence[dict[str, Any]], reference: datetime,
) -> dict[str, dict[str, Any]]:
    """Resolve the latest point-in-time option facts through their source runs."""

    symbols = [str(row["symbol"]).upper() for row in instruments]
    if not symbols:
        return {}
    rows = connection.execute(
        """
        WITH candidates AS (
            SELECT instrument.symbol, instrument.id AS instrument_id,
                   snapshot.id AS snapshot_id, snapshot.source_id,
                   snapshot.observed_at, snapshot.capture_state, snapshot.contract_count,
                   snapshot.collection_profile,
                   snapshot.completeness, ingest_run.finished_at AS available_at,
                   row_number() OVER (
                       PARTITION BY instrument.id
                       ORDER BY snapshot.observed_at DESC, snapshot.id DESC
                   ) AS rank
              FROM catalog.instrument instrument
              JOIN raw.option_snapshot snapshot
                ON upper(snapshot.history_symbol) = instrument.symbol
              JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
             JOIN ingest.source source ON source.id = snapshot.source_id
             JOIN LATERAL (
                 SELECT history.enabled, history.operational_state
                   FROM ingest.source_lifecycle_history history
                  WHERE history.source_id = source.id AND history.effective_at <= %s
                  ORDER BY history.effective_at DESC, history.id DESC LIMIT 1
             ) lifecycle ON lifecycle.enabled AND lifecycle.operational_state = 'active'
             WHERE instrument.symbol = ANY(%s::text[])
               AND snapshot.observed_at <= %s
               AND ingest_run.status IN ('succeeded', 'partial')
               AND ingest_run.finished_at IS NOT NULL
               AND ingest_run.finished_at <= %s
               AND source.created_at <= %s
               AND snapshot.collection_profile = 'history_full'
               AND coalesce(snapshot.completeness, 0) >= 0.98
        ), latest AS (
            SELECT * FROM candidates WHERE rank = 1
        )
        SELECT latest.*,
               coalesce(quotes.quote_count, 0) AS quote_count,
               coalesce(quotes.oi_volume_count, 0) AS oi_volume_count,
               dividend.observed_at AS dividend_observed_at,
               dividend.available_at AS dividend_available_at,
               dividend.id AS dividend_fact_id
          FROM latest
          LEFT JOIN LATERAL (
              SELECT count(*) AS quote_count,
                     count(*) FILTER (
                         WHERE quote.open_interest IS NOT NULL OR quote.volume IS NOT NULL
                     ) AS oi_volume_count
                FROM raw.option_quote quote
               WHERE quote.snapshot_id = latest.snapshot_id
                 AND quote.available_at <= %s
                 AND quote.observed_at <= %s
          ) quotes ON true
          LEFT JOIN LATERAL (
              SELECT observation.id, observation.observed_at, ingest_run.finished_at AS available_at
              FROM raw.fundamental_observation observation
              JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
              JOIN ingest.source source ON source.id = observation.source_id
              JOIN LATERAL (
                  SELECT history.enabled, history.operational_state
                    FROM ingest.source_lifecycle_history history
                   WHERE history.source_id = source.id AND history.effective_at <= %s
                   ORDER BY history.effective_at DESC, history.id DESC LIMIT 1
              ) dividend_lifecycle ON dividend_lifecycle.enabled AND dividend_lifecycle.operational_state = 'active'
               WHERE observation.instrument_id = latest.instrument_id
                 AND observation.metric_set ILIKE '%%dividend%%'
                 AND observation.observed_at <= %s
                 AND ingest_run.status IN ('succeeded', 'partial')
                AND ingest_run.finished_at IS NOT NULL
                AND ingest_run.finished_at <= %s
                AND source.created_at <= %s
               ORDER BY observation.observed_at DESC, observation.id DESC
               LIMIT 1
          ) dividend ON true
        """,
        [
            reference, symbols, reference, reference, reference, reference,
            reference, reference, reference, reference, reference,
        ],
    ).fetchall()
    loaded: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        symbol = str(row["symbol"]).upper()
        observed_at = row.get("observed_at")
        available_at = row.get("available_at")
        chain_confirmed = (
            row.get("collection_profile") == "history_full"
            and row.get("capture_state") == "complete"
            and int(row.get("contract_count") or 0) > 0
            and float(row.get("completeness") or 0.0) >= 0.98
        )
        quote_confirmed = int(row.get("oi_volume_count") or 0) > 0
        dividend_confirmed = row.get("dividend_fact_id") is not None
        loaded[symbol] = {
            "full_chain_state": _option_state(chain_confirmed, observed_at, available_at, "option_chain"),
            "oi_volume_state": _option_state(quote_confirmed, observed_at, available_at, "option_quotes"),
            "dividend_state": _option_state(
                dividend_confirmed, row.get("dividend_observed_at"), row.get("dividend_available_at"), "dividend_facts",
            ),
            "quote_quality": 1.0 if int(row.get("quote_count") or 0) > 0 else None,
            "fill_model_proven": False,
            "option_evidence_refs": tuple(filter(None, (
                f"raw.option_snapshot:{row.get('snapshot_id')}",
                f"raw.fundamental_observation:{row.get('dividend_fact_id')}" if dividend_confirmed else None,
            ))),
        }
    for symbol in symbols:
        loaded.setdefault(symbol, {
            "full_chain_state": _option_state(False, None, None, "option_chain"),
            "oi_volume_state": _option_state(False, None, None, "option_quotes"),
            "dividend_state": _option_state(False, None, None, "dividend_facts"),
            "quote_quality": None,
            "fill_model_proven": False,
            "option_evidence_refs": (),
        })
    return loaded


def _option_state(confirmed: bool, observed_at: Any, available_at: Any, dataset: str) -> dict[str, Any]:
    return {
        "status": "confirmed" if confirmed else "unavailable",
        "confirmed": confirmed,
        "disabled": False,
        "observed_at": observed_at,
        "available_at": available_at,
        "dataset": dataset,
    }


__all__ = ["load_strategy_inputs"]
