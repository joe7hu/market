"""Typed, deterministic Phase 3 strategy specifications and daily signals."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


MECHANISM_CLASSES = (
    "trend_underreaction",
    "gap_regime",
    "event_propagation",
    "options_recovery",
)
MANIFEST_PARTS = ("source", "data", "cost", "capacity", "failure")


def strategy_family_for_key(strategy_key: str, mechanism_class: str = "", name: str = "", strategy_family: str = "") -> str:
    if "martingale" in f"{strategy_key} {mechanism_class} {name} {strategy_family}".casefold():
        return "martingale"
    return "legacy"


def is_martingale_family(strategy_key: str, mechanism_class: str = "", name: str = "", strategy_family: str = "") -> bool:
    return strategy_family_for_key(strategy_key, mechanism_class, name, strategy_family) == "martingale"


class StrategySpec(BaseModel):
    """One versioned strategy definition shared by all research families."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_key: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    revision: int = Field(ge=1)
    name: str = Field(min_length=1)
    mechanism_class: str = Field(min_length=1)
    economic_mechanism: str = Field(min_length=1)
    falsification_rule: str = Field(min_length=1)
    source_definition_version: str = Field(min_length=1)
    strategy_family: str = "legacy"
    promotability: str = "standard"
    actionability: str = "daily_research"
    manifest: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_definition(self) -> "StrategySpec":
        if self.mechanism_class not in MECHANISM_CLASSES and self.mechanism_class not in {"crypto_basis", "flow_supporting", "martingale"}:
            raise ValueError("unknown strategy mechanism class")
        if not self.strategy_key.endswith(f"_v{self.revision}"):
            raise ValueError("strategy key must include its source revision")
        if set(self.manifest) != set(MANIFEST_PARTS):
            raise ValueError("strategy manifest requires source, data, cost, capacity, and failure parts")
        if any(not isinstance(self.manifest[key], Mapping) or not self.manifest[key] for key in MANIFEST_PARTS):
            raise ValueError("strategy manifest parts must be non-empty objects")
        if self.promotability not in {"standard", "negative_control", "registration_only", "exposure_sleeve"}:
            raise ValueError("unknown strategy promotability")
        if self.promotability == "negative_control" and not is_martingale_family(self.strategy_key, self.mechanism_class, self.name, self.strategy_family):
            raise ValueError("only the martingale strategy family may be a permanent negative control")
        if is_martingale_family(self.strategy_key, self.mechanism_class, self.name, self.strategy_family) and (
            self.promotability != "negative_control" or self.actionability != "research_only"
        ):
            raise ValueError("Martingale variants are permanent research-only negative controls")
        return self

    @property
    def versioned_key(self) -> str:
        return self.strategy_key


class StrategySignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_key: str
    status: str
    value: float | None = None
    direction: str | None = None
    horizon: str = "daily"
    actionability: str = "daily_research"
    regime: str | None = None
    blockers: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)


class StrategyImplementation(Protocol):
    spec: StrategySpec

    def forecast(self, inputs: Mapping[str, Any]) -> StrategySignal: ...


