"""PostgreSQL-native broad-market publication from confirmed daily prices."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
import json
import math
from statistics import median
from typing import Any

from investment_panel.domain.decision import (
    InputLineage,
    MARKET_HORIZONS,
    MARKET_TZ,
    is_us_market_day,
)
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.infrastructure.postgres.fundamental_history import hydrate_history
from investment_panel.infrastructure.postgres.portfolio_ledger import replay_portfolio_at
from investment_panel.infrastructure.postgres.phase2 import Phase2Repository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.domain.market.publication import (
    MarketPublicationInputs,
    _as_date,
    _as_utc,
    _CORPORATE_BENCHMARK_KEY,
    _CORPORATE_HORIZON_BLOCKERS,  # noqa: F401 - re-exported for existing domain contract checks
    _CORPORATE_METRIC_SET,
    _CORPORATE_SOURCE_ID,
    _HORIZON_LOOKBACK,
    _MARKET_STALE_AFTER,
    _corporate_lineage,
    _number,
    build_market_publication,
    json_dumps,
    market_dimension_v2_fields,
)


_CRYPTO_BENCHMARK_KEY = "market-crypto-majors"
_CRYPTO_SOURCE_ID = "daily-market-prices"
_CRYPTO_SOURCE_FRESHNESS_SECONDS = 3600
_CRYPTO_SYMBOLS = ("BTC-USD", "ETH-USD", "SOL-USD")
_MAX_EVENT_RISK_SCHEDULE = 8

__all__ = [
    "MarketPublicationInputs",
    "build_market_publication",
    "load_market_inputs",
    "market_dimension_v2_fields",
    "persist_market_publication",
]




def load_market_inputs(
    runtime: DatabaseRuntime,
    *,
    as_of: datetime,
    benchmark_symbols: list[str] | tuple[str, ...] | None = None,
    configured_watchlist: list[dict[str, Any]] | None = None,
    configured_watchlist_as_of: datetime | None = None,
) -> MarketPublicationInputs:
    if as_of.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    as_of = as_of.astimezone(UTC)
    configured_at = _as_utc(configured_watchlist_as_of)
    if configured_watchlist_as_of is not None and configured_at is None:
        raise ValueError("configured watchlist timestamp must be timezone-aware")
    if configured_at is not None and configured_at > as_of:
        raise ValueError("configured watchlist timestamp is after market cutoff")
    # Instrument and source updated_at values are maintenance timestamps touched by
    # idempotent registration. Membership timestamps are semantic and safe to gate.
    with runtime.read() as connection:
        owned_instrument_ids = (
            {
                int(row["instrument_id"])
                for row in replay_portfolio_at(None, as_of, connection=connection)["positions"]
            }
            if benchmark_symbols is None
            else set()
        )
        instrument_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT instrument.id, instrument.symbol, instrument.name, instrument.asset_class,
                       EXISTS (
                           SELECT 1 FROM app.watchlist_item watchlist
                           WHERE watchlist.instrument_id = instrument.id
                             AND watchlist.created_at <= %s
                             AND watchlist.updated_at <= %s
                             AND watchlist.watch_state <> 'excluded'
                       ) AS persisted_watchlist_member,
                       EXISTS (
                           SELECT 1 FROM app.watchlist_item watchlist
                           WHERE watchlist.instrument_id = instrument.id
                             AND watchlist.created_at <= %s
                             AND watchlist.updated_at <= %s
                             AND watchlist.watch_state = 'excluded'
                       ) AS persisted_watchlist_excluded
                FROM catalog.instrument instrument
                WHERE instrument.created_at <= %s
                  AND (instrument.delisted_at IS NULL OR instrument.delisted_at > %s)
                ORDER BY symbol
                """,
                [as_of, as_of, as_of, as_of, as_of, as_of],
            ).fetchall()
        ]
        explicit_benchmark = (
            {str(symbol).strip().upper() for symbol in benchmark_symbols if str(symbol).strip()}
            if benchmark_symbols is not None
            else None
        )
        configured_benchmark = (
            _configured_benchmark_symbols(
                configured_watchlist,
                as_of=as_of,
                configured_at=configured_at,
            )
            if explicit_benchmark is None
            else set()
        )
        for row in instrument_rows:
            symbol = str(row.get("symbol") or "").upper()
            row["market_benchmark_member"] = (
                symbol in explicit_benchmark
                if explicit_benchmark is not None
                else (
                    int(row["id"]) in owned_instrument_ids
                    or (
                        not bool(row.get("persisted_watchlist_excluded"))
                        and (
                            bool(row.get("persisted_watchlist_member"))
                            or symbol in configured_benchmark
                        )
                    )
                )
            )
        bars_by_id = confirmed_daily_bars(
            connection,
            [int(row["id"]) for row in instrument_rows],
            as_of=as_of,
            max_bars=400,
        )
        price_rows: list[dict[str, Any]] = []
        metadata = {int(row["id"]): row for row in instrument_rows}
        for instrument_id, rows in bars_by_id.items():
            instrument = metadata.get(int(instrument_id))
            if instrument is None:
                continue
            for row in rows:
                price_rows.append({
                    **dict(row),
                    "instrument_id": int(instrument_id),
                    "symbol": instrument["symbol"],
                    "name": instrument["name"],
                    "asset_class": instrument["asset_class"],
                    "price": row["close"],
                })
        valuation_rows = [
            {
                **dict(row),
                "values": hydrate_history(
                    dict(row.get("values") or {}),
                    archive_uri=row.get("payload_archive_uri"),
                    archive_sha256=row.get("payload_sha256"),
                ),
            }
            for row in connection.execute(
                """
                SELECT DISTINCT ON (observation.metric_set)
                       instrument.symbol, observation.period_end, observation.observed_at,
                       observation.values, observation.source_id, observation.metric_set,
                       ingest_run.id::text AS ingest_run_id,
                       ingest_run.finished_at AS available_at,
                       payload.archive_uri AS payload_archive_uri, payload.sha256 AS payload_sha256
                FROM raw.fundamental_observation observation
                JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
                JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
                JOIN ingest.source source
                  ON source.id = observation.source_id
                 AND source.enabled
                 AND source.operational_state = 'active'
                 AND source.created_at <= %s
                LEFT JOIN ingest.payload payload ON payload.id = observation.payload_id
                WHERE (observation.metric_set = 'market_valuation'
                   OR observation.metric_set LIKE 'market_valuation:%%')
                  AND observation.observed_at <= %s
                  AND (observation.filed_at IS NULL OR observation.filed_at <= %s)
                  AND instrument.created_at <= %s
                  AND ingest_run.status IN ('succeeded', 'partial')
                  AND ingest_run.finished_at IS NOT NULL
                  AND ingest_run.finished_at <= %s
                ORDER BY observation.metric_set, observation.observed_at DESC
                """,
                [as_of, as_of, as_of, as_of, as_of],
            ).fetchall()
        ]
        event_risk_evidence = _event_risk_evidence(connection, as_of)
        corporate_cycle_evidence = _corporate_cycle_evidence(connection, instrument_rows, as_of)
        crypto_volume_evidence = _crypto_volume_evidence(connection, instrument_rows, as_of)
        phase2_rows = [dict(row) for row in connection.execute(
            """SELECT observation.observation_id, observation.field_name, observation.dimension,
                      observation.asset_class, observation.source_id, observation.source_version,
                      observation.value, observation.unit, observation.ingest_run_id::text AS ingest_run_id,
                      observation.payload_id, observation.content_hash, observation.parent_snapshot_id,
                      observation.observed_at, observation.available_at, observation.publication_at,
                      observation.release_at, observation.vintage_at, observation.actual,
                      observation.consensus, observation.surprise, observation.revision,
                      observation.status, observation.confidence, observation.metadata,
                      lifecycle.enabled AS source_enabled, lifecycle.operational_state AS source_operational_state,
                      ingest_run.status AS ingest_status, ingest_run.finished_at AS ingest_finished_at
               FROM raw.market_observation observation
               JOIN ingest.source source ON source.id = observation.source_id
               JOIN LATERAL (
                   SELECT history.enabled, history.operational_state
                   FROM ingest.source_lifecycle_history history
                   WHERE history.source_id = source.id AND history.effective_at <= %s
                   ORDER BY history.effective_at DESC, history.id DESC LIMIT 1
               ) lifecycle ON lifecycle.enabled = true AND lifecycle.operational_state = 'active'
               JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
               WHERE observation.observed_at <= %s AND observation.available_at <= %s
                 AND ingest_run.status IN ('succeeded', 'partial')
                 AND ingest_run.finished_at IS NOT NULL AND ingest_run.finished_at <= %s
               ORDER BY observation.dimension, observation.observed_at, observation.observation_id
               LIMIT 500""",
            [as_of, as_of, as_of, as_of],
        ).fetchall()]
        phase2_source_rows = [dict(row) for row in connection.execute(
            """SELECT source.id AS source_id, lifecycle.enabled AS source_enabled,
                      lifecycle.operational_state AS source_operational_state,
                      source.capabilities->>'phase2_status' AS phase2_status
               FROM ingest.source source
               JOIN LATERAL (
                   SELECT history.enabled, history.operational_state
                   FROM ingest.source_lifecycle_history history
                   WHERE history.source_id = source.id AND history.effective_at <= %s
                   ORDER BY history.effective_at DESC, history.id DESC LIMIT 1
               ) lifecycle ON true
               WHERE source.family = 'phase2' AND source.created_at <= %s""",
            [as_of, as_of],
        ).fetchall()]
    return {
        "instrument_rows": instrument_rows,
        "bars_by_id": bars_by_id,
        "price_rows": price_rows,
        "valuation_rows": valuation_rows,
        "event_risk_evidence": event_risk_evidence,
        "corporate_cycle_evidence": corporate_cycle_evidence,
        "crypto_volume_evidence": crypto_volume_evidence,
        "phase2_rows": phase2_rows,
        "phase2_source_rows": phase2_source_rows,
    }




