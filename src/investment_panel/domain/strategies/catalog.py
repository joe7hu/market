"""Typed strategy definitions and the single pure evaluation path."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from investment_panel.domain.factors import EvaluationContext
from investment_panel.domain.strategies.implementations import (
    DAILY_ACTIONABILITY,
    EmptyParameters,
    StrategyParameters,
    StrategySignal,
    TrendParameters,
    crypto_funding_basis,
    daily_gap_regime,
    daily_trend_underreaction,
    event_propagation,
    factor_snapshot_for_inputs,
    options_recovery_v2,
    volatility_aware_momentum,
)


MECHANISM_CLASSES = (
    "trend_underreaction",
    "gap_regime",
    "event_propagation",
    "options_recovery",
)
MANIFEST_PARTS = ("source", "data", "cost", "capacity", "failure")
ACTIONABILITY_LEVELS = MappingProxyType({
    "registration_only": 0,
    "research_only": 1,
    "shadow_only": 2,
    "daily_research": 3,
})
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
    implementation_id: str | None = None
    implementation_version: str | None = None
    strategy_family: str = "legacy"
    promotability: str = "standard"
    actionability: DAILY_ACTIONABILITY = "daily_research"
    manifest: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_definition(self) -> "StrategySpec":
        if self.mechanism_class not in MECHANISM_CLASSES and self.mechanism_class not in {"crypto_basis", "flow_supporting", "martingale"}:
            raise ValueError("unknown strategy mechanism class")
        if (self.implementation_id is None) != (self.implementation_version is None):
            raise ValueError("strategy implementation identity requires both id and version")
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


@dataclass(frozen=True)
class StrategyImplementationDefinition:
    implementation_id: str
    implementation_version: str
    parameters_type: type[StrategyParameters]
    evaluate: Callable[..., "StrategySignal"]


def content_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    if set(manifest) != set(MANIFEST_PARTS):
        raise ValueError("strategy manifest is incomplete")
    return content_hash({key: manifest[key] for key in MANIFEST_PARTS})


IMPLEMENTATION_CATALOG: Mapping[str, StrategyImplementationDefinition] = MappingProxyType({
    "daily_trend_underreaction": StrategyImplementationDefinition(
        "daily_trend_underreaction", "2", TrendParameters, daily_trend_underreaction,
    ),
    "daily_gap_regime": StrategyImplementationDefinition(
        "daily_gap_regime", "1", EmptyParameters, daily_gap_regime,
    ),
    "daily_event_propagation": StrategyImplementationDefinition(
        "daily_event_propagation", "1", EmptyParameters, event_propagation,
    ),
    "options_recovery": StrategyImplementationDefinition(
        "options_recovery", "2", EmptyParameters, options_recovery_v2,
    ),
    "crypto_funding_basis": StrategyImplementationDefinition(
        "crypto_funding_basis", "1", EmptyParameters, crypto_funding_basis,
    ),
    "volatility_aware_momentum": StrategyImplementationDefinition(
        "volatility_aware_momentum", "1", EmptyParameters, volatility_aware_momentum,
    ),
})


def evaluate_strategy(
    spec: StrategySpec,
    inputs: Mapping[str, Any], *,
    account_actionability: str | None = None,
    factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    """Evaluate a resolved revision through its exact implementation binding."""

    if is_martingale_family(spec.strategy_key, spec.mechanism_class, spec.name, spec.strategy_family):
        return _finalize_strategy_result(spec, StrategySignal(
            strategy_key=spec.strategy_key,
            status="blocked",
            actionability="research_only",
            blockers=("permanent_negative_control",),
        ), account_actionability)

    implementation = IMPLEMENTATION_CATALOG.get(spec.implementation_id or "")
    if (
        implementation is None
        or spec.implementation_version is None
        or implementation.implementation_version != spec.implementation_version
    ):
        return _finalize_strategy_result(spec, StrategySignal(
            strategy_key=spec.strategy_key,
            status="blocked",
            actionability="registration_only",
            blockers=("strategy_implementation_unavailable",),
        ), account_actionability)
    try:
        params = implementation.parameters_type.model_validate(spec.parameters)
    except ValidationError:
        return _finalize_strategy_result(spec, StrategySignal(
            strategy_key=spec.strategy_key,
            status="blocked",
            actionability="registration_only",
            blockers=("strategy_parameters_invalid",),
        ), account_actionability)

    result = implementation.evaluate(
        inputs, strategy_key=spec.strategy_key, params=params, factor_context=factor_context,
    )
    return _finalize_strategy_result(spec, result, account_actionability)


def _finalize_strategy_result(
    spec: StrategySpec,
    result: StrategySignal,
    account_actionability: str | None,
) -> StrategySignal:
    if not isinstance(result, StrategySignal):
        try:
            result = StrategySignal.model_validate(result)
        except ValidationError as exc:
            raise ValueError("strategy evaluator returned an invalid result") from exc
    if result.strategy_key != spec.strategy_key:
        raise ValueError("strategy evaluator identity mismatch")
    ceiling = _actionability_min(spec.actionability, account_actionability)
    if ceiling is None:
        return result.model_copy(update={"status": "blocked", "actionability": "registration_only", "blockers": (*result.blockers, "strategy_actionability_invalid")})
    blockers = tuple(dict.fromkeys((*spec.blockers, *result.blockers)))
    return result.model_copy(update={
        "status": "blocked" if spec.blockers or result.status == "blocked" else result.status,
        "actionability": _actionability_min(result.actionability, ceiling) or "registration_only",
        "blockers": blockers,
    })


def _actionability_min(left: str, right: str | None) -> str | None:
    if left not in ACTIONABILITY_LEVELS or (right is not None and right not in ACTIONABILITY_LEVELS):
        return None
    if right is None:
        return left
    return left if ACTIONABILITY_LEVELS[left] <= ACTIONABILITY_LEVELS[right] else right


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


BUILTIN_STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(strategy_key="classic_momentum_v1", revision=1, name="Classic momentum baseline", mechanism_class="trend_underreaction", economic_mechanism="Persistent information is incorporated gradually into daily prices.", falsification_rule="The cost-adjusted out-of-sample return is not positive and stable across parameter neighbors.", source_definition_version="classic-momentum.v1", implementation_id="daily_trend_underreaction", implementation_version="2", manifest=_manifest("classic daily momentum", ("confirmed_daily_close",))),
        StrategySpec(strategy_key="classic_mean_reversion_v1", revision=1, name="Classic mean reversion baseline", mechanism_class="gap_regime", economic_mechanism="Short-lived daily dislocations partially revert after the opening shock.", falsification_rule="The continuation/reversal split has no stable cost-adjusted out-of-sample difference.", source_definition_version="classic-mean-reversion.v1", implementation_id="daily_gap_regime", implementation_version="1", manifest=_manifest("classic daily gap reversion", ("confirmed_daily_open", "confirmed_daily_close"))),
        StrategySpec(strategy_key="martingale_v1", revision=1, name="Martingale negative control", mechanism_class="gap_regime", economic_mechanism="Increasing size after losses has no economic source of return.", falsification_rule="It must not show persistent positive edge and can never be promoted.", source_definition_version="martingale.v1", implementation_id="daily_gap_regime", implementation_version="1", promotability="negative_control", actionability="research_only", manifest=_manifest("classic martingale negative control", ("confirmed_daily_close",), failure=("loss_streak", "ruin",))),
    StrategySpec(strategy_key="daily_trend_underreaction_v2", revision=2, name="Daily trend underreaction", mechanism_class="trend_underreaction", economic_mechanism="Medium-horizon underreaction creates persistent daily drift after information arrives.", falsification_rule="Neutralized and 3x-cost out-of-sample returns do not remain positive.", source_definition_version="daily-trend-underreaction.v2", implementation_id="daily_trend_underreaction", implementation_version="2", manifest=_manifest("daily trend and underreaction with lossless missing-session handling", ("confirmed_daily_open", "confirmed_daily_close", "realized_volatility"))),
        StrategySpec(strategy_key="daily_gap_regime_v1", revision=1, name="Daily gap continuation versus reversal", mechanism_class="gap_regime", economic_mechanism="The sign and size of an opening gap condition later continuation or reversal.", falsification_rule="Regime-conditioned returns are indistinguishable after costs and purged validation.", source_definition_version="daily-gap-regime.v1", implementation_id="daily_gap_regime", implementation_version="1", manifest=_manifest("daily gap continuation and reversal", ("confirmed_daily_open", "confirmed_daily_close", "market_regime"))),
        StrategySpec(strategy_key="daily_event_propagation_v1", revision=1, name="Daily event information propagation", mechanism_class="event_propagation", economic_mechanism="Released information propagates into daily prices over a measured horizon.", falsification_rule="Surprise direction does not predict later daily returns out of sample.", source_definition_version="daily-event-propagation.v1", implementation_id="daily_event_propagation", implementation_version="1", actionability="shadow_only", manifest=_manifest("point-in-time event actual consensus surprise revision", ("event.release_at", "event.actual", "event.consensus", "event.surprise"), failure=("missing_release_clock", "unproven_fill_model"))),
        StrategySpec(strategy_key="options_recovery_v2", revision=2, name="Options recovery v2", mechanism_class="options_recovery", economic_mechanism="A qualified full-chain options state can measure recovery after a daily event shock.", falsification_rule="Recovery expectancy fails after full-chain controls, neutralization, and 3x-cost stress.", source_definition_version="options-recovery.v2", implementation_id="options_recovery", implementation_version="2", actionability="shadow_only", manifest=_manifest("existing options recovery registry extended with full-chain controls", ("full_chain_state", "open_interest", "volume", "quote_quality", "dividend_state"), failure=("stale_chain", "missing_oi_volume", "missing_fill_model"))),
        StrategySpec(strategy_key="crypto_funding_basis_v1", revision=1, name="Crypto funding and basis", mechanism_class="crypto_basis", economic_mechanism="Venue-specific funding and basis can compensate for hedged carry risk.", falsification_rule="Venue-level executable evidence, liquidation paths, and failure scenarios must pass before validation or actionability.", source_definition_version="crypto-funding-basis.v1", implementation_id="crypto_funding_basis", implementation_version="1", promotability="registration_only", actionability="registration_only", blockers=("venue_identity_required", "executable_depth_required", "liquidation_data_required", "failure_scenarios_required"), manifest=_manifest("registered Coin Metrics venue derivatives seam", ("venue", "funding", "basis", "executable_depth", "liquidations"), failure=("venue_data_missing", "liquidation_data_missing", "basis_not_executable"))),
        StrategySpec(strategy_key="structural_flow_v1", revision=1, name="Structural flow supporting family", mechanism_class="flow_supporting", economic_mechanism="Reported positioning and flow may support a distinct exposure explanation when coverage matures.", falsification_rule="The flow signal is rejected or labeled an exposure sleeve when it is only a factor replica.", source_definition_version="structural-flow.v1", implementation_id="unavailable", implementation_version="1", promotability="exposure_sleeve", actionability="shadow_only", manifest=_manifest("existing SEC 13F positioning and flow seam", ("positioning.flow",), failure=("insufficient_history", "factor_replica"))),
        StrategySpec(strategy_key="volatility_aware_momentum_v1", revision=1, name="Volatility-aware momentum research", mechanism_class="trend_underreaction", economic_mechanism="Momentum direction is interpreted with an explicit realized-volatility context.", falsification_rule="The interpretation does not survive independent out-of-sample and cost validation.", source_definition_version="volatility-aware-momentum.v1", implementation_id="volatility_aware_momentum", implementation_version="1", actionability="research_only", manifest=_manifest("reusable momentum and volatility signal", ("confirmed_daily_close", "realized_volatility"), failure=("missing_window", "unqualified_forecast"))),
    )


def default_strategy_definitions() -> tuple[StrategySpec, ...]:
    return BUILTIN_STRATEGIES


def resolve_builtin_strategy(strategy_key: str) -> StrategySpec:
    for spec in BUILTIN_STRATEGIES:
        if spec.strategy_key == strategy_key:
            return spec
    raise KeyError(f"unknown built-in strategy key: {strategy_key}")


__all__ = [
    "ACTIONABILITY_LEVELS", "BUILTIN_STRATEGIES", "EmptyParameters", "IMPLEMENTATION_CATALOG", "MANIFEST_PARTS",
    "MECHANISM_CLASSES", "StrategyImplementationDefinition", "StrategyParameters", "StrategySignal", "StrategySpec",
    "TrendParameters", "content_hash", "crypto_funding_basis", "daily_gap_regime", "daily_trend_underreaction",
    "default_strategy_definitions", "evaluate_strategy", "event_propagation", "full_denominator_complete",
    "factor_snapshot_for_inputs", "is_martingale_family", "manifest_hash", "monitoring_complete", "options_recovery_v2", "resolve_builtin_strategy",
    "strategy_family_for_key",
]