class StrategyRegistry:
    """Compatibility-only registration catalog, never runtime authority.

    Production resolution and forecasting use StrategyFactoryRepository, which
    reads the PostgreSQL registry and manifest. This catalog exists only for
    deterministic built-in registration specifications and pure unit tests.
    """

    def __init__(self, strategies: tuple[StrategySpec, ...] = (), handlers: Mapping[str, Any] | None = None) -> None:
        self._specs = {item.strategy_key: item for item in strategies}
        self._handlers = dict(handlers or {})

    def register(self, spec: StrategySpec) -> None:
        if spec.strategy_key in self._specs:
            raise ValueError(f"strategy key is already registered: {spec.strategy_key}")
        self._specs[spec.strategy_key] = spec

    def resolve(self, strategy_key: str) -> StrategySpec:
        try:
            return self._specs[strategy_key]
        except KeyError as exc:
            raise KeyError(f"unknown versioned strategy key: {strategy_key}") from exc

    def all(self) -> tuple[StrategySpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def forecast(self, strategy_key: str, inputs: Mapping[str, Any]) -> StrategySignal:
        spec = self.resolve(strategy_key)
        handler = self._handlers.get(strategy_key)
        if handler is None:
            return StrategySignal(strategy_key=spec.strategy_key, status="blocked", actionability=spec.actionability, blockers=("strategy_handler_unregistered",))
        return handler(inputs, strategy_key=spec.strategy_key)


def content_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    if set(manifest) != set(MANIFEST_PARTS):
        raise ValueError("strategy manifest is incomplete")
    return content_hash({key: manifest[key] for key in MANIFEST_PARTS})


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _daily_rows(inputs: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    if cutoff is None:
        return []
    rows = [
        row for row in inputs.get("daily_bars", ())
        if isinstance(row, Mapping)
        and row.get("status") == "confirmed"
        and row.get("confirmed") is True
        and row.get("disabled") is not True
        and (observed := _parse_clock(row.get("observed_at"))) is not None
        and (available := _parse_clock(row.get("available_at"))) is not None
        and observed <= cutoff and available <= cutoff
    ]
    return sorted(rows, key=lambda row: (str(row.get("trading_date") or row.get("date") or ""), str(row.get("id") or "")))


def _parse_clock(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def daily_trend_underreaction(inputs: Mapping[str, Any], *, strategy_key: str = "daily_trend_underreaction_v1") -> StrategySignal:
    rows = _daily_rows(inputs)
    closes = [_number(row.get("close")) for row in rows]
    if len(closes) < 2 or any(value is None or value <= 0 for value in closes[-2:]):
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("daily_close_history_incomplete",))
    lookback = min(len(closes) - 1, 20)
    start, end = closes[-lookback - 1], closes[-1]
    assert start is not None and end is not None
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=end / start - 1,
        direction="long" if end > start else "short" if end < start else "flat",
        evidence={"lookback_days": lookback, "inputs": "confirmed_daily_bars", "underreaction_test": "future_oos_required"},
    )


def daily_gap_regime(inputs: Mapping[str, Any], *, strategy_key: str = "daily_gap_regime_v1") -> StrategySignal:
    rows = _daily_rows(inputs)
    if len(rows) < 2:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("daily_gap_history_incomplete",))
    current, previous = rows[-1], rows[-2]
    opening, previous_close = _number(current.get("open")), _number(previous.get("close"))
    if opening is None or previous_close is None or previous_close <= 0:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("gap_open_or_previous_close_missing",))
    gap = opening / previous_close - 1
    regime = "gap_up" if gap > 0 else "gap_down" if gap < 0 else "no_gap"
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=gap,
        direction="continuation" if gap else "flat", regime=regime,
        evidence={"gap_pct": gap, "branches": ("continuation", "reversal"), "decision": "continuation_vs_reversal_requires_oos_regime_evidence"},
    )


def event_propagation(inputs: Mapping[str, Any], *, strategy_key: str = "daily_event_propagation_v1") -> StrategySignal:
    event = inputs.get("event")
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    if not isinstance(event, Mapping) or cutoff is None or event.get("status") != "confirmed" or event.get("confirmed") is not True or event.get("disabled") is True or not event.get("release_at"):
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("event_release_clock_missing",))
    release_at = _parse_clock(event.get("release_at"))
    observed_at = _parse_clock(event.get("observed_at"))
    available_at = _parse_clock(event.get("available_at"))
    if release_at is None or observed_at is None or available_at is None or release_at > cutoff or observed_at > cutoff or available_at > cutoff:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("event_clock_invalid_or_future",))
    actual, consensus = _number(event.get("actual")), _number(event.get("consensus"))
    if actual is None or consensus is None:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("event_actual_or_consensus_missing",))
    surprise = actual - consensus
    fill_ready = inputs.get("fill_model_proven") is True
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=surprise,
        direction="long" if surprise > 0 else "short" if surprise < 0 else "flat",
        actionability="daily_research" if fill_ready else "shadow_only",
        blockers=() if fill_ready else ("event_time_fill_model_unproven",),
        evidence={"release_at": release_at.isoformat(), "surprise": surprise, "daily_only": True},
    )


