"""Validation and merge rules for user-editable Market settings."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import re
import socket
from typing import Any, Iterable
from urllib.parse import urlsplit

from investment_panel.core.agent_providers import (
    resolve_provider_selection,
    validate_registry_command,
)


def apply_agent_settings_update(
    current: dict[str, Any] | None,
    update: dict[str, Any],
) -> dict[str, Any]:
    """Return one validated agents section while preserving non-editable fields."""

    if not isinstance(update, dict):
        raise ValueError("agent settings must be an object")
    next_agents = dict(current or {})
    if "option_agent" in update:
        previous = _mapping(next_agents.get("option_agent"))
        candidate = _mapping_required(update["option_agent"], "agent settings")
        sanitized = _sanitize_option_agent_settings(candidate, current=previous)
        if isinstance(sanitized.get("context_sources"), dict) and isinstance(
            previous.get("context_sources"), dict
        ):
            sanitized["context_sources"] = {
                **previous["context_sources"],
                **sanitized["context_sources"],
            }
        next_agents["option_agent"] = {**previous, **sanitized}
    if "thesis_monitor" in update:
        previous = _mapping(next_agents.get("thesis_monitor"))
        candidate = _mapping_required(
            update["thesis_monitor"], "thesis_monitor settings"
        )
        sanitized = _sanitize_thesis_monitor_settings(candidate, current=previous)
        next_agents["thesis_monitor"] = {**previous, **sanitized}
    return next_agents


def apply_research_sources_update(
    current: dict[str, Any] | None,
    update: dict[str, Any],
    *,
    resolve_urls: bool = True,
) -> dict[str, Any]:
    """Return one validated research-sources section."""

    if not isinstance(update, dict):
        raise ValueError("research source settings must be an object")
    next_sources = dict(current or {})
    sanitizers = {
        "x": _sanitize_research_x,
        "news": _sanitize_research_news,
        "blogs": lambda value: _sanitize_research_blogs(
            value, resolve_urls=resolve_urls
        ),
    }
    for name, sanitizer in sanitizers.items():
        if name not in update:
            continue
        previous = _mapping(next_sources.get(name))
        candidate = _mapping_required(update[name], f"{name} settings")
        next_sources[name] = {**previous, **sanitizer(candidate)}
    return next_sources


def validate_public_http_url(value: Any) -> str:
    """Return a public HTTP(S) URL or reject an SSRF-capable destination."""

    return resolve_public_http_url(value).url


@dataclass(frozen=True)
class ResolvedPublicHttpUrl:
    """One public URL and the address checked for its next connection."""

    url: str
    hostname: str
    authority: str
    address: str


def resolve_public_http_url(value: Any) -> ResolvedPublicHttpUrl:
    """Resolve one public HTTP(S) URL for an address-pinned request."""

    url = str(value or "").strip()
    if not url or any(ord(char) < 32 for char in url):
        raise ValueError("source URL is empty or contains control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source URL is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("source URL port is invalid")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("source URL must use http or https")
    if not parsed.hostname:
        raise ValueError("source URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source URL must not include credentials")

    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("source URL host is invalid") from exc
    if host == "localhost" or host.endswith(".localhost") or "%" in host:
        raise ValueError("source URL must use a public host")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    resolved_port = port or default_port
    address = _public_addresses(host, resolved_port)[0]
    header_host = f"[{host}]" if ":" in host else host
    authority = header_host if resolved_port == default_port else f"{header_host}:{resolved_port}"
    return ResolvedPublicHttpUrl(
        url=url,
        hostname=host,
        authority=authority,
        address=address,
    )


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = [ip_address(host)]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("source URL host could not be resolved") from exc
        addresses = list(dict.fromkeys(
            ip_address(str(sockaddr[0]).split("%", 1)[0])
            for _family, _type, _proto, _canonname, sockaddr in resolved
        ))
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("source URL must not resolve to a private or non-routable address")
    return [address.compressed for address in addresses]


def _sanitize_option_agent_settings(
    value: Any,
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("agent settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = _strict_bool(value["enabled"], "enabled")
    if "timeout_seconds" in value:
        clean["timeout_seconds"] = _bounded_int(
            value["timeout_seconds"], "timeout_seconds", minimum=10, maximum=900
        )
    if "thesis_limit" in value:
        clean["thesis_limit"] = _bounded_int(
            value["thesis_limit"], "thesis_limit", minimum=0, maximum=50
        )
    if "postmortem_limit" in value:
        clean["postmortem_limit"] = _bounded_int(
            value["postmortem_limit"], "postmortem_limit", minimum=0, maximum=50
        )
    current = current or {}
    provider = str(value.get("provider", current.get("provider", "codex"))).strip().lower()
    model = _clean_token(value.get("model", current.get("model", "")), "model", maximum=80)
    effort = str(
        value.get("reasoning_effort", current.get("reasoning_effort", ""))
    ).strip().lower()
    selection = resolve_provider_selection(provider, model or None, effort or None)
    if "command" in value:
        validate_registry_command(selection.provider, value["command"])
    if {"provider", "model", "reasoning_effort"} & set(value):
        clean.update(
            {
                "provider": selection.provider,
                "model": selection.model,
                "reasoning_effort": selection.reasoning_effort,
            }
        )
    if "auto_run_seconds" in value:
        clean["auto_run_seconds"] = _bounded_int(
            value["auto_run_seconds"], "auto_run_seconds", minimum=0, maximum=604800
        )
    if "max_runs_per_day" in value:
        clean["max_runs_per_day"] = _bounded_int(
            value["max_runs_per_day"], "max_runs_per_day", minimum=0, maximum=48
        )
    if "context_sources" in value:
        sources = value["context_sources"]
        if not isinstance(sources, dict):
            raise ValueError("context_sources must be an object of name -> bool")
        allowed = {
            "fundamentals",
            "technicals",
            "ownership",
            "news",
            "social_signals",
            "catalysts",
            "portfolio",
            "decision",
        }
        clean["context_sources"] = {
            key: _strict_bool(item, f"context_sources.{key}")
            for key, item in sources.items()
            if key in allowed
        }
    return clean


def _sanitize_thesis_monitor_settings(
    value: Any,
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("thesis_monitor settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = _strict_bool(value["enabled"], "enabled")
    current = current or {}
    provider = str(value.get("provider", current.get("provider", "codex"))).strip().lower()
    model = _clean_token(value.get("model", current.get("model", "")), "model", maximum=80)
    effort = str(
        value.get("reasoning_effort", current.get("reasoning_effort", ""))
    ).strip().lower()
    selection = resolve_provider_selection(provider, model or None, effort or None)
    if {"provider", "model", "reasoning_effort"} & set(value):
        clean.update(
            {
                "provider": selection.provider,
                "model": selection.model,
                "reasoning_effort": selection.reasoning_effort,
            }
        )
    if "prompt_version" in value:
        clean["prompt_version"] = _clean_token(
            value["prompt_version"], "prompt_version", maximum=80
        )
    if "concurrency" in value:
        clean["concurrency"] = _bounded_int(
            value["concurrency"], "concurrency", minimum=1, maximum=2
        )
    if "evidence_items_per_symbol" in value:
        clean["evidence_items_per_symbol"] = _bounded_int(
            value["evidence_items_per_symbol"],
            "evidence_items_per_symbol",
            minimum=1,
            maximum=12,
        )
    if "preopen_enabled" in value:
        clean["preopen_enabled"] = _strict_bool(
            value["preopen_enabled"], "preopen_enabled"
        )
    if "material_event_enabled" in value:
        clean["material_event_enabled"] = _strict_bool(
            value["material_event_enabled"], "material_event_enabled"
        )
    if "debounce_minutes" in value:
        clean["debounce_minutes"] = _bounded_int(
            value["debounce_minutes"], "debounce_minutes", minimum=1, maximum=240
        )
    if "max_material_runs_per_symbol_per_day" in value:
        clean["max_material_runs_per_symbol_per_day"] = _bounded_int(
            value["max_material_runs_per_symbol_per_day"],
            "max_material_runs_per_symbol_per_day",
            minimum=0,
            maximum=8,
        )
    clean["authority"] = "research_ranking_only"
    return clean


def _sanitize_research_x(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("x settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = _strict_bool(value["enabled"], "enabled")
    if "list_id" in value:
        clean["list_id"] = _clean_token(value["list_id"], "list_id", maximum=64)
    if "priority_handles" in value:
        clean["priority_handles"] = _clean_str_list(
            value["priority_handles"],
            "priority_handles",
            max_items=50,
            strip_prefix="@",
        )
    if "limit" in value:
        clean["limit"] = _bounded_int(value["limit"], "limit", minimum=1, maximum=200)
    if "account_fetch_cap" in value:
        clean["account_fetch_cap"] = _bounded_int(
            value["account_fetch_cap"], "account_fetch_cap", minimum=0, maximum=50
        )
    return clean


def _sanitize_research_news(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("news settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = _strict_bool(value["enabled"], "enabled")
    if "providers" in value:
        clean["providers"] = _clean_str_list(
            value["providers"], "providers", max_items=20
        )
    if "limit" in value:
        clean["limit"] = _bounded_int(value["limit"], "limit", minimum=1, maximum=200)
    return clean


def _sanitize_research_blogs(
    value: Any,
    *,
    resolve_urls: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("blogs settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = _strict_bool(value["enabled"], "enabled")
    if "substack_urls" in value:
        urls = _clean_str_list(
            value["substack_urls"], "substack_urls", max_items=50
        )
        clean["substack_urls"] = (
            [validate_public_http_url(url) for url in urls]
            if resolve_urls
            else urls
        )
    if "rss_urls" in value:
        urls = _clean_str_list(value["rss_urls"], "rss_urls", max_items=50)
        clean["rss_urls"] = (
            [validate_public_http_url(url) for url in urls]
            if resolve_urls
            else urls
        )
    return clean


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _mapping_required(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _clean_token(value: Any, name: str, *, maximum: int) -> str:
    token = str(value or "").strip()
    if len(token) > maximum:
        raise ValueError(f"{name} is too long")
    return token


def _clean_str_list(
    value: Any,
    name: str,
    *,
    max_items: int,
    strip_prefix: str = "",
) -> list[str]:
    if isinstance(value, str):
        items: Iterable[Any] = re.split(r"[\n,]+", value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ValueError(f"{name} must be a list or comma-separated string")
    out: list[str] = []
    for item in items:
        token = str(item or "").strip()
        if strip_prefix and token.startswith(strip_prefix):
            token = token[len(strip_prefix) :]
        if not token:
            continue
        if len(token) > 240:
            raise ValueError(f"{name} entry is too long")
        if token not in out:
            out.append(token)
    if len(out) > max_items:
        raise ValueError(f"{name} accepts at most {max_items} entries")
    return out


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        parsed = int(value)
    else:
        raise ValueError(f"{name} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _strict_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"{name} must be a boolean")


__all__ = [
    "apply_agent_settings_update",
    "apply_research_sources_update",
    "resolve_public_http_url",
    "validate_public_http_url",
]
