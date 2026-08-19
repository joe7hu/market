"""Point-in-time, research-only event-volatility evidence.

This module has no order, ticket, or sizing concepts.  It only materializes
historical observations that were known at an explicit ``as_of`` cutoff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil, floor, isfinite
from random import Random
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from investment_panel.analysis.trend_features import compute_trend_feature
from investment_panel.core.decision import is_us_market_day, market_session_bounds
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE

MIN_EVENT_SAMPLES = 20
MAX_EVENT_COHORT = 100
MAX_EVENT_ROWS_PER_GROUP = 320
MAX_EVENT_INSTRUMENTS = 10
MAX_EVENT_TARGETS_TOTAL = 10
MAX_EVENT_REGIME_EVALUATIONS = 250
EVENT_TREND_WINDOW = 260
MAX_EVENT_FACT_VERSIONS_PER_INSTRUMENT = 12_000
FEATURE_VERSION = "event-volatility-v1"
SUPPORTED_EVENT_KINDS = frozenset({"earnings", "cpi", "nfp", "fomc", "pce", "gdp", "opex"})
MARKET_EVENT_KINDS = frozenset({"cpi", "nfp", "fomc", "pce", "gdp", "opex"})


def percentile(values: Iterable[float], fraction: float) -> float | None:
    """Linear-interpolated percentile for a finite, non-empty sample."""
    ordered = _finite(values)
    if not ordered or not 0 <= fraction <= 1:
        return None
    position = (len(ordered) - 1) * fraction
    lower, upper = floor(position), ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap_median_interval(
    values: Iterable[float], *, iterations: int = 1_000, seed: int = 0
) -> tuple[float | None, float | None]:
    """Deterministic 95% bootstrap interval for the sample median."""
    sample = _finite(values)
    if not sample or iterations <= 0:
        return None, None
    generator = Random(seed)
    medians = sorted(median([generator.choice(sample) for _ in sample]) for _ in range(iterations))
    return percentile(medians, 0.025), percentile(medians, 0.975)


def paired_atm_straddle_cost(rows: Iterable[dict[str, Any]], *, underlying_price: float | None) -> float | None:
    """Return one complete call+put ATM straddle cost, never a half-straddle.

    Quotes must share expiry and strike.  A missing, crossed, or non-positive
    leg deliberately produces no implied-move evidence.
    """
    if underlying_price is None or not isfinite(float(underlying_price)) or float(underlying_price) <= 0:
        return None
    pairs: dict[tuple[Any, float], dict[str, float]] = {}
    for row in rows:
        try:
            expiration = row["expiration"]
            strike = float(row["strike"])
            option_type = str(row["option_type"]).lower()
        except (KeyError, TypeError, ValueError):
            continue
        if option_type not in {"call", "put"} or not isfinite(strike):
            continue
        price = _quote_price(row)
        if price is None:
            continue
        pairs.setdefault((expiration, strike), {})[option_type] = price
    eligible = [
        (expiration, abs(strike - float(underlying_price)), strike, legs)
        for (expiration, strike), legs in pairs.items()
        if set(legs) == {"call", "put"}
    ]
    if not eligible:
        return None
    _expiration, _distance, _strike, legs = min(eligible, key=lambda item: (item[0], item[1], item[2]))
    return legs["call"] + legs["put"]


def summarize_actual_moves(values: Iterable[float], *, min_samples: int = MIN_EVENT_SAMPLES) -> dict[str, Any]:
    sample = [abs(value) for value in _finite(values)]
    if len(sample) < min_samples:
        return {"sample_size": len(sample), "evidence_state": "insufficient_event_evidence"}
    low, high = bootstrap_median_interval(sample)
    return {
        "sample_size": len(sample), "evidence_state": "ready",
        "actual_move_median": percentile(sample, 0.5),
        "actual_move_p75": percentile(sample, 0.75),
        "actual_move_p90": percentile(sample, 0.9),
        "bootstrap_low": low,
        "bootstrap_high": high,
        "win_rate": None,
    }


def materialize_event_studies(
    runtime: DatabaseRuntime, *, run_id: Any, as_of: datetime, horizon: int = 1,
    feature_version: str = FEATURE_VERSION, target_limit: int = 3,
) -> int:
    """Store bounded event studies using only confirmed, cutoff-visible facts."""
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if target_limit <= 0 or target_limit > 10:
        raise ValueError("target_limit must be between 1 and 10 per instrument/event kind")
    cutoff = _utc(as_of)
    with runtime.read(JOB_PROFILE) as connection:
        events = [dict(row) for row in connection.execute(
            """
            WITH qqq AS (
                SELECT id FROM catalog.instrument WHERE symbol = 'QQQ' LIMIT 1
            ), ranked_instruments AS (
                SELECT decision.instrument_id,
                       min(coalesce(decision.rank, 2147483647)) AS priority
                FROM analysis.decision decision
                WHERE decision.run_id = %s
                GROUP BY decision.instrument_id
            ), universe_candidates AS (
                SELECT id AS instrument_id, 0 AS priority FROM qqq
                UNION ALL
                SELECT instrument_id, priority FROM ranked_instruments
            ), radar_universe AS (
                SELECT instrument_id
                FROM universe_candidates
                GROUP BY instrument_id
                ORDER BY min(priority), instrument_id
                LIMIT %s
            ), visible AS (
                SELECT DISTINCT ON (version.market_event_id) version.*
                FROM raw.market_event_version version
                JOIN ingest.run ingestion ON ingestion.id = version.ingest_run_id
                WHERE version.available_at <= %s
                  AND ingestion.status IN ('succeeded', 'partial') AND ingestion.finished_at <= %s
                  AND (version.instrument_id IS NULL
                       OR version.instrument_id IN (SELECT instrument_id FROM radar_universe))
                ORDER BY version.market_event_id, version.available_at DESC, version.id DESC
            ), normalized AS (
                SELECT event.*,
                       CASE
                         WHEN lower(event.event_kind) = 'inflation'
                           AND lower(event.title) LIKE '%%consumer price index%%' THEN 'cpi'
                         WHEN lower(event.event_kind) = 'labor'
                           AND lower(event.title) LIKE '%%employment situation%%' THEN 'nfp'
                         WHEN lower(event.event_kind) = 'central_bank'
                           AND lower(event.title) LIKE '%%fomc%%' THEN 'fomc'
                         ELSE lower(event.event_kind)
                       END AS normalized_kind
                FROM visible event
            ), resolved AS (
                SELECT event.*,
                       coalesce(event.instrument_id,
                           CASE WHEN event.normalized_kind = ANY(%s) THEN qqq.id END)
                         AS resolved_instrument_id,
                       qqq.id AS benchmark_instrument_id
                FROM normalized event CROSS JOIN qqq
            ), ranked AS (
                SELECT event.*,
                       row_number() OVER (
                         PARTITION BY event.resolved_instrument_id, event.normalized_kind
                         ORDER BY abs(extract(epoch FROM event.starts_at - %s)),
                                  event.starts_at, event.id
                       ) AS event_rank
                FROM resolved event
                WHERE event.resolved_instrument_id IN (SELECT instrument_id FROM radar_universe)
                  AND event.normalized_kind = ANY(%s)
                  AND event.verification_status IN ('confirmed', 'verified')
            )
            SELECT event.market_event_id AS id, event.id AS market_event_version_id,
                   event.resolved_instrument_id AS instrument_id,
                   event.normalized_kind AS event_kind, event.starts_at, event.available_at,
                   event.benchmark_instrument_id
            FROM ranked event
            WHERE event.event_rank <= %s
            ORDER BY event.instrument_id, event.event_kind, event.starts_at, event.id
            """, [run_id, MAX_EVENT_INSTRUMENTS, cutoff, cutoff, list(MARKET_EVENT_KINDS), cutoff,
                   list(SUPPORTED_EVENT_KINDS), MAX_EVENT_ROWS_PER_GROUP]
        ).fetchall()]
        events = deduplicate_logical_events(events)
        target_events = bounded_event_targets(
            events, cutoff=cutoff, per_group=target_limit,
            total_limit=MAX_EVENT_TARGETS_TOTAL,
        )
        cohort_events = bounded_event_cohort(
            events, target_events, cutoff=cutoff, max_peers=MAX_EVENT_COHORT,
            max_total=MAX_EVENT_REGIME_EVALUATIONS,
        )
        price_instrument_ids = {
            int(value)
            for row in cohort_events
            for value in (row["instrument_id"], row["benchmark_instrument_id"])
        }
        bars = confirmed_daily_bars(
            connection, price_instrument_ids, as_of=cutoff, max_bars=6_000,
            include_versions=True,
            max_fact_versions=MAX_EVENT_FACT_VERSIONS_PER_INSTRUMENT,
        )
        quotes = _event_implied_moves(connection, target_events, cutoff)
    regimes = {
        int(event["id"]): _pre_event_regime(event, bars, cutoff=cutoff)
        for event in cohort_events
    }
    move_completed_date = _latest_completed_market_date(cutoff)
    move_bars = {
        instrument_id: _canonical_event_bars(
            rows, reference=cutoff, completed_date=move_completed_date,
        )
        for instrument_id, rows in bars.items()
    }
    prepared = []
    for event in target_events:
        session = event_session(event["starts_at"])
        regime = regimes[int(event["id"])]
        peers = [candidate for candidate in cohort_events if candidate["instrument_id"] == event["instrument_id"]
                 and candidate["event_kind"] == event["event_kind"]
                 and event_session(candidate["starts_at"]) == session
                 and regimes[int(candidate["id"])] == regime
                 and candidate["starts_at"] < min(event["starts_at"], cutoff)]
        moves = [
            _event_move(
                move_bars.get(int(event["instrument_id"]), []), peer["starts_at"], horizon,
                session=event_session(peer["starts_at"]),
            )
            for peer in peers
        ]
        summary = summarize_actual_moves((move for move in moves if move is not None))
        if regime == "unavailable":
            summary = {"sample_size": 0, "evidence_state": "insufficient_event_evidence"}
        prepared.append((event, session, regime, summary, quotes.get(int(event["id"]))))
    with runtime.transaction(JOB_PROFILE) as connection:
        for event, session, regime, summary, implied_move in prepared:
            connection.execute(
                """
                INSERT INTO analysis.event_study_feature (
                    run_id, instrument_id, market_event_id, market_event_version_id, as_of,
                    event_kind, event_session,
                    pre_event_regime, horizon, sample_size, actual_move_median,
                    actual_move_p75, actual_move_p90, bootstrap_low, bootstrap_high,
                    win_rate, implied_move, evidence_state, feature_version, details
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id, instrument_id, market_event_version_id, horizon, feature_version)
                DO UPDATE SET sample_size=EXCLUDED.sample_size, actual_move_median=EXCLUDED.actual_move_median,
                  actual_move_p75=EXCLUDED.actual_move_p75, actual_move_p90=EXCLUDED.actual_move_p90,
                  bootstrap_low=EXCLUDED.bootstrap_low, bootstrap_high=EXCLUDED.bootstrap_high,
                  win_rate=EXCLUDED.win_rate, implied_move=EXCLUDED.implied_move,
                  evidence_state=EXCLUDED.evidence_state, details=EXCLUDED.details
                """, [run_id, event["instrument_id"], event["id"], event["market_event_version_id"], cutoff,
                    event["event_kind"], session, regime, horizon,
                    summary["sample_size"], summary.get("actual_move_median"), summary.get("actual_move_p75"),
                    summary.get("actual_move_p90"), summary.get("bootstrap_low"), summary.get("bootstrap_high"),
                    summary.get("win_rate"), implied_move, summary["evidence_state"], feature_version,
                    Jsonb({
                        "research_only": True,
                        "work_budget": {
                            "max_instruments": MAX_EVENT_INSTRUMENTS,
                            "max_targets": MAX_EVENT_TARGETS_TOTAL,
                            "max_regime_evaluations": MAX_EVENT_REGIME_EVALUATIONS,
                        },
                        "confirmed_provenance": True,
                        "complete_same_expiry_atm_legs": implied_move is not None,
                        "pre_event_regime": regime,
                        "regime_blockers": ["pre_event_regime_unavailable"] if regime == "unavailable" else [],
                        "iv_crush_frequency": None,
                        "atm_iv": None,
                        "skew_25": None,
                        "term_slope": None,
                        "unavailable_fields": ["iv_crush_frequency", "atm_iv", "skew_25", "term_slope"],
                        "win_rate_semantics": "unavailable_until_historical_event_straddles_are_paired",
                    })])
    return len(prepared)


def event_study_rows(runtime: DatabaseRuntime, *, ticker: str, event_kind: str, as_of: datetime) -> list[dict[str, Any]]:
    """Return the nearest applicable event from the latest visible study run."""
    cutoff = _utc(as_of)
    requested_kind = event_kind.strip().lower()
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT study.id::text AS id, instrument.symbol AS ticker, study.event_kind,
                   study.as_of, event.starts_at AS event_starts_at,
                   study.event_session, study.horizon, study.sample_size,
                   study.actual_move_median, study.actual_move_p75, study.actual_move_p90,
                   study.bootstrap_low, study.bootstrap_high, study.win_rate, study.implied_move,
                   study.evidence_state, study.feature_version, study.details
            FROM analysis.event_study_feature study
            JOIN catalog.instrument instrument ON instrument.id = study.instrument_id
            JOIN raw.market_event_version event ON event.id = study.market_event_version_id
            JOIN ingest.run ingestion ON ingestion.id = event.ingest_run_id
            JOIN analysis.run study_run ON study_run.id = study.run_id
            WHERE instrument.symbol = %s AND (%s = 'nearest' OR study.event_kind = %s)
              AND study.run_id = (
                SELECT latest.run_id
                FROM analysis.event_study_feature latest
                JOIN catalog.instrument latest_instrument ON latest_instrument.id = latest.instrument_id
                JOIN analysis.run latest_run ON latest_run.id = latest.run_id
                WHERE latest_instrument.symbol = %s
                  AND (%s = 'nearest' OR latest.event_kind = %s)
                  AND latest.as_of <= %s AND latest_run.status = 'succeeded'
                ORDER BY latest.as_of DESC, latest_run.finished_at DESC, latest.run_id DESC LIMIT 1
              )
              AND event.starts_at BETWEEN %s - interval '45 days' AND %s + interval '45 days'
              AND event.verification_status IN ('confirmed', 'verified')
              AND ingestion.status IN ('succeeded', 'partial') AND ingestion.finished_at <= %s
              AND event.available_at <= %s
              AND study_run.status = 'succeeded'
            ORDER BY abs(extract(epoch FROM event.starts_at - %s)), event.starts_at, event.id
            LIMIT 1
            """, [ticker.strip().upper(), requested_kind, requested_kind,
                    ticker.strip().upper(), requested_kind, requested_kind,
                    cutoff, cutoff, cutoff, cutoff, cutoff, cutoff]
        ).fetchall()
    return [dict(row) for row in rows]


