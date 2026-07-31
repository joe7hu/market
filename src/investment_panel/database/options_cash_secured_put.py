"""Complete cash-secured-put strategy lane for the live options publication."""

from __future__ import annotations

from typing import Any

from investment_panel.analysis.cash_secured_put import CashSecuredPutInputs, evaluate_cash_secured_put
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


FEATURE_VERSION = "option-professional-v3-ticket"
DEFAULT_PARAMETERS = {
    "min_dte": 21,
    "max_dte": 60,
    "delta_min": 0.15,
    "delta_max": 0.30,
    "max_ticker_nav_pct": 0.05,
    "max_aggregate_nav_pct": 0.15,
}


def insert_cash_secured_put_decisions(
    runtime: DatabaseRuntime,
    repository: AnalysisRepository,
    run_id: Any,
    strategy_id: int,
    parameters: dict[str, Any],
    calibrated_ready: set[str],
) -> int:
    """Select, gate, size, score, and persist the cash-secured-put lane."""

    csp = dict(parameters.get("cash_secured_put") or DEFAULT_PARAMETERS)
    min_dte = int(csp.get("min_dte", 21))
    max_dte = int(csp.get("max_dte", 60))
    delta_min = float(csp.get("delta_min", 0.15))
    delta_max = float(csp.get("delta_max", 0.30))
    max_ticker_nav_pct = float(csp.get("max_ticker_nav_pct", 0.05))
    with runtime.read(JOB_PROFILE) as connection:
        account = connection.execute(
            """
            SELECT net_liquidation, cash_balance, buying_power, observed_at
            FROM raw.broker_account_snapshot
            ORDER BY observed_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        rows = connection.execute(
            """
            SELECT feature.snapshot_id, feature.contract_id, feature.quote_observed_at,
                   feature.dte, feature.spread_pct, feature.liquidity_score,
                   quote.underlying_price, quote.bid, quote.ask, quote.provider_iv,
                   quote.provider_delta, quote.open_interest, quote.volume,
                   contract.strike, contract.expiration, contract.multiplier,
                   instrument.id AS instrument_id, instrument.symbol, instrument.asset_class,
                   instrument.category,
                   EXISTS (
                       SELECT 1 FROM raw.fundamental_observation fundamental
                       WHERE fundamental.instrument_id = instrument.id
                   ) AS has_fundamentals,
                   (SELECT fundamental.values FROM raw.fundamental_observation fundamental
                    WHERE fundamental.instrument_id = instrument.id
                    ORDER BY fundamental.observed_at DESC, fundamental.id DESC LIMIT 1) AS quality_values,
                   (SELECT count(*) FROM raw.price_bar bar
                    WHERE bar.instrument_id = instrument.id AND bar.interval = '1d') AS history_observations,
                   EXISTS (
                       SELECT 1 FROM raw.market_event event
                       WHERE event.instrument_id = instrument.id AND event.event_kind = 'earnings'
                         AND event.starts_at::date > feature.quote_observed_at::date
                         AND event.starts_at::date <= contract.expiration
                   ) AS earnings_before_expiry
            FROM analysis.option_feature feature
            JOIN raw.option_quote quote
              ON quote.snapshot_id = feature.snapshot_id
             AND quote.contract_id = feature.contract_id
             AND quote.observed_at = feature.quote_observed_at
            JOIN catalog.option_contract contract ON contract.id = feature.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            WHERE feature.run_id = %s AND contract.option_type = 'put'
              AND feature.dte BETWEEN %s AND %s
              AND ABS(quote.provider_delta) BETWEEN %s AND %s
            ORDER BY feature.liquidity_score DESC, feature.contract_id
            """,
            [run_id, min_dte, max_dte, delta_min, delta_max],
        ).fetchall()

    created = 0
    for raw_row in rows:
        row = dict(raw_row)
        if _hard_blockers(row):
            continue
        evaluation = evaluate_cash_secured_put(
            CashSecuredPutInputs(
                spot=_float(row.get("underlying_price")) or 0,
                strike=_float(row.get("strike")) or 0,
                dte=int(row.get("dte") or 0),
                bid=_float(row.get("bid")) or 0,
                ask=_float(row.get("ask")) or 0,
                delta=_float(row.get("provider_delta")) or 0,
                multiplier=int(row.get("multiplier") or 100),
                annualized_volatility=_float(row.get("provider_iv")),
            )
        )
        if evaluation is None:
            continue

        net_liquidation = _float(account.get("net_liquidation")) if account else None
        cash_balance = _float(account.get("cash_balance")) if account else None
        buying_power = _float(account.get("buying_power")) if account else None
        available_values = [value for value in (cash_balance, buying_power) if value is not None]
        available_cash = min(available_values) if available_values else None
        sizing_blockers = _sizing_blockers(
            evaluation.secured_cash,
            net_liquidation,
            available_cash,
            max_ticker_nav_pct,
        )
        max_contracts = 0
        if net_liquidation and available_cash is not None:
            max_contracts = max(
                0,
                int(min(available_cash, net_liquidation * max_ticker_nav_pct) // evaluation.secured_cash),
            )

        spread_pct = _float(row.get("spread_pct"))
        tail_ratio = evaluation.tail_cvar / evaluation.secured_cash
        expected_value = evaluation.entry_credit * (1 - evaluation.probability_assignment) - evaluation.tail_cvar * 0.05
        risk_adjusted = expected_value / evaluation.secured_cash
        score = max(
            0.0,
            min(
                100.0,
                0.45 * float(row.get("liquidity_score") or 0)
                + 35.0 * min(evaluation.annualized_return_on_collateral, 1.0)
                + 20.0 * (1.0 - min(tail_ratio, 1.0)),
            ),
        )
        details = {
            **evaluation.as_dict(),
            "contract_version": 3,
            "feature_version": FEATURE_VERSION,
            "probability_semantics": "provisional_uncalibrated",
            "provider_local_quote": True,
            "max_contracts": max_contracts,
            "available_cash": available_cash,
            "net_liquidation": net_liquidation,
            "management_plan": {
                "profit_review_pct": 0.50,
                "mandatory_review_dte": 21,
                "assignment_requires_quality_pass": True,
                "automatic_roll": False,
            },
            "quality_basis": {
                "fundamentals_present": bool(row.get("has_fundamentals")),
                "history_observations": int(row.get("history_observations") or 0),
                "earnings_before_expiry": False,
            },
        }
        repository.store_option_decision(
            run_id,
            decision_key=f"cash-secured-put:{row['contract_id']}",
            instrument_id=int(row["instrument_id"]),
            contract_id=int(row["contract_id"]),
            snapshot_id=int(row["snapshot_id"]),
            quote_observed_at=row["quote_observed_at"],
            state="READY" if "cash_secured_put" in calibrated_ready else "SETUP",
            score=round(score, 2),
            rank=None,
            inputs={"structure": "cash_secured_put", "row": row, "evaluation": details},
            reasons=("acceptable_assignment_entry", "cash_secured_income", "liquidity_supported"),
            blockers=sizing_blockers,
            details={
                "quality_status": "complete" if not sizing_blockers else "sizing_blocked",
                "premium_mid": row.get("bid"),
                "fill_assumption": row.get("bid"),
                "structure": "cash_secured_put",
                "entry_price": row.get("bid"),
                "exit_cost_estimate": max(0.0, (_float(row.get("ask")) or 0) - (_float(row.get("bid")) or 0)),
                "secured_cash": evaluation.secured_cash,
                "max_profit": evaluation.max_profit,
                "max_loss": evaluation.max_loss,
                "break_even": evaluation.break_even,
                "effective_assignment_price": evaluation.effective_assignment_price,
                "probability_profit": evaluation.probability_profit,
                "probability_assignment": evaluation.probability_assignment,
                "probability_touch": evaluation.probability_touch,
                "expected_value": expected_value,
                "risk_adjusted_expectancy": risk_adjusted,
                "tail_cvar": evaluation.tail_cvar,
                "data_confidence": 0.65,
                "execution_confidence": max(0.0, 1.0 - (spread_pct or 1.0)),
                "details": details,
            },
            strategy_revision_id=strategy_id,
        )
        created += 1
    return created


def _hard_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not row.get("has_fundamentals") and str(row.get("asset_class") or "") != "etf":
        blockers.append("missing_quality_evidence")
    quality_values = dict(row.get("quality_values") or {})
    if str(quality_values.get("quality_status") or "").lower() in {"bad", "rejected", "unsafe"}:
        blockers.append("company_quality_rejected")
    if int(row.get("history_observations") or 0) < 60:
        blockers.append("insufficient_price_history")
    if row.get("earnings_before_expiry"):
        blockers.append("earnings_before_expiry")
    if float(row.get("open_interest") or 0) < 50:
        blockers.append("open_interest_too_low")
    spread_pct = _float(row.get("spread_pct"))
    if spread_pct is None or spread_pct > 0.25:
        blockers.append("spread_too_wide")
    return blockers


def _sizing_blockers(
    secured_cash: float,
    net_liquidation: float | None,
    available_cash: float | None,
    max_ticker_nav_pct: float,
) -> list[str]:
    blockers: list[str] = []
    if net_liquidation is None or available_cash is None:
        blockers.append("missing_cash_context")
    elif secured_cash > available_cash:
        blockers.append("insufficient_cash_collateral")
    if net_liquidation and secured_cash / net_liquidation > max_ticker_nav_pct:
        blockers.append("one_contract_exceeds_ticker_limit")
    return blockers


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