def persist_market_publication(
    runtime: DatabaseRuntime,
    *,
    as_of: datetime,
    draft: dict[str, Any],
) -> dict[str, Any]:
    assets = draft["assets"]
    drivers = draft["drivers"]
    references = draft["references"]
    snapshot = draft["snapshot"]
    coverage_rows = draft["coverage_rows"]
    volatility_evidence = draft["volatility_evidence"]
    corporate_cycle_evidence = draft["corporate_cycle_evidence"]
    crypto_volume_evidence = draft["crypto_volume_evidence"]
    input_lineage = draft["input_lineage"]
    grouped = draft["grouped"]
    price_rows = draft["price_rows"]
    valuation_rows = draft["valuation_rows"]
    phase2_posterior = draft["phase2_posterior"]
    phase2_coverage = draft["phase2_coverage"]
    phase2_scenarios = draft["phase2_scenarios"]
    analysis = AnalysisRepository(runtime)
    volatility_inputs = {
        horizon: {
            "available": evidence.get("available"),
            "blockers": evidence.get("blockers"),
            "counts": {
                key: evidence.get(key)
                for key in (
                    "eligible_member_count", "available_member_count", "missing_member_count",
                    "stale_member_count", "truncated_member_count", "duplicate_member_count",
                    "invalid_member_count",
                )
            },
            "return_window_trading_days": evidence.get("return_window_trading_days"),
            "minimum_history_trading_days": evidence.get("minimum_history_trading_days"),
            "realized_volatility": evidence.get("realized_volatility"),
            "lineage": [item.model_dump(mode="json") for item in evidence.get("lineage") or ()],
        }
        for horizon, evidence in volatility_evidence.items()
    }
    run_id = analysis.start_run(
        "market-environment",
        input_cutoff=as_of,
        code_version="postgres-market-v2",
        inputs={
            "price_bar_rows": len(price_rows),
            "symbols": sorted(grouped),
            "valuation_rows": len(valuation_rows),
            "source_lineage": [item.model_dump(mode="json") for item in input_lineage],
            "volatility_evidence": volatility_inputs,
            "corporate_cycle_evidence": _corporate_cycle_inputs(corporate_cycle_evidence),
            "crypto_volume_evidence": _crypto_volume_inputs(crypto_volume_evidence),
        },
        feature_versions={"market_environment": "v2"},
    )
    publication_id = analysis.publish(
        run_id,
        "market",
        {
            "market_environment_assets": assets,
            "market_environment_model": drivers,
            "market_valuation_reference_charts": references,
            "market_state_snapshot": [snapshot.model_dump(mode="json")],
            "coverage_matrix": coverage_rows,
        },
        validation={"confirmed_daily_price_source": True, "quote_fallback": False, "asset_count": len(assets)},
        complete_run_summary={
            "assets": len(assets), "drivers": len(drivers), "valuation_series": len(references),
            "snapshot_id": snapshot.snapshot_id,
        },
    )
    publication = analysis.publication_by_id("market", publication_id)
    if publication is None or publication.get("published_at") is None:
        raise RuntimeError("published MarketState is not visible in PostgreSQL")
    Phase2Repository(runtime).publish(phase2_posterior, phase2_coverage, phase2_scenarios)
    return {
        "status": "ok",
        "publication_id": str(publication_id),
        "published_at": publication["published_at"],
        "assets": len(assets),
        "drivers": len(drivers),
        "valuation_series": len(references),
        "snapshot_id": snapshot.snapshot_id,
        "coverage_rows": len(coverage_rows),
        "available_coverage_rows": sum(1 for row in coverage_rows if row.get("current_status") == "available"),
        "unavailable_coverage_rows": sum(1 for row in coverage_rows if row.get("current_status") != "available"),
    }