def deduplicate_logical_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count one occurrence once when more than one confirmed feed reports it."""
    selected: dict[tuple[int, str, str, datetime], dict[str, Any]] = {}
    for event in events:
        key = (
            int(event["instrument_id"]),
            str(event["event_kind"]),
            event_session(event["starts_at"]),
            _utc(event["starts_at"]),
        )
        current = selected.get(key)
        candidate_order = (_utc(event.get("available_at") or event["starts_at"]), int(event["id"]))
        current_order = (
            _utc(current.get("available_at") or current["starts_at"]), int(current["id"])
        ) if current else None
        if current is None or candidate_order < current_order:
            selected[key] = event
    return sorted(
        selected.values(),
        key=lambda row: (int(row["instrument_id"]), str(row["event_kind"]), row["starts_at"], int(row["id"])),
    )


def bounded_event_targets(
    events: Iterable[dict[str, Any]], *, cutoff: datetime, per_group: int,
    total_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Keep nearest events with both per-group and whole-run limits."""
    if per_group <= 0 or (total_limit is not None and total_limit <= 0):
        raise ValueError("event target limits must be positive")
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for event in events:
        groups.setdefault((int(event["instrument_id"]), str(event["event_kind"])), []).append(event)
    selected = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda row: (
                abs((_utc(row["starts_at"]) - cutoff).total_seconds()),
                _utc(row["starts_at"]),
                int(row["id"]),
            ),
        )
        selected.extend(ordered[:per_group])
    ordered = sorted(
        selected,
        key=lambda row: (abs((_utc(row["starts_at"]) - cutoff).total_seconds()), int(row["id"])),
    )
    return ordered[:total_limit] if total_limit is not None else ordered


