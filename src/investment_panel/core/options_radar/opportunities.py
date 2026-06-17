"""Materialize the brutally selective first-screen opportunity read model."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.calibration import (load_conviction_calibration)
from investment_panel.core.options_radar.coerce import (_average, _integer, _iso, _json, _json_or_list, _list_value, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DATA_CONTRACT_READY, DEFAULT_STRATEGY_VERSION, MIN_SNAPSHOT_PREMIUM_COVERAGE, SERVICE_BUG_TIER)
from investment_panel.core.options_radar.dbutil import (_symbol_filter)
from investment_panel.core.options_radar.opportunity_contract import (_compact_opportunity_contract, _entry_zone, _extreme_opportunity_blockers, _kill_switch, _market_cap, _opportunity_data_contract, _opportunity_top_reasons, _position_sizing_band, _revenue_growth, _why_now, tier_rank)
from investment_panel.core.options_radar.opportunity_scoring import (_opportunity_scores, load_cohort_priors)
from investment_panel.core.options_radar.regime import (_qqq_above_200d)
from investment_panel.core.options_radar.scoring import (_theme_watch_matches)
from investment_panel.core.options_radar.strategy_outcomes import (_value_counts)

def refresh_option_radar_opportunities(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
) -> int:
    """Materialize the brutally selective first-screen opportunity read model."""

    symbol_filter = _symbol_filter(symbols, table_alias="ce", column="ticker")
    read_snapshot = _radar_read_snapshot_time(con, strategy_version, symbols)
    if read_snapshot is None:
        return 0
    rows = query_rows(
        con,
        f"""
        WITH latest AS (
            SELECT TRY_CAST(? AS TIMESTAMP) AS snapshot_time
        ),
        option_snapshot_one AS (
            SELECT *
            FROM option_snapshot
            QUALIFY row_number() OVER (
                PARTITION BY contract_id, snapshot_time
                ORDER BY CASE data_source WHEN 'robinhood' THEN 0 WHEN 'ibkr' THEN 1 WHEN 'tradingview' THEN 2 WHEN 'yfinance' THEN 3 ELSE 4 END
            ) = 1
        ),
        latest_validation AS (
            SELECT *
            FROM agent_thesis_validation
            WHERE strategy_version = ?
            QUALIFY row_number() OVER (
                PARTITION BY coalesce(candidate_event_id, ticker)
                ORDER BY validated_at DESC, validation_date DESC
            ) = 1
        ),
        latest_market_context AS (
            SELECT symbol, metrics
            FROM market_screener_rows
            QUALIFY row_number() OVER (
                PARTITION BY symbol
                ORDER BY CASE source WHEN 'yfinance_info' THEN 0 WHEN 'tradingview' THEN 1 ELSE 2 END,
                         observed_at DESC
            ) = 1
        )
        SELECT
            ce.*,
            s.expiration,
            s.strike,
            s.option_type,
            s.bid,
            s.ask,
            s.volume,
            s.open_interest,
            s.iv,
            s.delta,
            s.gamma,
            s.theta,
            s.vega,
            s.dte,
            s.spread_pct,
            s.data_source,
            f.required_2x_price,
            f.required_5x_price,
            f.breakeven,
            f.iv_percentile,
            f.iv_rank,
            f.liquidity_score,
            f.convexity_score,
            sf.price,
            sf.ma_20,
            sf.ma_50,
            sf.ma_200,
            sf.rs_vs_qqq_20d,
            sf.rs_vs_qqq_60d,
            sf.atr_pct,
            sf.volume_ratio,
            sf.distance_from_52w_high,
            sf.base_length_days,
            sf.breakout_level,
            i.asset_class,
            i.name AS instrument_name,
            i.sector,
            i.industry,
            i.category,
            mc.metrics AS market_metrics,
            v.validation_id,
            v.state AS thesis_validation_state,
            v.reason AS thesis_validation_reason,
            v.proof_status,
            v.catalyst_status,
            v.invalidation_status,
            v.evidence_status,
            v.red_team_status,
            v.red_team_flags
        FROM candidate_event ce
        JOIN latest ON ce.snapshot_time = latest.snapshot_time
        LEFT JOIN option_snapshot_one s ON s.contract_id = ce.contract_id AND s.snapshot_time = ce.snapshot_time
        LEFT JOIN option_features f ON f.contract_id = ce.contract_id AND f.snapshot_time = ce.snapshot_time
        LEFT JOIN stock_features sf ON sf.ticker = ce.ticker AND sf.snapshot_time = ce.snapshot_time
        LEFT JOIN instruments i ON i.symbol = ce.ticker
        LEFT JOIN latest_market_context mc ON mc.symbol = ce.ticker
        LEFT JOIN latest_validation v
          ON (v.candidate_event_id = ce.event_id OR (v.candidate_event_id IS NULL AND v.ticker = ce.ticker))
        WHERE ce.strategy_version = ?
              AND ce.state != 'REJECT'
              {symbol_filter["sql"]}
        QUALIFY row_number() OVER (
            PARTITION BY ce.event_id
            ORDER BY CASE WHEN v.candidate_event_id = ce.event_id THEN 0 ELSE 1 END,
                     v.validated_at DESC NULLS LAST
        ) = 1
        ORDER BY ce.ticker, ce.score DESC, ce.contract_id
        """,
        [read_snapshot, strategy_version, strategy_version, *symbol_filter["params"]],
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_normalize_symbol(row.get("ticker"))].append(row)

    cohort_priors = load_cohort_priors(con, strategy_version)
    built = [
        opportunity
        for ticker, candidate_rows in grouped.items()
        if (opportunity := build_option_radar_opportunity(con, ticker, candidate_rows, strategy_version, cohort_priors=cohort_priors))
    ]
    # Preserve the last-good opportunities if the latest snapshot built none — a
    # single bad snapshot (e.g. an off-hours pull of all near-term/REJECT
    # contracts) must not blank the radar. Only replace when there is a fresh set.
    if not built:
        return 0

    if symbols:
        clean_symbols = [_normalize_symbol(symbol) for symbol in symbols if symbol]
        placeholders = ", ".join("?" for _ in clean_symbols)
        con.execute(
            f"DELETE FROM option_radar_opportunity WHERE strategy_version = ? AND ticker IN ({placeholders})",
            [strategy_version, *clean_symbols],
        )
    else:
        con.execute("DELETE FROM option_radar_opportunity WHERE strategy_version = ?", [strategy_version])

    count = 0
    for opportunity in built:
        con.execute(
            """
            INSERT OR REPLACE INTO option_radar_opportunity
            (opportunity_id, snapshot_time, ticker, strategy_version, tier,
             primary_event_id, primary_contract_id, primary_state,
             conviction_score, asymmetry_score, entry_quality_score,
             catalyst_score, evidence_score, regime_score, survivability_score,
             learning_score, required_move_pct, premium_mid,
             premium_fill_assumption, required_10x_price, buy_under,
             entry_zone, max_loss_assumption, position_sizing_band,
             data_contract_status, data_contract_failures, data_contract_satisfied,
             service_repair_jobs, service_repair_summary,
             why_now, kill_switch, top_reasons, blockers, quality_status,
             quality_flags, evidence_refs, alternative_contracts, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                opportunity["opportunity_id"],
                opportunity["snapshot_time"],
                opportunity["ticker"],
                opportunity["strategy_version"],
                opportunity["tier"],
                opportunity["primary_event_id"],
                opportunity["primary_contract_id"],
                opportunity["primary_state"],
                opportunity["conviction_score"],
                opportunity["asymmetry_score"],
                opportunity["entry_quality_score"],
                opportunity["catalyst_score"],
                opportunity["evidence_score"],
                opportunity["regime_score"],
                opportunity["survivability_score"],
                opportunity["learning_score"],
                opportunity["required_move_pct"],
                opportunity["premium_mid"],
                opportunity["premium_fill_assumption"],
                opportunity["required_10x_price"],
                opportunity["buy_under"],
                opportunity["entry_zone"],
                opportunity["max_loss_assumption"],
                opportunity["position_sizing_band"],
                opportunity["data_contract_status"],
                json_dumps(opportunity["data_contract_failures"]),
                json_dumps(opportunity["data_contract_satisfied"]),
                json_dumps(opportunity["service_repair_jobs"]),
                opportunity["service_repair_summary"],
                opportunity["why_now"],
                opportunity["kill_switch"],
                json_dumps(opportunity["top_reasons"]),
                json_dumps(opportunity["blockers"]),
                opportunity["quality_status"],
                json_dumps(opportunity["quality_flags"]),
                json_dumps(opportunity["evidence_refs"]),
                json_dumps(opportunity["alternative_contracts"]),
                json_dumps(opportunity["raw"]),
            ],
        )
        count += 1
    return count


