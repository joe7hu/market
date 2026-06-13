"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from investment_panel.core.source_ingestion.raw_sources.constants import URL_KEYS


def add_ref_symbol(index: dict[str, set[str]], ref: str, symbol: str) -> None:
    if ref and symbol:
        index.setdefault(ref, set()).add(symbol)


def origin_ref_if_exact(ref: str) -> str:
    public = browser_public_url(ref)
    redacted = redacted_url(ref)
    if public and redacted.rstrip("/") == public.rstrip("/"):
        return public
    return ""


def raw_overlap_symbols(
    symbols: list[str],
    refs: list[str],
    analysis_refs: dict[str, Any],
    *,
    tweet_id: str = "",
) -> list[str]:
    overlap_symbols: set[str] = set()
    url_symbols = analysis_refs.get("url_symbols") or {}
    tweet_symbols = analysis_refs.get("tweet_symbols") or {}
    for ref in refs:
        if ref:
            overlap_symbols.update(url_symbols.get(ref, set()))
    if tweet_id:
        overlap_symbols.update(tweet_symbols.get(tweet_id, set()))
    return sorted(set(symbols) & overlap_symbols)


def urls_from_value(value: Any) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in URL_KEYS and item:
                urls.add(str(item))
            urls.update(urls_from_value(item))
    elif isinstance(value, list):
        for item in value:
            urls.update(urls_from_value(item))
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        urls.add(value)
    return urls


def host_from_url(url: str) -> str:
    stripped = url.replace("https://", "").replace("http://", "")
    return stripped.split("/", 1)[0]


def redacted_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return host_from_url(url)
    if not parsed.scheme or not parsed.netloc:
        return host_from_url(url)
    path = parsed.path or ""
    host, port = parsed_host_port(parsed)
    if not host:
        return host_from_url(url)
    return f"{parsed.scheme}://{host}{port}{path}"


def browser_public_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host, port = parsed_host_port(parsed)
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    return f"{parsed.scheme}://{host}{port}"


def parsed_host_port(parsed: Any) -> tuple[str, str]:
    try:
        host = parsed.hostname
    except ValueError:
        return "", ""
    if not host:
        return "", ""
    try:
        port_value = parsed.port
    except ValueError:
        port_value = None
    port = f":{port_value}" if port_value else ""
    return host, port


def latest_value(values: list[Any]) -> Any:
    candidates = [value for value in values if value not in (None, "")]
    return max(candidates) if candidates else None


def normalize_timestamp(value: Any) -> Any:
    if value in (None, ""):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        return value


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def trim(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}..."