def bounded_event_cohort(
    events: Iterable[dict[str, Any]], targets: Iterable[dict[str, Any]], *,
    cutoff: datetime, max_peers: int, max_total: int | None = None,
) -> list[dict[str, Any]]:
    """Keep target cohorts under one strict, fairly shared run budget."""
    universe = list(events)
    target_rows = list(targets)
    if max_peers < 0 or (max_total is not None and max_total <= 0):
        raise ValueError("event cohort limits must be valid")
    if max_total is not None and len(target_rows) > max_total:
        raise ValueError("event targets exceed the total cohort budget")
    selected: dict[int, dict[str, Any]] = {}
    peer_groups: list[list[dict[str, Any]]] = []
    for target in target_rows:
        selected[int(target["id"])] = target
        peers = [
            event for event in universe
            if event["instrument_id"] == target["instrument_id"]
            and event["event_kind"] == target["event_kind"]
            and event_session(event["starts_at"]) == event_session(target["starts_at"])
            and event["starts_at"] < min(target["starts_at"], cutoff)
        ]
        peer_groups.append(sorted(peers, key=lambda row: row["starts_at"], reverse=True)[:max_peers])
    for depth in range(max_peers):
        for peers in peer_groups:
            if depth >= len(peers):
                continue
            peer = peers[depth]
            selected[int(peer["id"])] = peer
            if max_total is not None and len(selected) >= max_total:
                return sorted(selected.values(), key=lambda row: (row["starts_at"], int(row["id"])))
    return sorted(selected.values(), key=lambda row: (row["starts_at"], int(row["id"])))