def _configured_benchmark_symbols(
    configured_watchlist: list[dict[str, Any]] | None,
    *,
    as_of: datetime,
    configured_at: datetime | None,
) -> set[str]:
    output: set[str] = set()
    for item in configured_watchlist or ():
        if str(item.get("watch_state") or "").strip().lower() == "excluded":
            continue
        created_at = _configured_membership_time(item, "created_at")
        updated_at = _configured_membership_time(item, "updated_at")
        if (
            ("created_at" in item and created_at is None)
            or ("updated_at" in item and updated_at is None)
            or (created_at is not None and created_at > as_of)
            or (updated_at is not None and updated_at > as_of)
            or (
                configured_at is None
                and created_at is None
                and updated_at is None
            )
        ):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol:
            output.add(symbol)
    return output


def _configured_membership_time(item: dict[str, Any], key: str) -> datetime | None:
    value = item.get(key)
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)














def _crypto_volume_evidence(
    connection: Any,
    instrument_rows: list[dict[str, Any]],
    cutoff: datetime,
) -> dict[str, dict[str, Any]]:
    instruments = {str(row.get("symbol")): row for row in instrument_rows}
    expected_instruments = {
        symbol: instruments.get(symbol) for symbol in _CRYPTO_SYMBOLS
    }
    missing_instruments = sum(row is None for row in expected_instruments.values())
    wrong_asset_class = sum(
        row is not None and str(row.get("asset_class") or "").lower() != "crypto"
        for row in expected_instruments.values()
    )
    crypto_instrument_ids = [
        int(row["id"])
        for row in expected_instruments.values()
        if row is not None and str(row.get("asset_class") or "").lower() == "crypto"
    ]
    crypto_bars = (
        confirmed_daily_bars(
            connection, crypto_instrument_ids, as_of=cutoff, max_bars=400,
        )
        if crypto_instrument_ids else {}
    )
    source = connection.execute(
        """
        SELECT enabled, operational_state, health_owner, freshness_seconds
        FROM ingest.source
        WHERE id = %s
          AND created_at <= %s
        """,
        [_CRYPTO_SOURCE_ID, cutoff],
    ).fetchone()
    source_blocker: str | None = None
    source_count_key = "invalid_member_count"
    freshness_seconds = 0
    latest_finished_at: datetime | None = None
    if source is None:
        source_blocker = "crypto_daily_trading_volume_source_unavailable"
    else:
        source = dict(source)
        freshness_seconds = int(source.get("freshness_seconds") or 0)
        if (
            not source.get("enabled")
            or source.get("operational_state") != "active"
            or source.get("health_owner") != "update_market_data"
            or freshness_seconds != _CRYPTO_SOURCE_FRESHNESS_SECONDS
        ):
            source_blocker = "crypto_daily_trading_volume_source_lifecycle_mismatch"
        else:
            run = connection.execute(
                """
                SELECT max(finished_at) AS finished_at
                FROM ingest.run
                WHERE source_id = %s
                  AND capability = 'price_bars'
                  AND status IN ('succeeded', 'partial')
                  AND finished_at IS NOT NULL
                  AND finished_at <= %s
                """,
                [_CRYPTO_SOURCE_ID, cutoff],
            ).fetchone()
            latest_finished_at = _as_utc(run["finished_at"]) if run and run["finished_at"] else None
            if latest_finished_at is None:
                source_blocker = "crypto_daily_trading_volume_source_run_unavailable"
            elif cutoff - latest_finished_at > timedelta(seconds=freshness_seconds):
                source_blocker = "crypto_daily_trading_volume_source_run_stale"
                source_count_key = "stale_member_count"
    result: dict[str, dict[str, Any]] = {}
    for horizon, lookback in _HORIZON_LOOKBACK.items():
        if lookback is None:
            result[horizon] = {
                "available": False,
                "status": "unavailable",
                "benchmark_key": _CRYPTO_BENCHMARK_KEY,
                "eligible_members": _CRYPTO_SYMBOLS,
                "eligible_member_count": len(_CRYPTO_SYMBOLS),
                "available_member_count": 0,
                "missing_member_count": 0,
                "stale_member_count": 0,
                "truncated_member_count": 0,
                "duplicate_member_count": 0,
                "invalid_member_count": 0,
                "wrong_currency_member_count": 0,
                "wrong_asset_class_member_count": 0,
                "wrong_source_member_count": 0,
                "expected_calendar_days": 0,
                "window_start": None,
                "window_end": None,
                "latest_aggregate_volume_usd": None,
                "median_aggregate_daily_volume_usd": None,
                "latest_to_horizon_median_ratio": None,
                "freshness_max_age_seconds": freshness_seconds or None,
                "freshness_latest_available_at": latest_finished_at,
                "lineage": (),
                "valid_rows": (),
                "blockers": ["crypto_liquidity_daily_volume_unsupported_for_intraday"],
                "data_requests": ["intraday_crypto_spread_depth_execution_data"],
            }
            continue

        expected = _completed_crypto_dates(cutoff, count=lookback)
        evidence: dict[str, Any] = {
            "available": False,
            "status": "unavailable",
            "benchmark_key": _CRYPTO_BENCHMARK_KEY,
            "eligible_members": _CRYPTO_SYMBOLS,
            "eligible_member_count": len(_CRYPTO_SYMBOLS),
            "available_member_count": 0,
            "missing_member_count": missing_instruments,
            "stale_member_count": 0,
            "truncated_member_count": 0,
            "duplicate_member_count": 0,
            "invalid_member_count": 0,
            "wrong_currency_member_count": 0,
            "wrong_asset_class_member_count": wrong_asset_class,
            "wrong_source_member_count": 0,
            "expected_calendar_days": lookback,
            "window_start": expected[-1],
            "window_end": expected[0],
            "latest_aggregate_volume_usd": None,
            "median_aggregate_daily_volume_usd": None,
            "latest_to_horizon_median_ratio": None,
            "freshness_max_age_seconds": freshness_seconds or None,
            "freshness_latest_available_at": latest_finished_at,
            "lineage": (),
            "valid_rows": (),
            "blockers": [],
            "data_requests": ["update_market_data"],
        }
        if source_blocker:
            evidence["blockers"] = [source_blocker]
            evidence[source_count_key] = len(_CRYPTO_SYMBOLS)
            result[horizon] = evidence
            continue

        valid_rows: list[dict[str, Any]] = []
        missing = stale = truncated = duplicate = invalid = wrong_currency = wrong_source = 0
        expected_set = set(expected)
        for symbol in _CRYPTO_SYMBOLS:
            member = expected_instruments[symbol]
            if member is None:
                continue
            if str(member.get("asset_class") or "").lower() != "crypto":
                continue
            all_rows = [dict(row) for row in crypto_bars.get(int(member["id"]), ())]
            if not all_rows:
                missing += 1
                continue
            source_rows = [row for row in all_rows if str(row.get("source_id")) == _CRYPTO_SOURCE_ID]
            if not source_rows:
                wrong_source += 1
                continue
            source_rows_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
            for row in source_rows:
                if row.get("trading_date") in expected_set:
                    source_rows_by_date[row["trading_date"]].append(row)
            relevant = []
            duplicate_version = False
            for trading_date in expected:
                candidates = source_rows_by_date.get(trading_date, [])
                if not candidates:
                    continue
                candidates.sort(
                    key=lambda row: (
                        _as_utc(row.get("available_at")) or datetime.min.replace(tzinfo=UTC),
                        _as_utc(row.get("confirmed_at")) or datetime.min.replace(tzinfo=UTC),
                        int(row.get("fact_id") or 0),
                    )
                )
                newest = candidates[-1]
                newest_key = (
                    _as_utc(newest.get("available_at")),
                    _as_utc(newest.get("confirmed_at")),
                )
                if sum(
                    (
                        _as_utc(row.get("available_at")),
                        _as_utc(row.get("confirmed_at")),
                    ) == newest_key
                    for row in candidates
                ) > 1:
                    duplicate_version = True
                relevant.append(newest)
            if not relevant:
                truncated += 1
                continue
            dates = [row.get("trading_date") for row in relevant]
            if duplicate_version or len(dates) != len(set(dates)):
                duplicate += 1
                continue
            if len(relevant) != len(expected) or set(dates) != expected_set:
                truncated += 1
                continue
            selected_by_date = {row["trading_date"]: row for row in relevant}
            selected = []
            for trading_date in expected:
                row = dict(selected_by_date[trading_date])
                row["symbol"] = symbol
                selected.append(row)
            observed = [_as_utc(row.get("observed_at")) for row in selected]
            available = [_as_utc(row.get("available_at")) for row in selected]
            confirmed = [_as_utc(row.get("confirmed_at")) for row in selected]
            if any(
                value is None or value > cutoff
                for values in (observed, available, confirmed)
                for value in values
            ):
                invalid += 1
                continue
            if any(
                value is None or cutoff - value > timedelta(seconds=freshness_seconds)
                for value in confirmed
            ):
                stale += 1
                continue
            latest_available = max(value for value in available if value is not None)
            if cutoff - latest_available > _MARKET_STALE_AFTER:
                stale += 1
                continue
            if any(str(row.get("currency") or "").upper() != "USD" for row in selected):
                wrong_currency += 1
                continue
            volumes = [_finite_number(row.get("volume")) for row in selected]
            if any(value is None or value <= 0 for value in volumes):
                invalid += 1
                continue
            valid_rows.extend(selected)

        evidence.update({
            "available_member_count": len(valid_rows) // lookback,
            "missing_member_count": missing_instruments + missing,
            "stale_member_count": stale,
            "truncated_member_count": truncated,
            "duplicate_member_count": duplicate,
            "invalid_member_count": invalid,
            "wrong_currency_member_count": wrong_currency,
            "wrong_asset_class_member_count": wrong_asset_class,
            "wrong_source_member_count": wrong_source,
        })
        quality_counts = (
            missing_instruments + missing,
            wrong_asset_class,
            stale,
            truncated,
            duplicate,
            invalid,
            wrong_currency,
            wrong_source,
        )
        if not any(quality_counts) and evidence["available_member_count"] == len(_CRYPTO_SYMBOLS):
            aggregates = {
                trading_date: sum(
                    _finite_number(row["volume"]) or 0
                    for row in valid_rows
                    if row["trading_date"] == trading_date
                )
                for trading_date in expected
            }
            if all(math.isfinite(value) and value > 0 for value in aggregates.values()):
                latest_volume = aggregates[expected[0]]
                median_volume = median(aggregates.values())
                ratio = latest_volume / median_volume if median_volume > 0 else None
                if ratio is not None and not math.isfinite(ratio):
                    ratio = None
                evidence.update({
                    "available": True,
                    "status": "available",
                    "latest_aggregate_volume_usd": latest_volume,
                    "median_aggregate_daily_volume_usd": median_volume,
                    "latest_to_horizon_median_ratio": ratio,
                    "lineage": _crypto_volume_lineage(valid_rows, cutoff),
                    "valid_rows": tuple(valid_rows),
                    "blockers": [],
                    "data_requests": [],
                })
        if not evidence["available"]:
            blockers = []
            for count, blocker in (
                (evidence["missing_member_count"], "crypto_daily_trading_volume_missing"),
                (evidence["wrong_asset_class_member_count"], "crypto_daily_trading_volume_wrong_asset_class"),
                (evidence["stale_member_count"], "crypto_daily_trading_volume_stale"),
                (evidence["truncated_member_count"], "crypto_daily_trading_volume_truncated"),
                (evidence["duplicate_member_count"], "crypto_daily_trading_volume_duplicate"),
                (evidence["invalid_member_count"], "crypto_daily_trading_volume_invalid"),
                (evidence["wrong_currency_member_count"], "crypto_daily_trading_volume_wrong_currency"),
                (evidence["wrong_source_member_count"], "crypto_daily_trading_volume_wrong_source"),
            ):
                if count:
                    blockers.append(blocker)
            evidence["blockers"] = blockers or ["crypto_daily_trading_volume_unavailable"]
        result[horizon] = evidence
    return result


