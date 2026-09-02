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
    STALE = "STALE"


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

SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "macro.value": ("fred", "treasury"),
    "rates.nominal_yield": ("treasury",),
    "rates.real_yield": ("treasury",),
    "credit.spread": ("fred", "treasury"),
    "event.actual": ("trading_economics", "official-event-calendar"),
    "event.consensus": ("trading_economics",),
    "event.surprise": ("trading_economics",),
    "event.revision": ("trading_economics",),
    "corporate.expected": ("alphavantage",),
    "corporate.revision": ("alphavantage",),
    "option.open_interest": ("robinhood_history_full", "ibkr_options"),
    "option.volume": ("robinhood_history_full", "ibkr_options"),
    "crypto.depth": ("coinmetrics",),
    "crypto.funding": ("coinmetrics",),
    "crypto.basis": ("coinmetrics",),
    "crypto.open_interest": ("coinmetrics",),
    "crypto.liquidations": ("coinmetrics",),
    "positioning.flow": ("sec_13f",),
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
    "corporate.revision": {"definition": "Revision to a quarterly provider expectation, not an SEC actual.", "clock": "publication_at and available_at."},
    "crypto.venue_derivatives": {"definition": "Venue-identified derivative observation with executable depth identity.", "clock": "observed_at and available_at."},
    "crypto.depth": {"definition": "Venue-level executable derivative depth observation.", "clock": "observed_at and available_at."},
    "option.open_interest": {"definition": "Contract-level end-of-session open interest.", "clock": "observed_at and available_at."},
    "option.volume": {"definition": "Contract-level session volume.", "clock": "observed_at and available_at."},
    "positioning.flow": {"definition": "Reported institutional positioning and flow from SEC 13F filings.", "clock": "filing/publication clock and source available_at; never received_at."},
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
    ingest_run_id: str | None = None
    payload_id: int | None = None
    content_hash: str | None = None
    parent_snapshot_id: str | None = None
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
        if self.status in {Phase2Status.AVAILABLE, Phase2Status.FALLBACK} and _registry_status_for_fact(self.source_id) is Phase2Status.UNSUPPORTED:
            raise ValueError(f"source {self.source_id!r} is not a supported Phase 2 source")
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
    content_hash: str | None = None
    reason: str = ""


def source_status(source_id: str, *, env: Mapping[str, str] | None = None, has_history: bool = True) -> Phase2Status:
    """Return status without exposing credential values."""

    contract = SOURCE_CONTRACTS.get(str(source_id))
    if contract is None:
        return Phase2Status.UNSUPPORTED
    if str(source_id) in {Phase2Source.SHORT_INTEREST, Phase2Source.COINGECKO}:
        return Phase2Status.UNSUPPORTED
    active_env = os.environ if env is None else env
    if contract.credential_env and not str(active_env.get(contract.credential_env) or "").strip():
        return Phase2Status.MISSING_SOURCE
    if not has_history:
        return Phase2Status.MISSING_HISTORY
    return Phase2Status.AVAILABLE


def _registry_status_for_fact(source_id: str) -> Phase2Status:
    """Classify a fact without requiring a real provider secret."""

    contract = SOURCE_CONTRACTS.get(str(source_id))
    if contract is None:
        return Phase2Status.UNSUPPORTED
    env = {contract.credential_env: "configured"} if contract.credential_env else {}
    return source_status(source_id, env=env, has_history=True)


def _status_value(value: Phase2Status | str) -> Phase2Status | None:
    if isinstance(value, Phase2Status):
        return value
    try:
        return Phase2Status(str(value))
    except ValueError:
        return None


