"""Locked Phase 2 data contracts and deterministic advisory market state.

Adapters in this module parse provider payloads only. Persistence is owned by
PostgreSQL repositories; credentials are checked for presence and never
returned or copied into an observation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import math
import os
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Phase2Status(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING_SOURCE = "MISSING_SOURCE"
    MISSING_HISTORY = "MISSING_HISTORY"
    CONFLICTED = "CONFLICTED"
    FALLBACK = "FALLBACK"
    UNSUPPORTED = "UNSUPPORTED"


class Phase2Source(StrEnum):
    FRED = "fred"
    TREASURY = "treasury"
    TRADING_ECONOMICS = "trading_economics"
    ALPHAVANTAGE = "alphavantage"
    ROBINHOOD_HISTORY_FULL = "robinhood_history_full"
    IBKR_OPTIONS = "ibkr_options"
    COINMETRICS = "coinmetrics"
    COINGECKO = "coingecko"
    SEC_COMPANYFACTS = "sec_companyfacts"
    SEC_13F = "sec_13f"
    SHORT_INTEREST = "short_interest"


@dataclass(frozen=True)
class SourceContract:
    source_id: str
    authority: str
    capabilities: tuple[str, ...]
    credential_env: str | None = None
    public: bool = False


SOURCE_CONTRACTS: dict[str, SourceContract] = {
    Phase2Source.FRED: SourceContract("fred", "PIT macro vintages", ("macro", "vintage"), "FRED_API_KEY"),
    Phase2Source.TREASURY: SourceContract("treasury", "public nominal and real yield curves", ("rates",), public=True),
    Phase2Source.TRADING_ECONOMICS: SourceContract("trading_economics", "event consensus", ("event_consensus",), "TRADING_ECONOMICS_API_KEY"),
    Phase2Source.ALPHAVANTAGE: SourceContract("alphavantage", "quarterly corporate expectations", ("corporate_expectations",), "ALPHAVANTAGE_API_KEY"),
    Phase2Source.ROBINHOOD_HISTORY_FULL: SourceContract("robinhood_history_full", "full option history", ("option_oi", "option_volume")),
    Phase2Source.IBKR_OPTIONS: SourceContract("ibkr_options", "option quote history", ("option_oi", "option_volume")),
    Phase2Source.COINMETRICS: SourceContract("coinmetrics", "venue-level crypto derivatives", ("funding", "basis", "open_interest", "liquidations", "depth"), "COINMETRICS_API_KEY"),
    Phase2Source.COINGECKO: SourceContract("coingecko", "aggregate descriptive crypto data", ("aggregate_volume",), public=True),
    Phase2Source.SEC_COMPANYFACTS: SourceContract("sec_companyfacts", "filed corporate actuals", ("corporate_actuals",), public=True),
    Phase2Source.SEC_13F: SourceContract("sec_13f", "reported positioning and flow", ("positioning",), public=True),
    Phase2Source.SHORT_INTEREST: SourceContract("short_interest", "short interest", (), public=True),
}


FIELD_CONTRACTS: dict[str, dict[str, str]] = {
    "macro.value": {"definition": "A published macro series value for one observation date.", "clock": "vintage_at or release_at; available_at must be source availability."},
    "rates.nominal_yield": {"definition": "Treasury nominal par yield for a stated tenor.", "clock": "observed_at and available_at from Treasury publication."},
    "rates.real_yield": {"definition": "Treasury real yield for a stated tenor.", "clock": "observed_at and available_at from Treasury publication."},
    "credit.spread": {"definition": "A named credit spread series value.", "clock": "vintage_at or release_at; available_at must be source availability."},
    "event.actual": {"definition": "Released event actual, preserving the event release clock.", "clock": "release_at and available_at."},
    "event.consensus": {"definition": "Consensus available before the event release.", "clock": "available_at, never received_at."},
    "event.surprise": {"definition": "Actual minus consensus using the provider unit and sign convention.", "clock": "release_at and available_at."},
    "event.revision": {"definition": "Revision to a previously released event value.", "clock": "publication_at or release_at and available_at."},
    "corporate.expected": {"definition": "Quarterly provider expectation, not an SEC actual.", "clock": "publication_at and available_at."},
    "crypto.venue_derivatives": {"definition": "Venue-identified derivative observation with executable depth identity.", "clock": "observed_at and available_at."},
    "crypto.depth": {"definition": "Venue-level executable derivative depth observation.", "clock": "observed_at and available_at."},
    "option.open_interest": {"definition": "Contract-level end-of-session open interest.", "clock": "observed_at and available_at."},
    "option.volume": {"definition": "Contract-level session volume.", "clock": "observed_at and available_at."},
}


class PITObservation(BaseModel):
    """One source observation with explicit source and information clocks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    value: float | int | str | bool | None = None
    unit: str | None = None
    observed_at: datetime
    available_at: datetime
    publication_at: datetime | None = None
    release_at: datetime | None = None
    vintage_at: datetime | None = None
    status: Phase2Status = Phase2Status.AVAILABLE
    confidence: float = Field(default=1.0, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def clocks_are_valid(self) -> "PITObservation":
        timestamps = (self.observed_at, self.available_at, self.publication_at, self.release_at, self.vintage_at)
        if any(value is not None and value.tzinfo is None for value in timestamps):
            raise ValueError("Phase 2 source clocks must be timezone-aware")
        if self.status is Phase2Status.AVAILABLE and self.value is None:
            raise ValueError("available observations require a value")
        return self


class EventObservation(PITObservation):
    actual: float | None = None
    consensus: float | None = None
    surprise: float | None = None
    revision: float | None = None


class AdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    status: Phase2Status
    observations: tuple[PITObservation, ...] = ()
    reason: str = ""


def source_status(source_id: str, *, env: Mapping[str, str] | None = None, has_history: bool = True) -> Phase2Status:
    """Return status without exposing credential values."""

    contract = SOURCE_CONTRACTS.get(str(source_id))
    if contract is None:
        return Phase2Status.UNSUPPORTED
    if str(source_id) == Phase2Source.SHORT_INTEREST:
        return Phase2Status.UNSUPPORTED
    active_env = os.environ if env is None else env
    if contract.credential_env and not str(active_env.get(contract.credential_env) or "").strip():
        return Phase2Status.MISSING_SOURCE
    if not has_history:
        return Phase2Status.MISSING_HISTORY
    return Phase2Status.AVAILABLE


def source_contracts() -> tuple[SourceContract, ...]:
    return tuple(SOURCE_CONTRACTS[key] for key in sorted(SOURCE_CONTRACTS))


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _clock(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    if value in (None, "", ".", "null", "None"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _payload_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("observations", payload.get("data", payload.get("results", ())))
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else []


def _observation(
    row: Mapping[str, Any], *, source_id: str, field_name: str, dimension: str, asset_class: str,
    source_version: str, value: Any, metadata: Mapping[str, Any] | None = None,
) -> PITObservation | None:
    observed_at = _clock(row.get("observed_at") or row.get("date") or row.get("period_end") or row.get("period"))
    available_at = _clock(row.get("available_at", row.get("vintage_at", row.get("publication_at", row.get("release_at")))))
    if observed_at is None or available_at is None:
        return None
    return PITObservation(
        observation_id=_stable_id(source_id, field_name, row.get("date", row.get("period")), value, available_at.isoformat()),
        field_name=field_name, dimension=dimension, asset_class=asset_class, source_id=source_id,
        source_version=source_version, value=value, unit=str(row.get("unit") or "") or None,
        observed_at=observed_at, available_at=available_at,
        publication_at=_clock(row.get("publication_at")), release_at=_clock(row.get("release_at")),
        vintage_at=_clock(row.get("vintage_at")), metadata=dict(metadata or {}),
    )


def parse_fred_alfred(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    status = source_status(Phase2Source.FRED, env=env, has_history=bool(_payload_rows(payload)))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id=Phase2Source.FRED, status=status, reason="FRED_API_KEY is absent or the vintage payload has no history")
    observations = tuple(
        item for row in _payload_rows(payload)
        if (item := _observation(row, source_id="fred", field_name=str(row.get("field_name") or row.get("series_id") or "macro.value"), dimension=str(row.get("dimension") or "growth/inflation"), asset_class="macro", source_version=str(payload.get("source_version") or "fred-alfred.v1"), value=_number(row.get("value")), metadata={"series_id": row.get("series_id"), "vintage": row.get("vintage_at")})) is not None and item.value is not None
    )
    return AdapterResult(source_id="fred", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=observations, reason="" if observations else "no usable FRED values")


def parse_treasury_yield_curve(payload: Mapping[str, Any]) -> AdapterResult:
    rows = _payload_rows(payload)
    if not rows:
        return AdapterResult(source_id="treasury", status=Phase2Status.MISSING_HISTORY, reason="Treasury yield curve payload has no history")
    observations = tuple(
        item for row in rows
        if (item := _observation(row, source_id="treasury", field_name=str(row.get("field_name") or ("rates.real_yield" if row.get("real") else "rates.nominal_yield")), dimension="rates", asset_class="rates", source_version=str(payload.get("source_version") or "treasury-curve.v1"), value=_number(row.get("value")), metadata={"tenor": row.get("tenor"), "real": bool(row.get("real"))})) is not None and item.value is not None
    )
    return AdapterResult(source_id="treasury", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=observations, reason="" if observations else "no usable Treasury values")


def parse_event_consensus(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    rows = _payload_rows(payload)
    status = source_status(Phase2Source.TRADING_ECONOMICS, env=env, has_history=bool(rows))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id="trading_economics", status=status, reason="Trading Economics credential or event history is unavailable")
    observations: list[PITObservation] = []
    for row in rows:
        observed_at = _clock(row.get("event_at", row.get("observed_at", row.get("date"))))
        available_at = _clock(row.get("available_at", row.get("publication_at")))
        actual = _number(row.get("actual"))
        consensus = _number(row.get("consensus", row.get("forecast")))
        if observed_at is None or available_at is None:
            continue
        surprise = round(actual - consensus, 12) if actual is not None and consensus is not None else None
        revision = _number(row.get("revision"))
        status_value = Phase2Status.AVAILABLE if actual is not None and consensus is not None else Phase2Status.MISSING_HISTORY
        observations.append(EventObservation(
            observation_id=_stable_id("trading_economics", row.get("event_id", row.get("name")), observed_at.isoformat(), actual, consensus, revision),
            field_name="event.actual", dimension="event risk", asset_class="macro", source_id="trading_economics",
            source_version=str(payload.get("source_version") or "trading-economics.v1"), value=actual, unit=str(row.get("unit") or "") or None,
            observed_at=observed_at, available_at=available_at, publication_at=_clock(row.get("publication_at")), release_at=_clock(row.get("release_at", row.get("event_at"))),
            status=status_value, actual=actual, consensus=consensus, surprise=surprise, revision=revision,
            metadata={"event_id": row.get("event_id"), "name": row.get("name")},
        ))
    return AdapterResult(source_id="trading_economics", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=tuple(observations), reason="" if observations else "no event clocks or values")


def parse_corporate_expectations(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    rows = _payload_rows(payload)
    status = source_status(Phase2Source.ALPHAVANTAGE, env=env, has_history=bool(rows))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id="alphavantage", status=status, reason="Alpha Vantage credential or quarterly history is unavailable")
    observations = tuple(
        item for row in rows
        if (item := _observation(row, source_id="alphavantage", field_name="corporate.expected", dimension="corporate cycle", asset_class="equity", source_version=str(payload.get("source_version") or "alphavantage-quarterly.v1"), value=_number(row.get("expected", row.get("value")),), metadata={"ticker": row.get("ticker"), "period_end": row.get("period_end"), "metric": row.get("metric")})) is not None and item.value is not None
    )
    return AdapterResult(source_id="alphavantage", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=observations, reason="" if observations else "no usable quarterly expectations")


def parse_coinmetrics_derivatives(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    rows = _payload_rows(payload)
    status = source_status(Phase2Source.COINMETRICS, env=env, has_history=bool(rows))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id="coinmetrics", status=status, reason="Coin Metrics credential or venue history is unavailable")
    observations: list[PITObservation] = []
    for row in rows:
        venue = str(row.get("venue") or row.get("exchange") or "").strip()
        instrument = str(row.get("instrument") or row.get("symbol") or "").strip()
        depth = _number(row.get("depth_usd", row.get("depth")))
        if not venue or not instrument or depth is None or depth < 0:
            continue
        for field, value in (("funding", row.get("funding")), ("basis", row.get("basis")), ("open_interest", row.get("open_interest")), ("liquidations", row.get("liquidations")), ("depth", depth)):
            parsed = _number(value)
            if parsed is None:
                continue
            item = _observation(row, source_id="coinmetrics", field_name=f"crypto.{field}", dimension="crypto liquidity", asset_class="crypto", source_version=str(payload.get("source_version") or "coinmetrics-derivatives.v1"), value=parsed, metadata={"venue": venue, "instrument": instrument, "depth_usd": depth})
            if item is not None:
                observations.append(item)
    return AdapterResult(source_id="coinmetrics", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=tuple(observations), reason="" if observations else "venue identity, depth, or derivative values are missing")


def assess_option_oi_volume_sla(rows: Sequence[Mapping[str, Any]], *, minimum_coverage: float = 0.98) -> dict[str, Any]:
    """Assess the existing history_full/IBKR seam without authorizing trades."""

    total = len(rows)
    valid = sum(
        1 for row in rows
        if _number(row.get("open_interest")) is not None and _number(row.get("volume")) is not None
        and _number(row.get("open_interest")) >= 0 and _number(row.get("volume")) >= 0
    )
    coverage = valid / total if total else 0.0
    status = Phase2Status.MISSING_HISTORY if not total else Phase2Status.AVAILABLE if coverage >= minimum_coverage else Phase2Status.MISSING_HISTORY
    return {
        "status": status.value,
        "minimum_coverage": minimum_coverage,
        "contract_count": total,
        "complete_count": valid,
        "coverage": coverage,
        "positioning_allowed": status is Phase2Status.AVAILABLE,
        "reason": "" if status is Phase2Status.AVAILABLE else "contract-level OI and volume coverage is below the SLA or history is absent",
    }


def assess_crypto_venue_data(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if str(row.get("venue") or row.get("exchange") or "").strip() and str(row.get("instrument") or row.get("symbol") or "").strip() and _number(row.get("depth_usd", row.get("depth"))) is not None and _number(row.get("depth_usd", row.get("depth"))) >= 0]
    return {
        "status": Phase2Status.AVAILABLE.value if valid else Phase2Status.MISSING_HISTORY.value,
        "venue_count": len({str(row.get("venue") or row.get("exchange")) for row in valid}),
        "observation_count": len(valid),
        "executable": bool(valid),
        "reason": "" if valid else "venue identity and non-negative depth are required; aggregate crypto volume is descriptive only",
    }


@dataclass(frozen=True)
class PITSelection:
    selected: tuple[PITObservation, ...]
    retained: tuple[PITObservation, ...]
    conflicts: tuple[tuple[str, ...], ...]
    missing_fields: tuple[str, ...]


def select_point_in_time(observations: Sequence[PITObservation], cutoff: datetime, *, fields: Sequence[str] = ()) -> PITSelection:
    cutoff = _utc(cutoff)
    eligible = tuple(sorted((row for row in observations if _utc(row.observed_at) <= cutoff and _utc(row.available_at) <= cutoff), key=lambda row: (row.field_name, _utc(row.observed_at), _utc(row.available_at), row.source_id, row.observation_id)))
    grouped: dict[tuple[str, datetime], list[PITObservation]] = defaultdict(list)
    for row in eligible:
        grouped[(row.field_name, _utc(row.observed_at))].append(row)
    selected: list[PITObservation] = []
    conflicts: list[tuple[str, ...]] = []
    for key, group in sorted(grouped.items()):
        values = {json.dumps(row.value, sort_keys=True, default=str) for row in group}
        if len(values) > 1 or any(row.status is Phase2Status.CONFLICTED for row in group):
            ids = tuple(row.observation_id for row in group)
            conflicts.append(ids)
            continue
        selected.append(max(group, key=lambda row: (_utc(row.available_at), row.source_id, row.observation_id)))
    present = {row.field_name for row in selected}
    missing = tuple(sorted(set(fields) - present))
    return PITSelection(tuple(selected), eligible, tuple(conflicts), missing)


class CoverageVectorRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: str
    expression: str
    required_fields: tuple[str, ...]
    status: Phase2Status
    point_in_time_safe: bool
    selected_sources: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    blockers: tuple[str, ...] = ()


class CoverageVector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "phase2-coverage-vector.v1"
    vector_id: str
    as_of: datetime
    rows: tuple[CoverageVectorRow, ...]


def build_coverage_vector(
    as_of: datetime, strategies: Mapping[str, Mapping[str, Sequence[str]]], observations: Sequence[PITObservation],
) -> CoverageVector:
    rows: list[CoverageVectorRow] = []
    for strategy, expressions in sorted(strategies.items()):
        for expression, fields in sorted(expressions.items()):
            required = tuple(sorted(set(fields)))
            selection = select_point_in_time(observations, as_of, fields=required)
            by_field = {row.field_name: row for row in selection.selected}
            blockers = [f"missing:{field}" for field in selection.missing_fields]
            if selection.conflicts:
                blockers.append("conflicted:source_observations")
            sources = tuple(sorted({row.source_id for row in by_field.values()}))
            statuses = {row.status for row in by_field.values()}
            if selection.conflicts or Phase2Status.CONFLICTED in statuses:
                status = Phase2Status.CONFLICTED
            elif blockers:
                status = Phase2Status.MISSING_HISTORY
            elif Phase2Status.FALLBACK in statuses:
                status = Phase2Status.FALLBACK
            elif all(row.status is Phase2Status.AVAILABLE for row in by_field.values()):
                status = Phase2Status.AVAILABLE
            else:
                status = Phase2Status.MISSING_SOURCE
            confidence = min((row.confidence for row in by_field.values()), default=0.0)
            if status is Phase2Status.FALLBACK:
                confidence *= 0.75
                blockers.append("fallback_source_confidence_haircut")
            rows.append(CoverageVectorRow(strategy=strategy, expression=expression, required_fields=required, status=status, point_in_time_safe=not blockers and status is not Phase2Status.CONFLICTED, selected_sources=sources, confidence=confidence, blockers=tuple(sorted(set(blockers)))))
    payload = [row.model_dump(mode="json") for row in rows]
    return CoverageVector(vector_id=_stable_id("coverage-vector", _utc(as_of).isoformat(), payload), as_of=as_of, rows=tuple(rows))


STATE_LABELS = ("negative", "neutral", "positive")


class DimensionPosterior(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Phase2Status
    state: str | None = None
    distribution: dict[str, float] = Field(default_factory=dict)
    entropy: float | None = None
    persistence: float | None = None
    transition_probabilities: dict[str, dict[str, float]] = Field(default_factory=dict)
    missingness: float
    uncertainty: str
    sample_count: int = Field(ge=0)


class MarketStatePosterior(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "market-state-posterior.v1"
    posterior_id: str
    as_of: datetime
    input_cutoff: datetime
    baseline: dict[str, Any]
    challenger: dict[str, Any]
    dimensions: dict[str, DimensionPosterior]
    overall_confidence: float = Field(ge=0, le=1)
    entropy: float | None = None
    missingness: float
    persistence: float | None = None
    transition_probabilities: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)
    incremental_oos_net_utility: float | None = None
    rank_authorized: bool = False
    advisory_only: bool = True

    @model_validator(mode="after")
    def preserve_advisory_boundary(self) -> "MarketStatePosterior":
        if self.rank_authorized or not self.advisory_only:
            raise ValueError("Phase 2 posterior is advisory-only")
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("posterior timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("posterior as_of and input_cutoff must match")
        return self


def build_market_state_posterior(observations: Sequence[PITObservation], *, as_of: datetime, max_observations: int = 500) -> MarketStatePosterior:
    """Build a bounded deterministic observable baseline and persistence challenger."""

    cutoff = _utc(as_of)
    rows = sorted((row for row in observations if _utc(row.observed_at) <= cutoff and _utc(row.available_at) <= cutoff), key=lambda row: (row.dimension, _utc(row.observed_at), row.observation_id))[-max(1, min(max_observations, 500)) :]
    by_dimension: dict[str, list[PITObservation]] = defaultdict(list)
    for row in rows:
        by_dimension[row.dimension].append(row)
    dimensions: dict[str, DimensionPosterior] = {}
    for dimension in sorted(by_dimension):
        group = by_dimension[dimension]
        valid = [row for row in group if row.status in {Phase2Status.AVAILABLE, Phase2Status.FALLBACK} and _number(row.value) is not None]
        labels = [_state_label(float(row.value)) for row in valid]
        counts = Counter(labels)
        distribution = {label: counts[label] / len(labels) for label in STATE_LABELS} if labels else {}
        transitions: dict[str, dict[str, float]] = {}
        for left, right in zip(labels, labels[1:]):
            transitions.setdefault(left, Counter())[right] += 1  # type: ignore[index]
        normalized_transitions = {left: {state: count / sum(values.values()) for state, count in sorted(values.items())} for left, values in sorted(transitions.items())}
        entropy = _entropy(distribution) if distribution else None
        persistence = sum(1 for left, right in zip(labels, labels[1:]) if left == right) / (len(labels) - 1) if len(labels) > 1 else None
        missingness = 1 - (len(valid) / len(group) if group else 0)
        status = Phase2Status.AVAILABLE if valid else Phase2Status.MISSING_HISTORY
        dimensions[dimension] = DimensionPosterior(status=status, state=labels[-1] if labels else None, distribution=distribution, entropy=entropy, persistence=persistence, transition_probabilities=normalized_transitions, missingness=missingness, uncertainty="observable frequency with deterministic persistence challenger" if valid else "missing point-in-time history", sample_count=len(valid))
    valid_count = sum(item.sample_count for item in dimensions.values())
    total_count = len(rows)
    overall_confidence = valid_count / total_count if total_count else 0.0
    entropy_values = [item.entropy for item in dimensions.values() if item.entropy is not None]
    persistence_values = [item.persistence for item in dimensions.values() if item.persistence is not None]
    baseline = {"method": "observable-frequency.v1", "dimensions": {key: value.distribution for key, value in dimensions.items()}, "status": "available" if valid_count else "missing_history"}
    challenger = {"method": "bounded-persistence.v1", "dimensions": {key: value.transition_probabilities for key, value in dimensions.items()}, "status": "advisory", "incremental_oos_net_utility": None}
    transitions = {key: value.transition_probabilities for key, value in dimensions.items()}
    payload = {"as_of": cutoff.isoformat(), "baseline": baseline, "challenger": challenger, "dimensions": {key: value.model_dump(mode="json") for key, value in dimensions.items()}}
    return MarketStatePosterior(posterior_id=_stable_id("market-state-posterior", payload), as_of=as_of, input_cutoff=as_of, baseline=baseline, challenger=challenger, dimensions=dimensions, overall_confidence=overall_confidence, entropy=sum(entropy_values) / len(entropy_values) if entropy_values else None, missingness=1 - (valid_count / total_count if total_count else 0), persistence=sum(persistence_values) / len(persistence_values) if persistence_values else None, transition_probabilities=transitions)


def posterior_can_influence_rank(posterior: MarketStatePosterior) -> bool:
    """A positive utility result alone cannot bypass the advisory boundary."""

    return False if posterior.advisory_only else bool(posterior.incremental_oos_net_utility is not None and posterior.incremental_oos_net_utility > 0)


@dataclass(frozen=True)
class ScenarioNode:
    step: int
    state: str
    probability: float


class ScenarioPath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "market-scenario-path.v1"
    path_id: str
    snapshot_id: str
    model_version: str
    nodes: tuple[ScenarioNode, ...]
    scenario_hash: str


def build_scenario_paths(snapshot_id: str, posterior: MarketStatePosterior, *, steps: int = 3, max_paths: int = 12) -> tuple[ScenarioPath, ...]:
    steps = max(1, min(int(steps), 12))
    max_paths = max(1, min(int(max_paths), 12))
    paths: list[ScenarioPath] = []
    for dimension, state in sorted((key, value.state) for key, value in posterior.dimensions.items() if value.state):
        if len(paths) >= max_paths:
            break
        row = posterior.dimensions[dimension]
        nodes = [ScenarioNode(step=0, state=f"{dimension}:{state}", probability=row.distribution.get(state or "", 0.0))]
        current = state
        for step in range(1, steps):
            next_state = max(row.transition_probabilities.get(current or "", {}).items(), key=lambda item: (-item[1], item[0]))[0] if row.transition_probabilities.get(current or "") else current
            probability = nodes[-1].probability * row.transition_probabilities.get(current or "", {}).get(next_state or "", 1.0)
            nodes.append(ScenarioNode(step=step, state=f"{dimension}:{next_state}", probability=probability))
            current = next_state
        encoded = [node.__dict__ for node in nodes]
        digest = _stable_id(snapshot_id, posterior.contract_version, dimension, encoded)
        paths.append(ScenarioPath(path_id=digest, snapshot_id=snapshot_id, model_version=posterior.contract_version, nodes=tuple(nodes), scenario_hash=digest))
    return tuple(paths)


def replay_scenario_path(path: ScenarioPath) -> bool:
    expected = _stable_id(path.snapshot_id, path.model_version, path.nodes[0].state.split(":", 1)[0] if path.nodes else "", [node.__dict__ for node in path.nodes])
    return expected == path.scenario_hash


def _state_label(value: float) -> str:
    return "positive" if value > 0 else "negative" if value < 0 else "neutral"


def _entropy(distribution: Mapping[str, float]) -> float:
    return -sum(value * math.log(value, 2) for value in distribution.values() if value > 0)


def _stable_id(*parts: Any) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "AdapterResult", "CoverageVector", "CoverageVectorRow", "DimensionPosterior", "EventObservation", "FIELD_CONTRACTS", "MarketStatePosterior", "PITObservation", "PITSelection", "Phase2Source", "Phase2Status", "SOURCE_CONTRACTS", "ScenarioNode", "ScenarioPath", "SourceContract", "assess_crypto_venue_data", "assess_option_oi_volume_sla", "build_coverage_vector", "build_market_state_posterior", "build_scenario_paths", "parse_coinmetrics_derivatives", "parse_corporate_expectations", "parse_event_consensus", "parse_fred_alfred", "parse_treasury_yield_curve", "posterior_can_influence_rank", "replay_scenario_path", "select_point_in_time", "source_contracts", "source_status",
]