def _crypto_volume_lineage(rows: list[dict[str, Any]], cutoff: datetime) -> tuple[InputLineage, ...]:
    lineage: list[InputLineage] = []
    for row in rows:
        available_at = _as_utc(row.get("available_at"))
        observed_at = _as_utc(row.get("observed_at"))
        confirmed_at = _as_utc(row.get("confirmed_at"))
        if available_at is None or observed_at is None or confirmed_at is None:
            continue
        fact_table = str(row.get("fact_table") or "raw.price_bar")
        fact_id = int(row["fact_id"]) if row.get("fact_id") is not None else None
        revision = f"{fact_table}:{fact_id}:{available_at.isoformat()}"
        lineage.append(InputLineage(
            field="crypto_daily_trading_volume_usd",
            source_id=_CRYPTO_SOURCE_ID,
            source_version=str(row.get("ingest_run_id") or revision),
            event_at=observed_at,
            available_at=available_at,
            received_at=confirmed_at,
            revision=revision,
            cutoff=cutoff,
            fact_id=fact_id,
            fact_table=fact_table,
            symbol=str(row.get("symbol")),
            trading_date=row.get("trading_date"),
            currency="USD",
            observed_at=observed_at,
            confirmed_at=confirmed_at,
        run_finished_at=_as_utc(row.get("run_finished_at")) or confirmed_at,
            ingest_run_id=str(row.get("ingest_run_id")) if row.get("ingest_run_id") is not None else None,
        ))
    return tuple(sorted(lineage, key=lambda item: (item.trading_date, item.symbol, item.fact_id or 0)))