def options_recovery_v2(inputs: Mapping[str, Any], *, strategy_key: str = "options_recovery_v2") -> StrategySignal:
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    required = ("full_chain_state", "oi_volume_state", "dividend_state")
    blockers = tuple(
        f"{key}_invalid" for key in required
        if cutoff is None or not _authoritative_state(inputs.get(key), cutoff)
    )
    quote_quality = inputs.get("quote_quality")
    if not isinstance(quote_quality, (int, float)) or isinstance(quote_quality, bool) or not isfinite(float(quote_quality)) or float(quote_quality) < 0:
        blockers += ("quote_quality_invalid",)
    if inputs.get("fill_model_proven") is not True:
        blockers += ("fill_model_unproven",)
    if blockers:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", actionability="shadow_only", blockers=blockers)
    return StrategySignal(strategy_key=strategy_key, status="available", actionability="shadow_only", evidence={"controls": [*required, "quote_quality", "fill_model_proven"], "paper_only": True})


def _authoritative_state(value: Any, cutoff: datetime) -> bool:
    if not isinstance(value, Mapping) or not value or value.get("status") != "confirmed" or value.get("confirmed") is not True or value.get("disabled") is True:
        return False
    available_at = _parse_clock(value.get("available_at"))
    observed_at = _parse_clock(value.get("observed_at"))
    return available_at is not None and observed_at is not None and available_at <= cutoff and observed_at <= cutoff


def crypto_funding_basis(inputs: Mapping[str, Any] | None = None, *, strategy_key: str = "crypto_funding_basis_v1") -> StrategySignal:
    return StrategySignal(
        strategy_key=strategy_key, status="blocked", actionability="registration_only",
        blockers=("venue_identity_required", "executable_depth_required", "liquidation_data_required", "failure_scenarios_required"),
    )


def full_denominator_complete(expected_members: list[str] | tuple[str, ...], observations: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]) -> bool:
    """Require one PIT observation and one realized outcome for every member."""

    expected = {str(member) for member in expected_members}
    observed = {str(row.get("instrument_id")) for row in observations}
    return bool(expected) and observed == expected and all(bool(row.get("outcome")) for row in observations)


def monitoring_complete(evidence_kinds: list[str] | tuple[str, ...]) -> bool:
    return set(evidence_kinds) >= {"correlation", "tail_correlation", "crowding", "capacity", "decay", "regime"}


def _manifest(source: str, data: tuple[str, ...], *, failure: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "source": {"definition": source, "version": "v1"},
        "data": {"fields": list(data), "clock": "observed_at and available_at"},
        "cost": {"model": "explicit_net_cost", "stress": ["1x", "2x", "3x"]},
        "capacity": {"model": "research_estimate", "status": "unproven"},
        "failure": {"scenarios": list(failure) or ["missing_or_stale_evidence", "negative_control_failure"]},
    }