def _pre_event_regime(
    event: dict[str, Any],
    bars_by_instrument: dict[int, list[dict[str, Any]]],
    *,
    cutoff: datetime,
) -> str:
    instrument_id = int(event["instrument_id"])
    benchmark_id = int(event["benchmark_instrument_id"])
    reference = min(_utc(event["starts_at"]), cutoff)
    local_reference = reference.astimezone(ZoneInfo("America/New_York"))
    completed_date = _latest_completed_market_date(reference)

    def visible(instrument: int) -> list[dict[str, Any]]:
        canonical = _canonical_event_bars(
            bars_by_instrument.get(instrument, []), reference=reference,
            completed_date=completed_date,
        )
        return canonical[-EVENT_TREND_WINDOW:]

    feature = compute_trend_feature(
        visible(instrument_id),
        visible(benchmark_id),
        as_of_date=local_reference.date(),
        expected_last_date=completed_date,
        require_relative_strength=instrument_id != benchmark_id,
    )
    return feature.trend_state


def event_session(starts_at: datetime) -> str:
    local = _utc(starts_at).astimezone(ZoneInfo("America/New_York"))
    if not is_us_market_day(local.date()):
        return "non_market"
    market_open, market_close = market_session_bounds(local.date())
    if local < market_open:
        return "pre_market"
    if local >= market_close:
        return "post_market"
    return "regular"