def _completed_crypto_dates(as_of: datetime, *, count: int) -> tuple[date, ...]:
    reference = _as_utc(as_of)
    if reference is None or count <= 0:
        return ()
    return tuple(reference.date() - timedelta(days=offset) for offset in range(1, count + 1))


def _crypto_volume_inputs(evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        horizon: {
            key: value
            for key, value in row.items()
            if key not in {"lineage", "valid_rows"}
        } | {"lineage": [item.model_dump(mode="json") for item in row.get("lineage") or ()]}
        for horizon, row in evidence.items()
    }


def _corporate_cycle_evidence(
    connection: Any,
    instrument_rows: list[dict[str, Any]],
    cutoff: datetime,
) -> dict[str, Any]:
    benchmark = sorted(
        (
            row for row in instrument_rows
            if row.get("market_benchmark_member")
            and str(row.get("asset_class") or "").lower() == "equity"
        ),
        key=lambda row: str(row.get("symbol") or ""),
    )
    base = {
        "available": False,
        "status": "unavailable",
        "benchmark_key": _CORPORATE_BENCHMARK_KEY,
        "eligible_members": [str(row["symbol"]) for row in benchmark],
        "eligible_member_count": len(benchmark),
        "available_member_count": 0,
        "missing_member_count": 0,
        "stale_member_count": 0,
        "duplicate_member_count": 0,
        "invalid_member_count": 0,
        "median_revenue_growth": None,
        "median_operating_margin_change_bps": None,
        "selected_periods": (),
        "lineage": (),
        "blockers": [],
        "data_requests": ["update_company_financials"],
    }
    if not benchmark:
        base["blockers"] = ["market_corporate_equity_benchmark_unavailable"]
        base["missing_member_count"] = 0
        return base

    source = connection.execute(
        """
        SELECT enabled, operational_state, freshness_seconds
        FROM ingest.source
        WHERE id = %s
          AND created_at <= %s
        """,
        [_CORPORATE_SOURCE_ID, cutoff],
    ).fetchone()
    if source is None:
        base["blockers"] = ["corporate_cycle_source_missing"]
        base["missing_member_count"] = len(benchmark)
        return base
    source = dict(source)
    if not source["enabled"] or source["operational_state"] != "active":
        base["blockers"] = ["corporate_cycle_source_unavailable"]
        base["invalid_member_count"] = len(benchmark)
        return base
    freshness_seconds = int(source.get("freshness_seconds") or 0)
    if freshness_seconds != 86400:
        base["blockers"] = ["corporate_cycle_source_lifecycle_mismatch"]
        base["invalid_member_count"] = len(benchmark)
        return base

    latest_run = connection.execute(
        """
        SELECT max(run.finished_at) AS finished_at
        FROM ingest.run run
        WHERE run.source_id = %s
          AND run.capability = 'company_financials'
          AND run.status IN ('succeeded', 'partial')
          AND run.finished_at IS NOT NULL
          AND run.finished_at <= %s
        """,
        [_CORPORATE_SOURCE_ID, cutoff],
    ).fetchone()
    latest_finished_at = _as_utc(latest_run["finished_at"]) if latest_run and latest_run["finished_at"] else None
    if latest_finished_at is None:
        base["blockers"] = ["corporate_cycle_source_run_unavailable"]
        base["invalid_member_count"] = len(benchmark)
        return base
    if cutoff - latest_finished_at > timedelta(seconds=freshness_seconds):
        base["blockers"] = ["corporate_cycle_source_run_stale"]
        base["stale_member_count"] = len(benchmark)
        return base

    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT observation.id, observation.instrument_id, instrument.symbol,
                   observation.metric_set, observation.period_start, observation.period_end,
                   observation.filed_at, observation.observed_at, observation.values,
                   run.id::text AS ingest_run_id, run.finished_at AS available_at
            FROM raw.fundamental_observation observation
            JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
            JOIN ingest.source source
              ON source.id = observation.source_id
             AND source.created_at <= %s
            JOIN ingest.run run ON run.id = observation.ingest_run_id
            WHERE observation.instrument_id = ANY(%s)
              AND observation.source_id = %s
              AND observation.metric_set = %s
              AND observation.filed_at IS NOT NULL
              AND observation.filed_at <= %s
              AND observation.observed_at <= %s
              AND run.capability = 'company_financials'
              AND run.status IN ('succeeded', 'partial')
              AND run.finished_at IS NOT NULL
              AND run.finished_at <= %s
            ORDER BY observation.instrument_id, observation.period_end DESC,
                     observation.filed_at DESC, observation.observed_at DESC, observation.id DESC
            """,
            [cutoff, [int(row["id"]) for row in benchmark], _CORPORATE_SOURCE_ID,
             _CORPORATE_METRIC_SET, cutoff, cutoff, cutoff],
        ).fetchall()
    ]
    by_member: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_member[int(row["instrument_id"])].append(row)

    valid_members: list[dict[str, Any]] = []
    counts = {key: 0 for key in ("missing", "stale", "duplicate", "invalid")}
    blockers: set[str] = set()
    for member in benchmark:
        annual_by_end: dict[date, list[dict[str, Any]]] = defaultdict(list)
        invalid_rows = 0
        for row in by_member.get(int(member["id"]), ()):
            values = row.get("values")
            if not isinstance(values, dict):
                invalid_rows += 1
                continue
            form = str(values.get("form") or "").strip()
            fiscal_period = str(values.get("fiscal_period") or "").strip().upper()
            if form not in {"10-K", "10-K/A"} or fiscal_period != "FY":
                continue
            period_end = _as_date(row.get("period_end"))
            fact = _corporate_fact(row, period_end)
            if period_end is None:
                invalid_rows += 1
                continue
            annual_by_end[period_end].append(fact)

        selected: list[dict[str, Any]] = []
        duplicate = False
        for period_end in sorted(annual_by_end, reverse=True):
            candidates = annual_by_end[period_end]
            candidates.sort(key=lambda item: (item["accepted_at"], item["observed_at"], int(item["id"])))
            newest = candidates[-1]
            newest_time = (newest["accepted_at"], newest["observed_at"])
            if sum((item["accepted_at"], item["observed_at"]) == newest_time for item in candidates) > 1:
                duplicate = True
            if len({item["accession_number"] for item in candidates}) < len(candidates):
                duplicate = True
            selected.append(newest)

        if not selected:
            bucket = "invalid" if invalid_rows else "missing"
            counts[bucket] += 1
            blockers.add(
                "corporate_cycle_annual_fact_invalid"
                if invalid_rows else "corporate_cycle_annual_pair_missing"
            )
            continue
        latest = selected[0]
        if latest["period_end"] > cutoff.date() or cutoff.date() - latest["period_end"] > timedelta(days=550):
            counts["stale"] += 1
            blockers.add("corporate_cycle_annual_pair_stale")
            continue
        if len(selected) < 2:
            counts["missing"] += 1
            blockers.add("corporate_cycle_annual_pair_missing")
            continue
        prior = selected[1]
        if any(
            fact["available_at"] is None
            or cutoff - _as_utc(fact["available_at"]) > timedelta(seconds=freshness_seconds)
            for fact in (latest, prior)
        ):
            counts["stale"] += 1
            blockers.add("corporate_cycle_annual_pair_stale")
            continue
        if duplicate:
            counts["duplicate"] += 1
            blockers.add("corporate_cycle_annual_pair_duplicate")
            continue
        if not latest["valid"] or not prior["valid"]:
            counts["invalid"] += 1
            blockers.add("corporate_cycle_annual_fact_invalid")
            continue
        if not 300 <= (latest["period_end"] - prior["period_end"]).days <= 430:
            counts["invalid"] += 1
            blockers.add("corporate_cycle_annual_periods_not_comparable")
            continue
        if latest["units"] != prior["units"]:
            counts["invalid"] += 1
            blockers.add("corporate_cycle_annual_units_incompatible")
            continue
        valid_members.append({"member": member, "latest": latest, "prior": prior})

    available = len(valid_members) == len(benchmark)
    if not available:
        base.update({
            "available_member_count": len(valid_members),
            "missing_member_count": counts["missing"],
            "stale_member_count": counts["stale"],
            "duplicate_member_count": counts["duplicate"],
            "invalid_member_count": counts["invalid"],
            "blockers": sorted(blockers),
        })
        return base

    selected_periods = tuple(
        {
            "symbol": str(item["member"]["symbol"]),
            "latest": {
                "start": item["latest"]["period_start"],
                "end": item["latest"]["period_end"],
                "accession_number": item["latest"]["accession_number"],
                "revenue": item["latest"]["revenue"],
                "operating_income": item["latest"]["operating_income"],
            },
            "prior": {
                "start": item["prior"]["period_start"],
                "end": item["prior"]["period_end"],
                "accession_number": item["prior"]["accession_number"],
                "revenue": item["prior"]["revenue"],
                "operating_income": item["prior"]["operating_income"],
            },
        }
        for item in valid_members
    )
    lineages = tuple(
        lineage
        for item in valid_members
        for lineage in (
            _corporate_lineage(item["latest"], cutoff),
            _corporate_lineage(item["prior"], cutoff),
        )
    )
    return {
        **base,
        "available": True,
        "status": "available",
        "available_member_count": len(valid_members),
        "missing_member_count": 0,
        "stale_member_count": 0,
        "duplicate_member_count": 0,
        "invalid_member_count": 0,
        "median_revenue_growth": median(item["latest"]["revenue"] / item["prior"]["revenue"] - 1 for item in valid_members),
        "median_operating_margin_change_bps": median(
            ((item["latest"]["operating_income"] / item["latest"]["revenue"])
             - (item["prior"]["operating_income"] / item["prior"]["revenue"])) * 10000
            for item in valid_members
        ),
        "latest_period_start": min(item["latest"]["period_start"] for item in valid_members),
        "latest_period_end": max(item["latest"]["period_end"] for item in valid_members),
        "prior_period_start": min(item["prior"]["period_start"] for item in valid_members),
        "prior_period_end": max(item["prior"]["period_end"] for item in valid_members),
        "selected_periods": selected_periods,
        "lineage": lineages,
        "blockers": [],
        "data_requests": [],
        "freshness_latest_available_at": latest_finished_at,
        "freshness_max_age_days": 1,
    }


def _corporate_fact(row: dict[str, Any], period_end: date | None) -> dict[str, Any]:
    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    metrics = values.get("metrics") if isinstance(values.get("metrics"), dict) else {}
    tags = values.get("tags") if isinstance(values.get("tags"), dict) else {}
    revenue = _finite_number(metrics.get("revenue"))
    operating_income = _finite_number(metrics.get("operating_income"))
    period_start = _as_date(row.get("period_start"))
    units = []
    for metric in ("revenue", "operating_income"):
        tag = tags.get(metric)
        unit = tag.get("unit") if isinstance(tag, dict) else None
        units.append(unit.strip() if isinstance(unit, str) else "")
    units = tuple(units)
    accession_number = str(values.get("accession_number") or "").strip()
    accepted_at = _as_utc(row.get("filed_at"))
    observed_at = _as_utc(row.get("observed_at"))
    valid = (
        period_start is not None
        and period_end is not None
        and 300 <= (period_end - period_start).days <= 400
        and bool(accession_number)
        and accepted_at is not None
        and observed_at is not None
        and revenue is not None
        and revenue > 0
        and operating_income is not None
        and all(units)
    )
    return {
        **row,
        "period_start": period_start,
        "period_end": period_end,
        "accession_number": accession_number,
        "accepted_at": accepted_at,
        "observed_at": observed_at,
        "revenue": revenue,
        "operating_income": operating_income,
        "units": units,
        "valid": valid,
    }




def _corporate_cycle_inputs(evidence: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value for key, value in evidence.items()
        if key not in {"lineage", "valid_rows"}
    }
    result["lineage"] = [item.model_dump(mode="json") for item in evidence.get("lineage") or ()]
    return json.loads(json_dumps(result))


def _event_risk_evidence(connection: Any, cutoff: datetime) -> dict[str, dict[str, Any]]:
    rows = [
        dict(row)
        for row in connection.execute(
            """
            WITH visible AS (
                SELECT DISTINCT ON (version.market_event_id)
                       version.id AS market_event_version_id,
                       version.market_event_id,
                       version.source_id,
                       version.ingest_run_id,
                       version.event_scope,
                       version.event_kind,
                       version.title,
                       version.starts_at,
                       version.available_at,
                       version.verification_status,
                       ingestion.finished_at AS ingest_finished_at,
                       source.enabled,
                       source.operational_state,
                       source.freshness_seconds
                FROM raw.market_event_version version
                JOIN ingest.run ingestion ON ingestion.id = version.ingest_run_id
                JOIN ingest.source source
                  ON source.id = version.source_id
                 AND source.created_at <= %s
                WHERE version.source_id = 'official-event-calendar'
                  AND version.available_at <= %s
                  AND ingestion.status IN ('succeeded', 'partial')
                  AND ingestion.finished_at IS NOT NULL
                  AND ingestion.finished_at <= %s
                ORDER BY version.market_event_id, version.available_at DESC, version.id DESC
            )
            SELECT *
            FROM visible
            WHERE enabled
              AND operational_state = 'active'
              AND event_scope = 'macro'
              AND starts_at > %s
              AND btrim(title) <> ''
              AND btrim(event_kind) <> ''
              AND lower(btrim(coalesce(verification_status, ''))) IN ('confirmed', 'verified', 'scheduled')
              AND freshness_seconds > 0
              AND ingest_finished_at >= %s - make_interval(secs => freshness_seconds)
            ORDER BY starts_at, event_kind, title, market_event_id, market_event_version_id
            """,
            [cutoff, cutoff, cutoff, cutoff, cutoff],
        ).fetchall()
    ]
    result = {
        horizon: {
            "available": False,
            "status": "unavailable",
            "blockers": ["event_risk_inputs_unavailable"],
            "data_requests": ["update_market_events"],
            "eligible_event_count": 0,
            "scheduled_events": (),
            "lineage": (),
        }
        for horizon in MARKET_HORIZONS
    }
    for row in rows:
        horizon = _event_risk_horizon(row["starts_at"], cutoff)
        if horizon is None:
            continue
        evidence = result[horizon]
        evidence["eligible_event_count"] += 1
        if len(evidence["scheduled_events"]) >= _MAX_EVENT_RISK_SCHEDULE:
            continue
        starts_at = _as_utc(row["starts_at"])
        available_at = _as_utc(row["available_at"])
        finished_at = _as_utc(row["ingest_finished_at"])
        if starts_at is None or available_at is None or finished_at is None:
            continue
        fact_identity = f"raw.market_event_version:{int(row['market_event_version_id'])}:{available_at.isoformat()}"
        lineage = InputLineage(
            field="market_event_schedule",
            source_id=str(row["source_id"]),
            source_version=str(row["ingest_run_id"]),
            event_at=starts_at,
            available_at=available_at,
            received_at=finished_at,
            revision=fact_identity,
            cutoff=cutoff,
            fact_id=int(row["market_event_version_id"]),
            fact_table="raw.market_event_version",
            market_event_id=int(row["market_event_id"]),
            ingest_run_id=str(row["ingest_run_id"]),
        )
        evidence["scheduled_events"] = (*evidence["scheduled_events"], {
            "title": str(row["title"]).strip(),
            "kind": str(row["event_kind"]).strip().lower(),
            "starts_at": starts_at.isoformat(),
        })
        evidence["lineage"] = (*evidence["lineage"], lineage)
        evidence["available"] = True
        evidence["status"] = "available"
        evidence["blockers"] = []
        evidence["data_requests"] = []
        evidence["freshness_max_age_seconds"] = int(row["freshness_seconds"])
    return result


def _event_risk_horizon(starts_at: datetime, cutoff: datetime) -> str | None:
    event_local = _as_utc(starts_at).astimezone(MARKET_TZ)
    cutoff_local = _as_utc(cutoff).astimezone(MARKET_TZ)
    if event_local.date() == cutoff_local.date() and is_us_market_day(event_local.date()):
        return "intraday"
    target_date = event_local.date()
    while not is_us_market_day(target_date):
        target_date += timedelta(days=1)
    cursor = cutoff_local.date() + timedelta(days=1)
    trading_days = 0
    while cursor <= target_date:
        if is_us_market_day(cursor):
            trading_days += 1
        cursor += timedelta(days=1)
    if trading_days <= 5:
        return "1-5 trading days"
    if trading_days <= 40:
        return "2-8 weeks"
    if trading_days > _HORIZON_LOOKBACK["3-12 months"]:
        return None
    return "3-12 months"








































def _finite_number(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and math.isfinite(result) else None
