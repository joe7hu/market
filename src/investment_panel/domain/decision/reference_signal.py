"""Account-independent trading conditions from the canonical daily-trend feature.

This is a presentation of a measured trend, not a calibrated alpha forecast or
an order authorization. The funded-paper path still requires its model, risk,
liquidity and causal-fill contracts. Never manufacture odds to fill a card.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_panel.domain.decision.assessment import assessment_quote, timestamp
from investment_panel.domain.decision.calendar import (
    latest_completed_market_day, is_us_market_day, market_session_bounds, MARKET_TZ,
)

REFERENCE_SIGNAL_VERSION = "daily-trend-conditions.v1"


class ReferenceSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = REFERENCE_SIGNAL_VERSION
    signal_id: str
    ticker: str
    action: Literal["BUY_SETUP", "EXIT_SETUP", "HOLD", "WAIT", "AVOID", "SERVICE_FAILURE"]
    summary: str
    condition: str
    evidence_kind: Literal["measured_trend_not_validated_alpha"] = "measured_trend_not_validated_alpha"
    order_authorized: Literal[False] = False
    as_of: datetime
    expires_at: datetime
    quote_state: str
    quote_observed_at: datetime | None = None
    feature_session: date | None = None
    source_revision: str | None = None
    reference_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    entry_low: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    entry_high: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    stop_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    target_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    risk_per_unit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    horizon: str = "Tactical · next 1–20 sessions"
    failure_code: str | None = None
    owner_job: str | None = None

    @model_validator(mode="after")
    def complete_action(self) -> "ReferenceSignal":
        if any(value.tzinfo is None for value in (self.as_of, self.expires_at)) or self.expires_at <= self.as_of:
            raise ValueError("Signal requires aware, ordered publication/expiry clocks")
        if self.action == "SERVICE_FAILURE":
            if not self.owner_job or not self.failure_code:
                raise ValueError("Service failure requires a named code and producer")
            if any(value is not None for value in (self.entry_low, self.entry_high, self.stop_price, self.target_price, self.risk_per_unit)):
                raise ValueError("Failed signals must not expose actionable terms")
        else:
            if not self.source_revision or self.feature_session is None or self.quote_observed_at is None:
                raise ValueError("Trading conditions require feature and quote provenance")
            if self.quote_observed_at.tzinfo is None or self.quote_observed_at > self.as_of:
                raise ValueError("Signal quote observation must be causal")
            if self.failure_code or self.owner_job:
                raise ValueError("A valid signal cannot also report a producer failure")
        if self.action == "BUY_SETUP":
            if any(value is None for value in (self.entry_low, self.entry_high, self.stop_price, self.target_price, self.risk_per_unit)):
                raise ValueError("BUY_SETUP requires all entry, invalidation, objective and risk terms")
            if not self.stop_price < self.entry_low <= self.entry_high < self.target_price:
                raise ValueError("Long setup price conditions must be ordered")
        return self


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _session(feature: Mapping[str, Any]) -> date | None:
    metrics = feature.get("metrics") or {}
    value = feature.get("as_of_date") or metrics.get("as_of_date") or metrics.get("last_bar_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def reference_expiry(now: datetime, *, continuous: bool) -> datetime:
    if continuous:
        return now + timedelta(minutes=15)
    local = now.astimezone(MARKET_TZ)
    if is_us_market_day(local.date()):
        opened, closed = market_session_bounds(local.date())
        if opened <= now < closed:
            return min(now + timedelta(minutes=15), closed.astimezone(UTC))
    # A closed-session condition remains inspectable until the next opening,
    # not executable from that old price. New conditions must be published then.
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if is_us_market_day(day):
            opened, _ = market_session_bounds(day)
            if opened > now:
                return opened.astimezone(UTC)
    return now


def build_reference_signal(
    ticker: str, *, quote: Mapping[str, Any], feature: Mapping[str, Any] | None,
    now: datetime, owned: bool = False, continuous: bool = False,
) -> ReferenceSignal:
    """Publish all conditions or a named producer failure; never partial terms.

    Rule v1: measured uptrend has a reference-entry band of +/- 0.25 ATR,
    invalidation 2 ATR below the completed close and an objective 4 ATR above.
    These distances are a *policy*, not an estimated probability/expected return.
    A downtrend means exit assessment for an existing long, otherwise avoid.
    Range/transition means wait (or hold an existing long), not a missing value.
    """
    now = now.astimezone(UTC)
    assessed = assessment_quote(quote, now=now, continuous=continuous)
    feature = feature or {}
    session = _session(feature)
    identity = sha256(json.dumps({"ticker": ticker, "version": REFERENCE_SIGNAL_VERSION,
        "quote_observed_at": assessed.observed_at, "feature_revision": feature.get("revision"),
        "as_of": now, "owned": owned}, sort_keys=True, default=str).encode()).hexdigest()[:24]
    base = dict(signal_id=identity, ticker=ticker, as_of=now,
        expires_at=reference_expiry(now, continuous=continuous), quote_state=assessed.state,
        quote_observed_at=assessed.observed_at, feature_session=session,
        source_revision=str(feature.get("revision") or "") or None,
        reference_price=assessed.price if assessed.usable else None,
        horizon="Tactical · next 1–20 days" if continuous else "Tactical · next 1–20 sessions")
    def fault(code: str, summary: str, job: str) -> ReferenceSignal:
        return ReferenceSignal(**base, action="SERVICE_FAILURE", failure_code=code,
            summary=summary, condition="New allocations paused for this instrument; see the named producer in System health.",
            owner_job=job)
    if not assessed.usable:
        return fault("assessment_quote_invalid", assessed.reason, "refresh_assessment_inputs")
    expected = (now.date() - timedelta(days=1)) if continuous else latest_completed_market_day(now)
    if not feature or feature.get("data_quality_status") != "complete":
        reasons = [str(item) for item in feature.get("reason_codes") or []]
        source_gap = any("history" in item or "bar" in item for item in reasons)
        details = "; ".join(reasons) or "no completed feature was published"
        return fault("trend_feature_incomplete", f"Daily-trend input contract failed: {details}.",
                     "update_market_data" if source_gap else "refresh_symbol_features")
    if not base["source_revision"]:
        return fault("trend_feature_provenance_missing", "Daily-trend feature has no persisted source revision.", "refresh_symbol_features")
    available, cutoff = timestamp(feature.get("available_at")), timestamp(feature.get("as_of"))
    if available is None or cutoff is None or not cutoff <= available <= now or session != expected:
        return fault("trend_feature_not_current", "Daily-trend feature does not cover the latest completed session.", "refresh_symbol_features")
    price, atr_pct = finite(feature.get("price")), finite(feature.get("atr_pct"))
    # ATR is stored as a fraction of close (0.02 means 2%).
    if price is None or atr_pct is None or price <= 0 or not 0 < atr_pct < .5:
        return fault("trend_risk_inputs_invalid", "Daily-trend producer returned invalid price or ATR risk inputs.", "refresh_symbol_features")
    atr = price * atr_pct
    stop, target = price - 2 * atr, price + 4 * atr
    state = str(feature.get("trend_state") or "")
    if state == "trend_up":
        low, high = price - .25 * atr, price + .25 * atr
        if assessed.price is not None and assessed.price <= stop:
            return ReferenceSignal(**base, action="EXIT_SETUP" if owned else "AVOID",
                summary="The uptrend's price invalidation has been crossed.",
                condition=f"Do not add a long below {stop:.4f}; recalculate after the next completed session.", stop_price=stop)
        return ReferenceSignal(**base, action="HOLD" if owned else "BUY_SETUP",
            summary="Measured daily uptrend; use the stated entry and invalidation, not a market chase.",
            condition=f"{'Hold above' if owned else 'Consider a long only inside'} {stop:.4f}" if owned else
                      f"Entry {low:.4f}–{high:.4f}; abandon below {stop:.4f}; objective {target:.4f}.",
            entry_low=low, entry_high=high, stop_price=stop, target_price=target,
            risk_per_unit=high-stop)
    if state == "trend_down":
        return ReferenceSignal(**base, action="EXIT_SETUP" if owned else "AVOID",
            summary="Measured daily downtrend; no new long setup.",
            condition="Reduce or exit the existing long under its approved exit policy." if owned else
                      "Stay out of new longs until the daily trend recovers; no short order is implied.")
    if state in {"range", "transition", "range_bound", "choppy"}:
        return ReferenceSignal(**base, action="HOLD" if owned else "WAIT",
            summary="No directional trend setup in the completed-session evidence.",
            condition="Keep the existing position's approved stop; do not add." if owned else
                      "Stay in cash for this setup. Re-evaluate when the daily-trend producer confirms a direction.")
    return fault("trend_state_invalid", "Daily-trend producer returned an unsupported state.", "refresh_symbol_features")


def project_reference_signal(signal: ReferenceSignal | Mapping[str, Any] | None, *, now: datetime) -> ReferenceSignal | None:
    """A read may revoke expired terms; it must not mint a new decision/price."""
    if signal is None:
        return None
    model = signal if isinstance(signal, ReferenceSignal) else ReferenceSignal.model_validate(signal)
    if model.as_of <= now < model.expires_at:
        return model
    return model.model_copy(update={"action": "SERVICE_FAILURE", "failure_code": "decision_publication_overdue",
        "summary": "Trading conditions expired before a replacement was published.",
        "condition": "New allocations paused for this instrument; decision publisher must deliver a current revision.",
        "owner_job": "refresh_decision_models", "entry_low": None, "entry_high": None,
        "stop_price": None, "target_price": None, "risk_per_unit": None})


__all__ = ["ReferenceSignal", "build_reference_signal", "project_reference_signal", "reference_expiry"]