def build_option_radar_opportunity(
    con: Any,
    ticker: str,
    candidate_rows: list[dict[str, Any]],
    strategy_version: str,
    *,
    cohort_priors: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    snapshot_time = _iso(candidate_rows[0].get("snapshot_time")) if candidate_rows else ""
    source_context = _source_signal_context(con, ticker, snapshot_time)
    qqq_above = _qqq_above_200d(con, snapshot_time, {})
    calibration = load_conviction_calibration(con, strategy_version)
    if cohort_priors is None:
        cohort_priors = load_cohort_priors(con, strategy_version)
    details = [_opportunity_candidate_detail(row, source_context=source_context, qqq_above_200d=qqq_above, calibration=calibration, cohort_priors=cohort_priors) for row in candidate_rows]
    details = [detail for detail in details if detail]
    if not details:
        return None
    details.sort(key=lambda item: (tier_rank(str(item["tier"])), -float(item["conviction_score"]), _number(item.get("required_move_pct")) or 99.0))
    primary = details[0]
    snapshot_time = _iso(primary["snapshot_time"])
    alternatives = [_compact_opportunity_contract(detail) for detail in details[1:6]]
    return {
        "opportunity_id": stable_id("option_radar_opportunity", strategy_version, snapshot_time, ticker),
        "snapshot_time": snapshot_time,
        "ticker": ticker,
        "strategy_version": strategy_version,
        "tier": primary["tier"],
        "primary_event_id": primary["event_id"],
        "primary_contract_id": primary["contract_id"],
        "primary_state": primary["state"],
        "conviction_score": primary["conviction_score"],
        "asymmetry_score": primary["asymmetry_score"],
        "entry_quality_score": primary["entry_quality_score"],
        "catalyst_score": primary["catalyst_score"],
        "evidence_score": primary["evidence_score"],
        "regime_score": primary["regime_score"],
        "survivability_score": primary["survivability_score"],
        "learning_score": primary["learning_score"],
        "required_move_pct": primary["required_move_pct"],
        "premium_mid": primary["premium_mid"],
        "premium_fill_assumption": primary["premium_fill_assumption"],
        "required_10x_price": primary["required_10x_price"],
        "buy_under": primary["buy_under"],
        "entry_zone": primary["entry_zone"],
        "max_loss_assumption": primary["max_loss_assumption"],
        "position_sizing_band": primary["position_sizing_band"],
        "data_contract_status": primary["data_contract_status"],
        "data_contract_failures": primary["data_contract_failures"],
        "data_contract_satisfied": primary["data_contract_satisfied"],
        "service_repair_jobs": primary["service_repair_jobs"],
        "service_repair_summary": primary["service_repair_summary"],
        "why_now": primary["why_now"],
        "kill_switch": primary["kill_switch"],
        "top_reasons": primary["top_reasons"],
        "blockers": primary["blockers"],
        "quality_status": primary["quality_status"],
        "quality_flags": primary["quality_flags"],
        "evidence_refs": primary["evidence_refs"],
        "alternative_contracts": alternatives,
        "raw": {
            "authority": "deterministic_extreme_options_radar",
            "queue_policy": "one_primary_contract_per_ticker",
            "exceptional_policy": "data_contract_must_be_ready_before_trade_decision",
            "candidate_count": len(details),
            "tier_counts": _value_counts([str(detail["tier"]) for detail in details]),
            "data_contract_status": primary["data_contract_status"],
            "data_contract_failures": primary["data_contract_failures"],
            "service_repair_jobs": primary["service_repair_jobs"],
            "primary_detail": _compact_opportunity_contract(primary),
        },
    }


def _snapshot_premium_coverage(con: Any, snapshot_time: str, *, symbols: list[str] | None = None) -> float | None:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    rows = query_rows(
        con,
        f"""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE s.mid IS NOT NULL AND s.mid > 0) AS with_premium
        FROM option_snapshot s
        WHERE s.snapshot_time = TRY_CAST(? AS TIMESTAMP) {symbol_filter["sql"]}
        """,
        [snapshot_time, *symbol_filter["params"]],
    )
    total = _number(rows[0].get("total")) if rows else None
    if not total:
        return None
    with_premium = _number(rows[0].get("with_premium")) or 0.0
    return with_premium / total


def _radar_read_snapshot_time(con: Any, strategy_version: str, symbols: list[str] | None = None) -> str | None:
    symbol_filter = _symbol_filter(symbols, table_alias="ce", column="ticker")
    rows = query_rows(
        con,
        f"""
        SELECT DISTINCT ce.snapshot_time
        FROM candidate_event ce
        WHERE ce.strategy_version = ? {symbol_filter["sql"]}
        ORDER BY ce.snapshot_time DESC
        LIMIT 20
        """,
        [strategy_version, *symbol_filter["params"]],
    )
    snapshots = [_iso(row.get("snapshot_time")) for row in rows if row.get("snapshot_time")]
    for snapshot in snapshots:
        coverage = _snapshot_premium_coverage(con, snapshot, symbols=symbols)
        if coverage is None or coverage >= MIN_SNAPSHOT_PREMIUM_COVERAGE:
            return snapshot
    return snapshots[0] if snapshots else None


def _opportunity_candidate_detail(
    row: dict[str, Any],
    *,
    source_context: dict[str, Any],
    qqq_above_200d: bool | None,
    calibration: dict[str, Any] | None = None,
    cohort_priors: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot_time = _iso(row.get("snapshot_time"))
    raw = _json(row.get("raw"))
    candidate_blockers = [str(item) for item in raw.get("blockers", []) if item] if isinstance(raw.get("blockers"), list) else []
    hard_rejects = [str(item) for item in raw.get("hard_rejects", []) if item] if isinstance(raw.get("hard_rejects"), list) else []
    positives = [str(item) for item in raw.get("positives", []) if item] if isinstance(raw.get("positives"), list) else []
    validation = _opportunity_validation(row)
    scores = _opportunity_scores(row, validation=validation, source_context=source_context, qqq_above_200d=qqq_above_200d, calibration=calibration, cohort_priors=cohort_priors)
    blockers = _extreme_opportunity_blockers(row, validation=validation, source_context=source_context, qqq_above_200d=qqq_above_200d, scores=scores)
    data_contract = _opportunity_data_contract(row, validation=validation, source_context=source_context, qqq_above_200d=qqq_above_200d)
    conviction = scores["conviction_score"]
    if conviction < 78.0 and "conviction_below_exceptional_bar" not in blockers:
        blockers.append("conviction_below_exceptional_bar")
    state = str(row.get("state") or "").upper()
    if data_contract["status"] != DATA_CONTRACT_READY:
        tier = SERVICE_BUG_TIER
    elif not blockers and state == "FIRE":
        tier = "Exceptional"
    elif state in {"FIRE", "SETUP"}:
        tier = "Research"
    else:
        tier = "Watch"
    top_reasons = _opportunity_top_reasons(row, validation=validation, source_context=source_context, qqq_above_200d=qqq_above_200d, scores=scores)
    evidence_refs = [{"type": "candidate_event", "id": row.get("event_id")}]
    if validation.get("validation_id"):
        evidence_refs.append({"type": "agent_thesis_validation", "id": validation["validation_id"]})
    evidence_refs.extend(source_context["evidence_refs"][:5])
    premium_fill = _number(row.get("premium_fill_assumption"))
    watch_themes = _theme_watch_matches(row)
    return {
        "event_id": row.get("event_id"),
        "snapshot_time": snapshot_time,
        "ticker": _normalize_symbol(row.get("ticker")),
        "contract_id": row.get("contract_id"),
        "state": state,
        "tier": tier,
        "conviction_score": conviction,
        "asymmetry_score": scores["asymmetry_score"],
        "entry_quality_score": scores["entry_quality_score"],
        "catalyst_score": scores["catalyst_score"],
        "evidence_score": scores["evidence_score"],
        "regime_score": scores["regime_score"],
        "survivability_score": scores["survivability_score"],
        "learning_score": scores["learning_score"],
        "required_move_pct": _number(row.get("required_move_pct")),
        "premium_mid": _number(row.get("premium_mid")),
        "premium_fill_assumption": premium_fill,
        "required_10x_price": _number(row.get("required_10x_price")),
        "buy_under": _number(row.get("buy_under")),
        "entry_zone": _entry_zone(row),
        "max_loss_assumption": premium_fill,
        "position_sizing_band": _position_sizing_band(tier),
        "data_contract_status": data_contract["status"],
        "data_contract_failures": data_contract["failures"],
        "data_contract_satisfied": data_contract["satisfied"],
        "service_repair_jobs": data_contract["repair_jobs"],
        "service_repair_summary": data_contract["summary"],
        "why_now": _why_now(top_reasons, blockers, data_contract=data_contract),
        "kill_switch": "Fix the data contract bug before computing a trade decision." if data_contract["status"] != DATA_CONTRACT_READY else _kill_switch(row, validation),
        "top_reasons": top_reasons,
        "blockers": blockers,
        "quality_status": str(row.get("quality_status") or "ok").lower(),
        "quality_flags": _list_value(row.get("quality_flags")),
        "evidence_refs": evidence_refs,
        "raw": {
            "candidate_blockers": candidate_blockers,
            "hard_rejects": hard_rejects,
            "positives": positives,
            "source_signal_count": source_context["count"],
            "source_signal_confidence": source_context["average_confidence"],
            "source_signal_score": source_context["score"],
            "thesis_validation_state": validation.get("state"),
            "watch_themes": watch_themes,
            "qqq_above_200d": qqq_above_200d,
            "data_source": row.get("data_source"),
            "asset_class": row.get("asset_class"),
            "instrument_name": row.get("instrument_name"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "category": row.get("category"),
            "market_cap": _market_cap(row),
            "revenue_growth": _revenue_growth(row),
            "expiration": str(row.get("expiration") or ""),
            "strike": _number(row.get("strike")),
            "option_type": row.get("option_type"),
            "dte": _integer(row.get("dte")),
            "spread_pct": _number(row.get("spread_pct")),
            "open_interest": _number(row.get("open_interest")),
            "volume": _number(row.get("volume")),
            "iv_percentile": _number(row.get("iv_percentile")),
            # Greeks for the contract, so the opportunity detail / alternative
            # contracts show what the trader buys (mirrors candidate_event.raw).
            "delta": _number(row.get("delta")),
            "gamma": _number(row.get("gamma")),
            "theta": _number(row.get("theta")),
            "vega": _number(row.get("vega")),
            "iv": _number(row.get("iv")),
            # The differentiating signals the trader actually decides on — EV asymmetry
            # (per-ticker, unlike the generic payoff shape) and catalyst proximity.
            "ev": raw.get("ev") if isinstance(raw.get("ev"), dict) else None,
            "days_to_earnings": raw.get("days_to_earnings"),
        },
    }


def _source_signal_context(con: Any, ticker: str, snapshot_time: str) -> dict[str, Any]:
    rows = query_rows(
        con,
        """
        SELECT source_item_id, source_id, observed_at, signal_type, sentiment,
               direction, confidence, thesis, antithesis, catalysts, risks,
               invalidation, evidence_refs
        FROM ticker_source_signals
        WHERE symbol = ? AND observed_at <= TRY_CAST(? AS TIMESTAMP)
        ORDER BY observed_at DESC, confidence DESC NULLS LAST
        LIMIT 8
        """,
        [ticker, snapshot_time],
    )
    confidences = [_number(row.get("confidence")) for row in rows]
    clean_confidences = [value for value in confidences if value is not None]
    catalyst_count = 0
    evidence_refs: list[dict[str, Any]] = []
    for row in rows:
        catalysts = _json_or_list(row.get("catalysts"))
        if isinstance(catalysts, list):
            catalyst_count += len([item for item in catalysts if item])
        elif catalysts:
            catalyst_count += 1
        refs = _json_or_list(row.get("evidence_refs"))
        if isinstance(refs, list):
            evidence_refs.extend([ref for ref in refs if isinstance(ref, dict)])
        if row.get("source_item_id"):
            evidence_refs.append({"type": "source_item", "id": row.get("source_item_id"), "source": row.get("source_id")})
    average_confidence = _average(clean_confidences) if clean_confidences else None
    score = min(100.0, len(rows) * 18.0 + catalyst_count * 8.0 + (average_confidence or 0.0) * 35.0)
    return {
        "count": len(rows),
        "catalyst_count": catalyst_count,
        "average_confidence": average_confidence,
        "score": round(score, 2),
        "evidence_refs": evidence_refs,
    }


def _opportunity_validation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "validation_id": row.get("validation_id"),
        "state": str(row.get("thesis_validation_state") or "").lower(),
        "reason": row.get("thesis_validation_reason"),
        "proof_status": str(row.get("proof_status") or "").lower(),
        "catalyst_status": str(row.get("catalyst_status") or "").lower(),
        "invalidation_status": str(row.get("invalidation_status") or "").lower(),
        "evidence_status": str(row.get("evidence_status") or "").lower(),
        "red_team_status": str(row.get("red_team_status") or "").lower(),
        "red_team_flags": _list_value(row.get("red_team_flags")),
    }