def default_strategy_registry() -> StrategyRegistry:
    specs = (
        StrategySpec(strategy_key="classic_momentum_v1", revision=1, name="Classic momentum baseline", mechanism_class="trend_underreaction", economic_mechanism="Persistent information is incorporated gradually into daily prices.", falsification_rule="The cost-adjusted out-of-sample return is not positive and stable across parameter neighbors.", source_definition_version="classic-momentum.v1", manifest=_manifest("classic daily momentum", ("confirmed_daily_close",))),
        StrategySpec(strategy_key="classic_mean_reversion_v1", revision=1, name="Classic mean reversion baseline", mechanism_class="gap_regime", economic_mechanism="Short-lived daily dislocations partially revert after the opening shock.", falsification_rule="The continuation/reversal split has no stable cost-adjusted out-of-sample difference.", source_definition_version="classic-mean-reversion.v1", manifest=_manifest("classic daily gap reversion", ("confirmed_daily_open", "confirmed_daily_close"))),
        StrategySpec(strategy_key="martingale_v1", revision=1, name="Martingale negative control", mechanism_class="gap_regime", economic_mechanism="Increasing size after losses has no economic source of return.", falsification_rule="It must not show persistent positive edge and can never be promoted.", source_definition_version="martingale.v1", promotability="negative_control", actionability="research_only", manifest=_manifest("classic martingale negative control", ("confirmed_daily_close",), failure=("loss_streak", "ruin",))),
        StrategySpec(strategy_key="daily_trend_underreaction_v1", revision=1, name="Daily trend underreaction", mechanism_class="trend_underreaction", economic_mechanism="Medium-horizon underreaction creates persistent daily drift after information arrives.", falsification_rule="Neutralized and 3x-cost out-of-sample returns do not remain positive.", source_definition_version="daily-trend-underreaction.v1", manifest=_manifest("daily trend and underreaction", ("confirmed_daily_open", "confirmed_daily_close", "realized_volatility"))),
        StrategySpec(strategy_key="daily_gap_regime_v1", revision=1, name="Daily gap continuation versus reversal", mechanism_class="gap_regime", economic_mechanism="The sign and size of an opening gap condition later continuation or reversal.", falsification_rule="Regime-conditioned returns are indistinguishable after costs and purged validation.", source_definition_version="daily-gap-regime.v1", manifest=_manifest("daily gap continuation and reversal", ("confirmed_daily_open", "confirmed_daily_close", "market_regime"))),
        StrategySpec(strategy_key="daily_event_propagation_v1", revision=1, name="Daily event information propagation", mechanism_class="event_propagation", economic_mechanism="Released information propagates into daily prices over a measured horizon.", falsification_rule="Surprise direction does not predict later daily returns out of sample.", source_definition_version="daily-event-propagation.v1", actionability="shadow_only", manifest=_manifest("point-in-time event actual consensus surprise revision", ("event.release_at", "event.actual", "event.consensus", "event.surprise"), failure=("missing_release_clock", "unproven_fill_model"))),
        StrategySpec(strategy_key="options_recovery_v2", revision=2, name="Options recovery v2", mechanism_class="options_recovery", economic_mechanism="A qualified full-chain options state can measure recovery after a daily event shock.", falsification_rule="Recovery expectancy fails after full-chain controls, neutralization, and 3x-cost stress.", source_definition_version="options-recovery.v2", actionability="shadow_only", manifest=_manifest("existing options recovery registry extended with full-chain controls", ("full_chain_state", "open_interest", "volume", "quote_quality", "dividend_state"), failure=("stale_chain", "missing_oi_volume", "missing_fill_model"))),
        StrategySpec(strategy_key="crypto_funding_basis_v1", revision=1, name="Crypto funding and basis", mechanism_class="crypto_basis", economic_mechanism="Venue-specific funding and basis can compensate for hedged carry risk.", falsification_rule="Venue-level executable evidence, liquidation paths, and failure scenarios must pass before validation or actionability.", source_definition_version="crypto-funding-basis.v1", promotability="registration_only", actionability="registration_only", blockers=("venue_identity_required", "executable_depth_required", "liquidation_data_required", "failure_scenarios_required"), manifest=_manifest("registered Coin Metrics venue derivatives seam", ("venue", "funding", "basis", "executable_depth", "liquidations"), failure=("venue_data_missing", "liquidation_data_missing", "basis_not_executable"))),
        StrategySpec(strategy_key="structural_flow_v1", revision=1, name="Structural flow supporting family", mechanism_class="flow_supporting", economic_mechanism="Reported positioning and flow may support a distinct exposure explanation when coverage matures.", falsification_rule="The flow signal is rejected or labeled an exposure sleeve when it is only a factor replica.", source_definition_version="structural-flow.v1", promotability="exposure_sleeve", actionability="shadow_only", manifest=_manifest("existing SEC 13F positioning and flow seam", ("positioning.flow",), failure=("insufficient_history", "factor_replica"))),
    )
    handlers = {
        "classic_momentum_v1": daily_trend_underreaction,
        "classic_mean_reversion_v1": daily_gap_regime,
        "martingale_v1": lambda _inputs, *, strategy_key: StrategySignal(strategy_key=strategy_key, status="blocked", actionability="research_only", blockers=("permanent_negative_control",)),
        "daily_trend_underreaction_v1": daily_trend_underreaction,
        "daily_gap_regime_v1": daily_gap_regime,
        "daily_event_propagation_v1": event_propagation,
        "options_recovery_v2": options_recovery_v2,
        "crypto_funding_basis_v1": crypto_funding_basis,
    }
    return StrategyRegistry(specs, handlers)


__all__ = [
    "MANIFEST_PARTS", "MECHANISM_CLASSES", "StrategyImplementation", "StrategyRegistry", "StrategySignal", "StrategySpec",
    "content_hash", "crypto_funding_basis", "daily_gap_regime", "daily_trend_underreaction", "default_strategy_registry",
    "event_propagation", "full_denominator_complete", "is_martingale_family", "manifest_hash", "monitoring_complete",
    "options_recovery_v2", "strategy_family_for_key",
]