def _canonical_event_bars(
    rows: list[dict[str, Any]], *, reference: datetime, completed_date: Any,
) -> list[dict[str, Any]]:
    visible = [
        row for row in rows
        if row.get("trading_date") and row["trading_date"] <= completed_date
        and isinstance(row.get("available_at"), datetime)
        and _utc(row["available_at"]) <= reference
        and isinstance(row.get("observed_at"), datetime)
        and _utc(row["observed_at"]) <= reference
        and isinstance(row.get("confirmed_at"), datetime)
        and _utc(row["confirmed_at"]) <= reference
    ]
    priority = {"polygon": 1, "yahoo_chart": 2, "yfinance": 3}
    ordered = sorted(
        visible,
        key=lambda row: (
            row["trading_date"], priority.get(str(row.get("source_id")), 10),
            -_utc(row["available_at"]).timestamp(), _utc(row["confirmed_at"]),
            str(row.get("source_id") or ""),
        ),
    )
    selected: dict[Any, dict[str, Any]] = {}
    for row in ordered:
        selected.setdefault(row["trading_date"], row)
    return [selected[key] for key in sorted(selected)]


def _event_move(
    bars: list[dict[str, Any]], starts_at: datetime, horizon: int, *, session: str
) -> float | None:
    if session in {"non_market", "regular"}:
        return None
    event_date = _utc(starts_at).astimezone(ZoneInfo("America/New_York")).date()
    if session == "post_market":
        before = [row for row in bars if row.get("trading_date") and row["trading_date"] <= event_date]
        after = [row for row in bars if row.get("trading_date") and row["trading_date"] > event_date]
        first_date = _next_market_date(event_date)
    else:
        before = [row for row in bars if row.get("trading_date") and row["trading_date"] < event_date]
        after = [row for row in bars if row.get("trading_date") and row["trading_date"] >= event_date]
        first_date = event_date if is_us_market_day(event_date) else _next_market_date(event_date)
    if not before or len(after) < horizon:
        return None
    expected_after = _market_dates(first_date, horizon)
    expected_before = _previous_market_date(first_date)
    if before[-1]["trading_date"] != expected_before:
        return None
    if [row["trading_date"] for row in after[:horizon]] != expected_after:
        return None
    try:
        return abs(float(after[horizon - 1]["close"]) / float(before[-1]["close"]) - 1.0)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _market_dates(first_date: Any, count: int) -> list[Any]:
    dates = []
    cursor = first_date
    while len(dates) < count:
        if is_us_market_day(cursor):
            dates.append(cursor)
        cursor += timedelta(days=1)
    return dates