def source_contracts() -> tuple[SourceContract, ...]:
    return tuple(SOURCE_CONTRACTS[key] for key in sorted(SOURCE_CONTRACTS))


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _clock(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _observed_clock(value: Any) -> datetime | None:
    """Accept a date-only observation clock only as an explicit UTC date boundary."""

    parsed = _clock(value)
    if parsed is not None:
        return parsed
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            return datetime.fromisoformat(f"{value}T00:00:00+00:00")
        except ValueError:
            return None
    return None


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
    content_hash: str | None = None,
) -> PITObservation | None:
    observed_at = _observed_clock(row.get("observed_at") or row.get("date") or row.get("period_end") or row.get("period"))
    available_at = _clock(row.get("available_at"))
    if observed_at is None or available_at is None:
        return None
    return PITObservation(
        observation_id=_stable_id(source_id, field_name, row.get("date", row.get("period_end", row.get("period"))), value, available_at.isoformat(), content_hash),
        field_name=field_name, dimension=dimension, asset_class=asset_class, source_id=source_id,
        source_version=source_version, value=value, unit=str(row.get("unit") or "") or None,
        observed_at=observed_at, available_at=available_at,
        publication_at=_clock(row.get("publication_at")), release_at=_clock(row.get("release_at")),
        vintage_at=_observed_clock(row.get("vintage_at")), content_hash=content_hash, metadata=dict(metadata or {}),
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_fred_alfred(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    content_hash = _payload_hash(payload)
    status = source_status(Phase2Source.FRED, env=env, has_history=bool(_payload_rows(payload)))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id=Phase2Source.FRED, status=status, reason="FRED_API_KEY is absent or the vintage payload has no history")
    observations = tuple(
        item for row in _payload_rows(payload)
        if (item := _observation(row, source_id="fred", field_name=str(row.get("field_name") or row.get("series_id") or "macro.value"), dimension=str(row.get("dimension") or "growth/inflation"), asset_class="macro", source_version=str(payload.get("source_version") or "fred-alfred.v1"), value=_number(row.get("value")), content_hash=content_hash, metadata={"series_id": row.get("series_id"), "vintage": row.get("vintage_at")})) is not None and item.value is not None
    )
    return AdapterResult(source_id="fred", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=observations, content_hash=content_hash, reason="" if observations else "no usable FRED values")


def parse_treasury_yield_curve(payload: Mapping[str, Any]) -> AdapterResult:
    rows = _payload_rows(payload)
    if not rows:
        return AdapterResult(source_id="treasury", status=Phase2Status.MISSING_HISTORY, reason="Treasury yield curve payload has no history")
    observations = tuple(
        item for row in rows
        if (item := _observation(row, source_id="treasury", field_name=str(row.get("field_name") or ("rates.real_yield" if row.get("real") else "rates.nominal_yield")), dimension="rates", asset_class="rates", source_version=str(payload.get("source_version") or "treasury-curve.v1"), value=_number(row.get("value")), content_hash=_payload_hash(payload), metadata={"tenor": row.get("tenor"), "real": bool(row.get("real"))})) is not None and item.value is not None
    )
    return AdapterResult(source_id="treasury", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=observations, content_hash=_payload_hash(payload), reason="" if observations else "no usable Treasury values")


def parse_event_consensus(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    rows = _payload_rows(payload)
    content_hash = _payload_hash(payload)
    status = source_status(Phase2Source.TRADING_ECONOMICS, env=env, has_history=bool(rows))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id="trading_economics", status=status, reason="Trading Economics credential or event history is unavailable")
    observations: list[PITObservation] = []
    for row in rows:
        observed_at = _observed_clock(row.get("event_at") or row.get("observed_at") or row.get("date"))
        available_at = _clock(row.get("available_at"))
        actual = _number(row.get("actual"))
        consensus = _number(row.get("consensus", row.get("forecast")))
        if observed_at is None or available_at is None:
            continue
        surprise = round(actual - consensus, 12) if actual is not None and consensus is not None else None
        revision = _number(row.get("revision"))
        clocks = {"publication_at": _clock(row.get("publication_at")), "release_at": _clock(row.get("release_at"))}
        identity = ("trading_economics", row.get("event_id", row.get("name")), observed_at.isoformat(), content_hash)
        for field_name, value in (("event.actual", actual), ("event.consensus", consensus), ("event.surprise", surprise), ("event.revision", revision)):
            if value is None:
                continue
            observations.append(EventObservation(
                observation_id=_stable_id(*identity, field_name, value),
                field_name=field_name, dimension="event risk", asset_class="macro", source_id="trading_economics",
                source_version=str(payload.get("source_version") or "trading-economics.v1"), value=value, unit=str(row.get("unit") or "") or None,
                observed_at=observed_at, available_at=available_at, **clocks,
                status=Phase2Status.AVAILABLE, actual=actual, consensus=consensus, surprise=surprise, revision=revision,
                content_hash=content_hash, metadata={"event_id": row.get("event_id"), "name": row.get("name")},
            ))
    return AdapterResult(source_id="trading_economics", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=tuple(observations), content_hash=content_hash, reason="" if observations else "no event clocks or values")


def parse_corporate_expectations(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    rows = _payload_rows(payload)
    content_hash = _payload_hash(payload)
    status = source_status(Phase2Source.ALPHAVANTAGE, env=env, has_history=bool(rows))
    if status is not Phase2Status.AVAILABLE:
        return AdapterResult(source_id="alphavantage", status=status, reason="Alpha Vantage credential or quarterly history is unavailable")
    parsed: list[PITObservation] = []
    for row in rows:
        metadata = {"ticker": row.get("ticker"), "period_end": row.get("period_end"), "metric": row.get("metric")}
        for field_name, value in (("corporate.expected", row.get("expected", row.get("value"))), ("corporate.revision", row.get("revision"))):
            numeric = _number(value)
            if numeric is None:
                continue
            item = _observation(row, source_id="alphavantage", field_name=field_name, dimension="corporate cycle", asset_class="equity", source_version=str(payload.get("source_version") or "alphavantage-quarterly.v1"), value=numeric, content_hash=content_hash, metadata=metadata)
            if item is not None and item.value is not None:
                parsed.append(item)
    observations = tuple(parsed)
    return AdapterResult(source_id="alphavantage", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=observations, content_hash=content_hash, reason="" if observations else "no usable quarterly expectations")


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
    return AdapterResult(source_id="coinmetrics", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=tuple(observations), content_hash=_payload_hash(payload), reason="" if observations else "venue identity, depth, or derivative values are missing")


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


def parse_option_history(source_id: str, payload: Mapping[str, Any]) -> AdapterResult:
    """Adapt the existing Robinhood/IBKR history seams with the OI/volume SLA."""
    rows = _payload_rows(payload)
    content_hash = _payload_hash(payload)
    assessment = assess_option_oi_volume_sla(rows)
    observations: list[PITObservation] = []
    if assessment["status"] == Phase2Status.AVAILABLE.value:
        for row in rows:
            for field_name, key in (("option.open_interest", "open_interest"), ("option.volume", "volume")):
                item = _observation(row, source_id=source_id, field_name=field_name, dimension="option liquidity", asset_class="equity", source_version=str(payload.get("source_version") or f"{source_id}.v1"), value=_number(row.get(key)), content_hash=content_hash, metadata={"contract_id": row.get("contract_id"), "underlying": row.get("underlying"), "sla": assessment})
                if item is not None and item.value is not None:
                    observations.append(item)
    return AdapterResult(source_id=source_id, status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=tuple(observations), content_hash=content_hash, reason="" if observations else assessment["reason"])


def parse_sec_positioning(payload: Mapping[str, Any]) -> AdapterResult:
    """Adapt the existing SEC/13F seam; no actuals are invented."""
    rows = _payload_rows(payload)
    content_hash = _payload_hash(payload)
    observations: list[PITObservation] = []
    for row in rows:
        value = _number(row.get("value", row.get("shares", row.get("notional"))))
        item = _observation(row, source_id="sec_13f", field_name=str(row.get("field_name") or "positioning.flow"), dimension="positioning", asset_class="equity", source_version=str(payload.get("source_version") or "sec-13f.v1"), value=value, content_hash=content_hash, metadata={"filer": row.get("filer"), "issuer": row.get("issuer"), "filing_date": row.get("filing_date")})
        if item is not None and item.value is not None:
            observations.append(item)
    return AdapterResult(source_id="sec_13f", status=Phase2Status.AVAILABLE if observations else Phase2Status.MISSING_HISTORY, observations=tuple(observations), content_hash=content_hash, reason="" if observations else "SEC/13F seam has no PIT positioning history")


@dataclass(frozen=True)
class PITSelection:
    selected: tuple[PITObservation, ...]
    retained: tuple[PITObservation, ...]
    conflicts: tuple[tuple[str, ...], ...]
    missing_fields: tuple[str, ...]


def _source_is_usable(source_id: str, source_lifecycle: Mapping[str, Mapping[str, Any]] | None, cutoff: datetime | None = None) -> bool:
    if source_lifecycle is None:
        return True
    state = source_lifecycle.get(source_id)
    if isinstance(state, Sequence) and not isinstance(state, (str, bytes)):
        valid: list[Mapping[str, Any]] = []
        for item in state:
            if not isinstance(item, Mapping):
                continue
            effective = _clock(item.get("effective_at"))
            if item.get("effective_at") is not None and effective is None:
                continue
            if cutoff is None or effective is None or effective <= cutoff:
                valid.append(item)
        state = max(valid, key=lambda item: _clock(item.get("effective_at")) or datetime.min.replace(tzinfo=UTC), default=None)
    return bool(state and state.get("enabled") is True and str(state.get("operational_state") or "").lower() == "active")


def _source_rank(field_name: str, source_id: str) -> tuple[int, str]:
    priority = SOURCE_PRIORITY.get(field_name, ())
    return (priority.index(source_id) if source_id in priority else len(priority), source_id)


def select_point_in_time(
    observations: Sequence[PITObservation],
    cutoff: datetime,
    *,
    fields: Sequence[str] = (),
    source_lifecycle: Mapping[str, Mapping[str, Any]] | None = None,
) -> PITSelection:
    cutoff = _utc(cutoff)
    excluded = {Phase2Status.UNSUPPORTED, Phase2Status.STALE, Phase2Status.MISSING_SOURCE, Phase2Status.MISSING_HISTORY}
    eligible = tuple(sorted((row for row in observations if row.status not in excluded and _registry_status_for_fact(row.source_id) is not Phase2Status.UNSUPPORTED and _source_is_usable(row.source_id, source_lifecycle, cutoff) and _utc(row.observed_at) <= cutoff and _utc(row.available_at) <= cutoff), key=lambda row: (row.field_name, _utc(row.observed_at), _utc(row.available_at), row.source_id, row.observation_id)))
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
        selected_row = min(group, key=lambda row: (_source_rank(row.field_name, row.source_id), -_utc(row.available_at).timestamp(), row.observation_id))
        preferred = SOURCE_PRIORITY.get(selected_row.field_name, ())
        if preferred and selected_row.source_id != preferred[0]:
            selected_row = selected_row.model_copy(update={
                "status": Phase2Status.FALLBACK,
                "confidence": selected_row.confidence * 0.75,
                "metadata": {**selected_row.metadata, "fallback_reason": "preferred source unavailable or removed"},
            })
        selected.append(selected_row)
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
    ingest_run_ids: tuple[str, ...] = ()
    input_content_hash: str
    parent_snapshot_id: str | None = None


def build_coverage_vector(
    as_of: datetime,
    strategies: Mapping[str, Mapping[str, Sequence[str]]],
    observations: Sequence[PITObservation],
    *,
    source_lifecycle: Mapping[str, Mapping[str, Any]] | None = None,
    source_statuses: Mapping[str, Phase2Status | str] | None = None,
    ingest_run_ids: Sequence[str] = (),
    input_content_hash: str | None = None,
    parent_snapshot_id: str | None = None,
) -> CoverageVector:
    rows: list[CoverageVectorRow] = []
    for strategy, expressions in sorted(strategies.items()):
        for expression, fields in sorted(expressions.items()):
            required = tuple(sorted(set(fields)))
            selection = select_point_in_time(observations, as_of, fields=required, source_lifecycle=source_lifecycle)
            by_field = {row.field_name: row for row in selection.selected}
            blockers = [f"missing:{field}" for field in selection.missing_fields]
            for row in observations:
                if row.field_name not in required or row.status not in {Phase2Status.MISSING_SOURCE, Phase2Status.MISSING_HISTORY, Phase2Status.UNSUPPORTED, Phase2Status.STALE}:
                    continue
                if _utc(row.observed_at) <= _utc(as_of) and _utc(row.available_at) <= _utc(as_of):
                    blockers.append(f"source_{row.status.value.lower()}:{row.source_id}")
            missing_sources = sorted({source for source, status_value in (source_statuses or {}).items() if _status_value(status_value) in {Phase2Status.MISSING_SOURCE, Phase2Status.MISSING_HISTORY, Phase2Status.STALE, Phase2Status.UNSUPPORTED}})
            if missing_sources and blockers:
                blockers.extend(f"source_{_status_value((source_statuses or {})[source]).value.lower()}:{source}" for source in missing_sources)
            if selection.conflicts:
                blockers.append("conflicted:source_observations")
            sources = tuple(sorted({row.source_id for row in by_field.values()}))
            statuses = {row.status for row in by_field.values()}
            if selection.conflicts or Phase2Status.CONFLICTED in statuses:
                status = Phase2Status.CONFLICTED
            elif blockers:
                status = Phase2Status.MISSING_SOURCE if missing_sources else Phase2Status.MISSING_HISTORY
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
    content_hash = input_content_hash or _stable_id("phase2-input", tuple(sorted(row.content_hash or row.observation_id for row in observations)))
    return CoverageVector(vector_id=_stable_id("coverage-vector", _utc(as_of).isoformat(), payload, tuple(sorted(ingest_run_ids)), content_hash, parent_snapshot_id), as_of=as_of, rows=tuple(rows), ingest_run_ids=tuple(sorted(set(ingest_run_ids))), input_content_hash=content_hash, parent_snapshot_id=parent_snapshot_id)


STATE_LABELS = ("negative", "neutral", "positive")


class DimensionPosterior(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Phase2Status
    state: str | None = None
    distribution: dict[str, float] = Field(default_factory=dict)
    entropy: float | None = None
    persistence: float | None = None
    transition_probabilities: dict[str, dict[str, float]] = Field(default_factory=dict)
    change_point_probability: float | None = Field(default=None, ge=0, le=1)
    log_likelihood: float | None = None
    missingness: float
    uncertainty: str
    sample_count: int = Field(ge=0)


class MarketStatePosterior(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "market-state-posterior.v1"
    posterior_id: str
    as_of: datetime
    input_cutoff: datetime
    status: Phase2Status
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
    ingest_run_ids: tuple[str, ...] = ()
    input_content_hash: str
    parent_snapshot_id: str | None = None
    phase1_evidence_verified: bool = False
    phase1_evidence_id: str | None = None
    phase1_evidence_hash: str | None = None
    phase1_strategy_revision_id: int | None = None
    phase1_strategy_key: str | None = None

    @model_validator(mode="after")
    def preserve_advisory_boundary(self) -> "MarketStatePosterior":
        if self.rank_authorized or not self.advisory_only:
            raise ValueError("Phase 2 posterior is advisory-only")
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("posterior timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("posterior as_of and input_cutoff must match")
        return self


def _hmm_dimension(labels: Sequence[str]) -> tuple[dict[str, float], dict[str, dict[str, float]], float, float, float]:
    """Run a fixed, bounded three-state HMM with explicit noisy emissions."""

    transition = ((0.80, 0.15, 0.05), (0.15, 0.70, 0.15), (0.05, 0.15, 0.80))
    alpha = [1 / 3] * 3
    log_likelihood = 0.0
    change_point_probability = 0.0
    for index, label in enumerate(labels):
        emission = [0.15, 0.70, 0.15] if label == "neutral" else [0.075, 0.075, 0.075]
        label_index = STATE_LABELS.index(label)
        if label != "neutral":
            emission[label_index] = 0.85
        if index == 0:
            predicted = alpha
        else:
            predicted = [sum(alpha[left] * transition[left][right] for left in range(3)) for right in range(3)]
        likelihood = sum(predicted[state] * emission[state] for state in range(3))
        weighted = [predicted[state] * emission[state] for state in range(3)]
        alpha = [value / likelihood for value in weighted]
        log_likelihood += math.log(max(likelihood, 1e-12))
        if index:
            joint_change = sum(
                alpha[right] * (previous_alpha[left] * transition[left][right] / max(predicted[right], 1e-12))
                for left in range(3) for right in range(3) if left != right
            )
            change_point_probability = max(change_point_probability, min(1.0, max(0.0, joint_change)))
        previous_alpha = alpha
    distribution = {state: round(alpha[index], 12) for index, state in enumerate(STATE_LABELS)}
    transitions = {state: {next_state: transition[index][next_index] for next_index, next_state in enumerate(STATE_LABELS)} for index, state in enumerate(STATE_LABELS)}
    persistence = sum(transition[index][index] for index in range(3)) / 3
    return distribution, transitions, persistence, change_point_probability, log_likelihood


def build_market_state_posterior(
    observations: Sequence[PITObservation],
    *,
    as_of: datetime,
    max_observations: int = 500,
    source_lifecycle: Mapping[str, Mapping[str, Any]] | None = None,
    source_statuses: Mapping[str, Phase2Status | str] | None = None,
    ingest_run_ids: Sequence[str] = (),
    input_content_hash: str | None = None,
    parent_snapshot_id: str | None = None,
) -> MarketStatePosterior:
    """Build bounded observable and hidden-state posterior models deterministically."""

    cutoff = _utc(as_of)
    excluded = {Phase2Status.UNSUPPORTED, Phase2Status.STALE, Phase2Status.MISSING_SOURCE, Phase2Status.MISSING_HISTORY}
    candidates = tuple(row for row in observations if row.status not in excluded and _source_is_usable(row.source_id, source_lifecycle, cutoff) and _utc(row.observed_at) <= cutoff and _utc(row.available_at) <= cutoff)
    field_names = tuple(sorted({row.field_name for row in candidates}))
    selected = select_point_in_time(candidates, cutoff, fields=field_names, source_lifecycle=source_lifecycle)
    rows = sorted(selected.selected, key=lambda row: (row.dimension, _utc(row.observed_at), row.observation_id))[-max(1, min(max_observations, 500)) :]
    by_dimension: dict[str, list[PITObservation]] = defaultdict(list)
    for row in rows:
        by_dimension[row.dimension].append(row)
    dimensions: dict[str, DimensionPosterior] = {}
    for dimension in sorted(by_dimension):
        group = by_dimension[dimension]
        valid = [row for row in group if row.status in {Phase2Status.AVAILABLE, Phase2Status.FALLBACK} and _number(row.value) is not None]
        labels = [_state_label(float(row.value)) for row in valid]
        distribution, transitions, persistence, change_probability, log_likelihood = _hmm_dimension(labels) if labels else ({}, {}, None, None, None)
        missingness = 1 - (len(valid) / len(group) if group else 0)
        dimension_status = Phase2Status.AVAILABLE if valid else Phase2Status.UNSUPPORTED if all(row.status is Phase2Status.UNSUPPORTED for row in group) else Phase2Status.MISSING_HISTORY
        dimensions[dimension] = DimensionPosterior(
            status=dimension_status,
            state=max(distribution, key=lambda state: (-distribution[state], state)) if distribution else None,
            distribution=distribution,
            entropy=_entropy(distribution) if distribution else None,
            persistence=persistence,
            transition_probabilities=transitions,
            change_point_probability=change_probability,
            log_likelihood=log_likelihood,
            missingness=missingness,
            uncertainty="bounded HMM posterior with noisy emissions; frequency baseline retained separately" if valid else "missing point-in-time history",
            sample_count=len(valid),
        )
    valid_count = sum(item.sample_count for item in dimensions.values())
    total_count = len(rows)
    source_status_values = []
    for value in (source_statuses or {}).values():
        try:
            source_status_values.append(value if isinstance(value, Phase2Status) else Phase2Status(value))
        except ValueError:
            continue
    degraded_statuses = {
        Phase2Status.MISSING_SOURCE, Phase2Status.MISSING_HISTORY,
        Phase2Status.STALE, Phase2Status.UNSUPPORTED,
    }
    if Phase2Status.MISSING_SOURCE in source_status_values:
        status = Phase2Status.MISSING_SOURCE
    elif Phase2Status.STALE in source_status_values:
        status = Phase2Status.STALE
    elif Phase2Status.MISSING_HISTORY in source_status_values:
        status = Phase2Status.MISSING_HISTORY
    elif Phase2Status.UNSUPPORTED in source_status_values:
        status = Phase2Status.UNSUPPORTED
    elif any(row.status is Phase2Status.CONFLICTED for row in rows):
        status = Phase2Status.CONFLICTED
    elif any(row.status is Phase2Status.FALLBACK for row in rows):
        status = Phase2Status.FALLBACK
    else:
        status = Phase2Status.AVAILABLE if valid_count else Phase2Status.MISSING_HISTORY
    degraded_count = sum(value in degraded_statuses for value in source_status_values)
    baseline_dimensions = {}
    for key, group in by_dimension.items():
        labels = [_state_label(float(row.value)) for row in group if row.status in {Phase2Status.AVAILABLE, Phase2Status.FALLBACK} and _number(row.value) is not None]
        counts = Counter(labels)
        baseline_dimensions[key] = {label: (counts[label] / len(labels) if labels else 0.0) for label in STATE_LABELS}
    baseline = {"method": "observable-frequency.v1", "dimensions": baseline_dimensions, "status": status.value, "degraded_source_count": degraded_count}
    challenger = {"method": "hmm-noisy-emission.v1", "semantics": "bounded three-state forward HMM with posterior transition-change probability", "dimensions": {key: value.transition_probabilities for key, value in dimensions.items()}, "status": "advisory", "incremental_oos_net_utility": None, "degraded_source_count": degraded_count}
    transitions = {key: value.transition_probabilities for key, value in dimensions.items()}
    payload = {"as_of": cutoff.isoformat(), "status": status.value, "baseline": baseline, "challenger": challenger, "dimensions": {key: value.model_dump(mode="json") for key, value in dimensions.items()}}
    content_hash = input_content_hash or _stable_id("phase2-input", tuple(sorted(row.content_hash or row.observation_id for row in rows)))
    return MarketStatePosterior(
        posterior_id=_stable_id("market-state-posterior", payload, tuple(sorted(ingest_run_ids)), content_hash, parent_snapshot_id),
        as_of=as_of,
        input_cutoff=as_of,
        status=status,
        baseline=baseline,
        challenger=challenger,
        dimensions=dimensions,
        overall_confidence=(valid_count / max(total_count + degraded_count, 1)) * (sum(row.confidence for row in rows if row.status in {Phase2Status.AVAILABLE, Phase2Status.FALLBACK} and _number(row.value) is not None) / valid_count if valid_count else 0.0) if total_count or degraded_count else 0.0,
        entropy=sum(item.entropy for item in dimensions.values() if item.entropy is not None) / len([item for item in dimensions.values() if item.entropy is not None]) if any(item.entropy is not None for item in dimensions.values()) else None,
        missingness=1 - (valid_count / max(total_count + degraded_count, 1) if total_count or degraded_count else 0),
        persistence=sum(item.persistence for item in dimensions.values() if item.persistence is not None) / len([item for item in dimensions.values() if item.persistence is not None]) if any(item.persistence is not None for item in dimensions.values()) else None,
        transition_probabilities=transitions,
        ingest_run_ids=tuple(sorted(set(ingest_run_ids))),
        input_content_hash=content_hash,
        parent_snapshot_id=parent_snapshot_id,
    )


def posterior_can_influence_rank(posterior: MarketStatePosterior, *, runtime: Any | None = None, phase1_evidence: Mapping[str, Any] | None = None) -> bool:
    """Require independent, verified Phase 1 evidence in addition to utility."""

    if runtime is None or posterior.advisory_only or not posterior.rank_authorized:
        return False
    if (not posterior.phase1_evidence_id or not posterior.phase1_evidence_hash
            or posterior.phase1_strategy_revision_id is None or not posterior.phase1_strategy_key):
        return False
    # Caller mappings and model_copy fields are not evidence.  The only
    # authorizing fact is the canonical PostgreSQL Phase 1 result and its
    # sealed evidence predicate.
    try:
        with runtime.read() as connection:
            row = connection.execute(
                """SELECT result.result_kind, result.input_hash,
                          trial.input_hash AS trial_input_hash,
                          dossier.strategy_revision_id, revision.id AS canonical_strategy_revision_id,
                          revision.strategy_key,
                          revision.status AS strategy_status,
                          evaluation.verdict AS evaluation_verdict,
                          evaluation.metrics AS evaluation_metrics,
                          analysis.research_evidence_complete(result.id) AS complete
                   FROM analysis.trial_result result
                   JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
                   JOIN analysis.validation_dossier dossier ON dossier.research_trial_id = trial.id
                   JOIN analysis.strategy_revision revision ON revision.id = dossier.strategy_revision_id
                   JOIN analysis.strategy_evaluation evaluation
                     ON evaluation.research_trial_id = trial.id
                    AND evaluation.strategy_revision_id = revision.id
                    AND evaluation.evaluation_type = 'out_of_sample'
                  WHERE result.id = %s""",
                [posterior.phase1_evidence_id],
            ).fetchone()
    except Exception:
        return False
    if not row or not row["complete"] or row["result_kind"] != "validation":
        return False
    if str(row["input_hash"]) != str(row["trial_input_hash"]) or str(row["input_hash"]) != posterior.phase1_evidence_hash:
        return False
    if (not row["strategy_revision_id"] or not row["strategy_key"]
            or row["strategy_status"] not in {"active", "superseded"}
            or int(row["canonical_strategy_revision_id"]) != posterior.phase1_strategy_revision_id
            or str(row["strategy_key"]) != posterior.phase1_strategy_key):
        return False
    if row["evaluation_verdict"] != "pass":
        return False
    metrics = row["evaluation_metrics"] if isinstance(row["evaluation_metrics"], Mapping) else {}
    utility = metrics.get("lower_confidence_net_utility_after_costs")
    try:
        return math.isfinite(float(utility)) and float(utility) > 0
    except (TypeError, ValueError):
        return False


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
    parent_snapshot_id: str
    posterior_id: str
    model_version: str
    ingest_run_ids: tuple[str, ...] = ()
    input_content_hash: str
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
        digest = _stable_id(snapshot_id, snapshot_id, posterior.posterior_id, posterior.contract_version, posterior.input_content_hash, tuple(posterior.ingest_run_ids), dimension, encoded)
        paths.append(ScenarioPath(path_id=digest, snapshot_id=snapshot_id, parent_snapshot_id=snapshot_id, posterior_id=posterior.posterior_id, model_version=posterior.contract_version, ingest_run_ids=posterior.ingest_run_ids, input_content_hash=posterior.input_content_hash, nodes=tuple(nodes), scenario_hash=digest))
    return tuple(paths)


def replay_scenario_path(path: ScenarioPath) -> bool:
    expected = _stable_id(path.snapshot_id, path.parent_snapshot_id, path.posterior_id, path.model_version, path.input_content_hash, tuple(path.ingest_run_ids), path.nodes[0].state.split(":", 1)[0] if path.nodes else "", [node.__dict__ for node in path.nodes])
    return path.path_id == path.scenario_hash and expected == path.scenario_hash


def _state_label(value: float) -> str:
    return "positive" if value > 0 else "negative" if value < 0 else "neutral"


def _entropy(distribution: Mapping[str, float]) -> float:
    return -sum(value * math.log(value, 2) for value in distribution.values() if value > 0)


def _stable_id(*parts: Any) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def phase2_input_content_hash(observations: Sequence[PITObservation]) -> str:
    """Hash the exact immutable facts used by a posterior or coverage vector."""

    facts = [
        {
            "observation_id": row.observation_id,
            "source_id": row.source_id,
            "content_hash": row.content_hash,
            "ingest_run_id": row.ingest_run_id,
            "payload_id": row.payload_id,
            "parent_snapshot_id": row.parent_snapshot_id,
            "field_name": row.field_name,
            "dimension": row.dimension,
            "observed_at": row.observed_at.isoformat(),
            "available_at": row.available_at.isoformat(),
            "value": row.value,
            "status": row.status.value,
        }
        for row in observations
    ]
    return _stable_id("phase2-facts.v1", sorted(facts, key=lambda item: item["observation_id"]))


__all__ = [
    "AdapterResult", "CoverageVector", "CoverageVectorRow", "DimensionPosterior", "EventObservation", "FIELD_CONTRACTS", "MarketStatePosterior", "PITObservation", "PITSelection", "Phase2Source", "Phase2Status", "SOURCE_CONTRACTS", "ScenarioNode", "ScenarioPath", "SourceContract", "assess_crypto_venue_data", "assess_option_oi_volume_sla", "build_coverage_vector", "build_market_state_posterior", "build_scenario_paths", "parse_coinmetrics_derivatives", "parse_corporate_expectations", "parse_event_consensus", "parse_fred_alfred", "parse_option_history", "parse_sec_positioning", "parse_treasury_yield_curve", "posterior_can_influence_rank", "replay_scenario_path", "select_point_in_time", "source_contracts", "source_status",
]
