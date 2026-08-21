"""Typed, fail-closed provider policy for Market advisory agents.

The provider registry is the only owner of an agent command, its supported
models and reasoning levels, and the rate card used for experiment telemetry.
It deliberately does not contain any trade authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderRateCard:
    """A published per-million-token rate card for one exact model."""

    input_per_1m: float
    cached_input_per_1m: float | None
    output_per_1m: float
    source: str
    verified_on: str


@dataclass(frozen=True)
class ProviderModel:
    name: str
    reasoning_efforts: frozenset[str]
    rate_card: ProviderRateCard


@dataclass(frozen=True)
class AgentProvider:
    name: str
    command: str
    default_model: str
    models: Mapping[str, ProviderModel]


@dataclass(frozen=True)
class ProviderSelection:
    provider: str
    model: str
    reasoning_effort: str
    command: str
    rate_card: ProviderRateCard


# The price cards are deliberately keyed by provider *and* exact model.  These
# are the standard API rate cards verified on 2026-08-13, not the historical
# shared `agents.pricing.default` estimate. The Codex CLI may still report
# estimated token counts; that fact is persisted separately in telemetry.
_CODEX_LUNA_RATE = ProviderRateCard(
    input_per_1m=0.10,
    cached_input_per_1m=0.01,
    output_per_1m=0.60,
    source="https://developers.openai.com/api/docs/pricing",
    verified_on="2026-08-13",
)
_DEEPSEEK_FLASH_RATE = ProviderRateCard(
    input_per_1m=0.14,
    cached_input_per_1m=0.0028,
    output_per_1m=0.28,
    source="https://api-docs.deepseek.com/quick_start/pricing/",
    verified_on="2026-08-13",
)

_CATALOG: dict[str, AgentProvider] = {
    "codex": AgentProvider(
        name="codex",
        command="market-run-option-agent",
        default_model="gpt-5.6-luna",
        models={
            "gpt-5.6-luna": ProviderModel(
                name="gpt-5.6-luna",
                reasoning_efforts=frozenset({"minimal", "low", "medium", "high"}),
                rate_card=_CODEX_LUNA_RATE,
            ),
        },
    ),
    "deepseek": AgentProvider(
        name="deepseek",
        command="market-run-option-agent",
        default_model="deepseek-v4-flash",
        models={
            "deepseek-v4-flash": ProviderModel(
                name="deepseek-v4-flash",
                reasoning_efforts=frozenset({"low", "medium", "high"}),
                rate_card=_DEEPSEEK_FLASH_RATE,
            ),
        },
    ),
}


def resolve_provider_selection(
    provider: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> ProviderSelection:
    """Return one valid provider/model/effort tuple or reject it before work."""

    provider_name = str(provider or "").strip().lower()
    spec = _CATALOG.get(provider_name)
    if spec is None:
        raise ValueError(f"unsupported advisory provider: {provider_name or '<empty>'}")
    model_name = str(model or spec.default_model).strip()
    model_spec = spec.models.get(model_name)
    if model_spec is None:
        raise ValueError(f"model {model_name or '<empty>'} is not supported by provider {provider_name}")
    effort = str(reasoning_effort or "high").strip().lower()
    if effort not in model_spec.reasoning_efforts:
        supported = ", ".join(sorted(model_spec.reasoning_efforts))
        raise ValueError(
            f"reasoning_effort {effort or '<empty>'} is not supported by {provider_name}/{model_name}; "
            f"expected one of {supported}"
        )
    return ProviderSelection(
        provider=provider_name,
        model=model_name,
        reasoning_effort=effort,
        command=spec.command,
        rate_card=model_spec.rate_card,
    )


def provider_catalog() -> dict[str, dict[str, Any]]:
    """Safe registry view for settings UI; commands remain backend-owned."""

    return {
        name: {
            "default_model": spec.default_model,
            "models": {
                model_name: {"reasoning_efforts": sorted(model.reasoning_efforts)}
                for model_name, model in spec.models.items()
            },
        }
        for name, spec in _CATALOG.items()
    }


def validate_registry_command(provider: str, command: str | None) -> str:
    """Reject command substitution; return the registry-owned command."""

    selection = resolve_provider_selection(provider)
    supplied = str(command or "").strip()
    if supplied and supplied != selection.command:
        raise ValueError(
            f"option-agent command is registry-owned for {selection.provider}; "
            f"expected {selection.command}"
        )
    return selection.command


def rate_snapshot(selection: ProviderSelection) -> dict[str, Any]:
    card = selection.rate_card
    return {
        "provider": selection.provider,
        "model": selection.model,
        "input_per_1m": card.input_per_1m,
        "cached_input_per_1m": card.cached_input_per_1m,
        "output_per_1m": card.output_per_1m,
        "source": card.source,
        "verified_on": card.verified_on,
    }


def provider_cost(
    meta: Mapping[str, Any],
    *,
    selection: ProviderSelection,
) -> tuple[float | None, dict[str, Any]]:
    """Calculate using an exact provider/model rate, never a shared fallback."""

    reported_provider = str(meta.get("provider") or selection.provider).strip().lower()
    reported_model = str(meta.get("model") or selection.model).strip()
    if reported_provider != selection.provider or reported_model != selection.model:
        return None, {
            "pricing_status": "provider_identity_mismatch",
            "requested_provider": selection.provider,
            "requested_model": selection.model,
            "reported_provider": reported_provider,
            "reported_model": reported_model,
        }
    usage = meta.get("usage") if isinstance(meta.get("usage"), Mapping) else {}
    try:
        input_tokens = max(0, int(usage.get("input_tokens") or 0))
        output_tokens = max(0, int(usage.get("output_tokens") or 0))
        cached_input_tokens = min(input_tokens, max(0, int(usage.get("cached_input_tokens") or 0)))
    except (TypeError, ValueError):
        return None, {"pricing_status": "invalid_usage", "rate": rate_snapshot(selection)}
    card = selection.rate_card
    uncached_input_tokens = input_tokens - cached_input_tokens
    input_cost = uncached_input_tokens / 1_000_000 * card.input_per_1m
    if card.cached_input_per_1m is not None:
        input_cost += cached_input_tokens / 1_000_000 * card.cached_input_per_1m
    elif cached_input_tokens:
        return None, {"pricing_status": "cached_rate_unavailable", "rate": rate_snapshot(selection)}
    cost = round(input_cost + output_tokens / 1_000_000 * card.output_per_1m, 6)
    return cost, {
        "pricing_status": "provider_rate",
        "rate": rate_snapshot(selection),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "token_usage_observed": not bool(meta.get("estimated", False)),
    }


def provider_telemetry(meta: Mapping[str, Any], *, selection: ProviderSelection) -> dict[str, Any]:
    """Normalize identity and preserve rate provenance for a persisted run."""

    cost_usd, pricing = provider_cost(meta, selection=selection)
    usage = meta.get("usage") if isinstance(meta.get("usage"), Mapping) else {}
    return {
        # Persist the selected registry identity in typed columns. A provider
        # mismatch is evidence of a failed task, not a reason to rewrite its
        # requested identity as if it had safely executed elsewhere.
        "provider": selection.provider,
        "model": selection.model,
        "reasoning_effort": selection.reasoning_effort,
        "reported_provider": str(meta.get("provider") or selection.provider).strip().lower(),
        "reported_model": str(meta.get("model") or selection.model).strip(),
        "reported_reasoning_effort": str(meta.get("reasoning_effort") or selection.reasoning_effort).strip().lower(),
        "latency_ms": meta.get("latency_ms"),
        "usage": {
            "input_tokens": _token_count(usage.get("input_tokens")),
            "output_tokens": _token_count(usage.get("output_tokens")),
            "reasoning_tokens": _token_count(usage.get("reasoning_tokens")),
            "cached_input_tokens": _token_count(usage.get("cached_input_tokens")),
        },
        "tokens_estimated": bool(meta.get("estimated", False)),
        "cost_usd": cost_usd,
        "pricing": pricing,
    }


def _token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