def _next_market_date(value: Any) -> Any:
    cursor = value + timedelta(days=1)
    while not is_us_market_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def _previous_market_date(value: Any) -> Any:
    cursor = value - timedelta(days=1)
    while not is_us_market_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _latest_completed_market_date(reference: datetime) -> Any:
    local = _utc(reference).astimezone(ZoneInfo("America/New_York"))
    if is_us_market_day(local.date()) and local >= market_session_bounds(local.date())[1]:
        return local.date()
    return _previous_market_date(local.date())


def _event_implied_moves(connection: Any, events: list[dict[str, Any]], cutoff: datetime) -> dict[int, float | None]:
    output: dict[int, float | None] = {}
    for event in events:
        quote_cutoff = min(cutoff, _utc(event["starts_at"]))
        minimum_expiration = _minimum_event_expiration(event["starts_at"])
        rows = connection.execute(
            """WITH selected AS (
                   SELECT latest.snapshot_id, latest.capture_generation_id
                   FROM raw.option_quote latest
                   JOIN catalog.option_contract latest_contract ON latest_contract.id = latest.contract_id
                   JOIN raw.option_snapshot latest_snapshot ON latest_snapshot.id = latest.snapshot_id
                   JOIN ingest.run latest_ingestion ON latest_ingestion.id = latest_snapshot.ingest_run_id
                   LEFT JOIN raw.option_capture_generation generation
                     ON generation.id = latest.capture_generation_id
                   WHERE latest_contract.underlying_instrument_id = %s
                     AND latest.available_at <= %s AND latest.observed_at <= %s
                     AND latest.available_at >= %s - interval '4 days'
                     AND latest.observed_at >= %s - interval '4 days'
                     AND latest.observed_at < %s
                     AND latest_snapshot.capture_state = 'complete'
                     AND latest_ingestion.status IN ('succeeded', 'partial')
                     AND latest_ingestion.finished_at <= %s
                     AND (
                       latest.capture_generation_id IS NULL
                       OR (generation.capture_state = 'complete' AND generation.capture_finished_at <= %s)
                     )
                   ORDER BY latest.observed_at DESC, latest.snapshot_id DESC LIMIT 1
               ), eligible AS (
                 SELECT contract.expiration,
                        contract.strike::double precision AS strike,
                        contract.option_type, contract.multiplier,
                        quote.contract_style AS style,
                        quote.contract_settlement AS settlement,
                        quote.contract_deliverable_key AS deliverable_key,
                        quote.standard_contract_verified,
                        quote.bid, quote.ask, quote.mid, quote.underlying_price
                 FROM raw.option_quote quote
                 JOIN selected ON selected.snapshot_id = quote.snapshot_id
                   AND selected.capture_generation_id IS NOT DISTINCT FROM quote.capture_generation_id
                 JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                 WHERE contract.underlying_instrument_id = %s AND quote.available_at <= %s
                   AND quote.observed_at <= %s AND quote.observed_at < %s
                   AND contract.expiration >= %s
                   AND quote.contract_style IS NOT NULL AND quote.contract_settlement IS NOT NULL
                   AND quote.contract_deliverable_key IS NOT NULL
                   AND quote.standard_contract_verified
                   AND quote.bid > 0 AND quote.ask >= quote.bid
                   AND (quote.ask - quote.bid) / NULLIF((quote.ask + quote.bid) / 2.0, 0) <= 0.25
               ), spot AS (
                 SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY underlying_price) AS price,
                        min(underlying_price) AS low, max(underlying_price) AS high
                 FROM eligible WHERE underlying_price > 0
               ), chosen_pair AS (
                 SELECT eligible.expiration, eligible.strike, eligible.multiplier,
                        eligible.style, eligible.settlement, eligible.deliverable_key
                 FROM eligible CROSS JOIN spot
                 WHERE spot.price IS NOT NULL AND spot.high / NULLIF(spot.low, 0) - 1 <= 0.005
                 GROUP BY eligible.expiration, eligible.strike, eligible.multiplier,
                          eligible.style, eligible.settlement, eligible.deliverable_key, spot.price
                 HAVING count(*) FILTER (WHERE eligible.option_type = 'call') = 1
                    AND count(*) FILTER (WHERE eligible.option_type = 'put') = 1
                 ORDER BY eligible.expiration, abs(eligible.strike - spot.price), eligible.strike,
                          eligible.multiplier, eligible.deliverable_key
                 LIMIT 1
               )
               SELECT eligible.expiration, eligible.strike, eligible.option_type,
                      eligible.bid, eligible.ask, eligible.mid, eligible.underlying_price
               FROM eligible JOIN chosen_pair USING (
                 expiration, strike, multiplier, style, settlement, deliverable_key
               )
               ORDER BY eligible.option_type""",
            [event["instrument_id"], quote_cutoff, quote_cutoff, quote_cutoff, quote_cutoff,
             event["starts_at"], quote_cutoff, quote_cutoff,
             event["instrument_id"], quote_cutoff, quote_cutoff,
             event["starts_at"], minimum_expiration],
        ).fetchall()
        if rows:
            spots = [float(row["underlying_price"]) for row in rows if _positive(row["underlying_price"])]
            coherent = spots and max(spots) / min(spots) - 1 <= 0.005
            spot = median(spots) if coherent else None
            cost = paired_atm_straddle_cost((dict(row) for row in rows), underlying_price=spot)
            output[int(event["id"])] = cost / float(spot) if cost is not None and spot is not None else None
        else:
            output[int(event["id"])] = None
    return output


def _minimum_event_expiration(starts_at: datetime) -> Any:
    local_date = _utc(starts_at).astimezone(ZoneInfo("America/New_York")).date()
    return local_date + timedelta(days=1) if event_session(starts_at) in {"post_market", "non_market"} else local_date


def _quote_price(row: dict[str, Any]) -> float | None:
    try:
        bid, ask = float(row["bid"]), float(row["ask"])
        midpoint = (bid + ask) / 2
        if bid <= 0 or ask < bid or midpoint <= 0 or (ask - bid) / midpoint > 0.25:
            return None
        mid = float(row.get("mid") or midpoint)
        return mid if bid <= mid <= ask else midpoint
    except (KeyError, TypeError, ValueError):
        return None


def _finite(values: Iterable[float]) -> list[float]:
    output: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(number):
            output.append(number)
    return output


def _positive(value: Any) -> bool:
    try:
        return isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
