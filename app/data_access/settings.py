"""Settings and agent-control payloads and config writes."""

from __future__ import annotations
import os
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from app.scheduler import scheduler_status
from app.data_access.coerce import jsonable
from app.data_access.payloads import _runtime_metadata, status_payload
from investment_panel.core.config import update_agent_settings_config, update_research_sources_config


def slug(value: Any) -> str:
    text = str(value or "source").lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "source"



def settings_payload(config: dict[str, Any], panel_data: PanelData) -> dict[str, Any]:
    return {
        "status": status_payload(panel_data),
        "config": jsonable(config),
        "sources": research_source_inventory(config, panel_data),
        "agents": agent_control_payload(config),
        "integration": {
            "core_modules": ["investment_panel.database"],
            "helper_names": ["load_panel_data", "load_ticker_dossier_data"],
            "database_url": config.get("database", {}).get("url"),
            "arco_raw_dir": config.get("arco", {}).get("raw_dir"),
            "birdclaw_command": config.get("birdclaw", {}).get("command") or "Not configured",
        },
    }


def research_source_inventory(config: dict[str, Any], panel_data: PanelData) -> dict[str, Any]:
    """Configured live research sources plus latest run stats.

    Settings is the edit/delete surface for user-configured live pulls, so this
    list is derived from config and joined to source_runs. Dynamic blog/news
    source IDs are produced by the live ingestion helpers.
    """

    research = config.get("research_sources", {}) if isinstance(config.get("research_sources"), dict) else {}
    x = research.get("x", {}) if isinstance(research.get("x"), dict) else {}
    news = research.get("news", {}) if isinstance(research.get("news"), dict) else {}
    blogs = research.get("blogs", {}) if isinstance(research.get("blogs"), dict) else {}
    run_index = _source_run_index(panel_data.rows("source_runs"))
    rows: list[dict[str, Any]] = []

    list_id = str(x.get("list_id") or "").strip()
    if list_id:
        rows.append(
            _inventory_row(
                run_index,
                source_id="birdclaw_primary_tweets",
                family="x",
                kind="x_list",
                label="X list",
                value=list_id,
                config_path="research_sources.x.list_id",
                removable=True,
                enabled=bool(x.get("enabled", True)),
                capability="x_list",
            )
        )
    for handle in _config_list(x.get("priority_handles")):
        rows.append(
            _inventory_row(
                run_index,
                source_id="birdclaw_primary_tweets",
                family="x",
                kind="x_handle",
                label=f"@{handle}",
                value=handle,
                config_path="research_sources.x.priority_handles",
                removable=True,
                enabled=bool(x.get("enabled", True)),
                capability="x_account",
            )
        )
    for provider in _config_list(news.get("providers")):
        rows.append(
            _inventory_row(
                run_index,
                source_id=slug(f"news_{provider}"),
                family="news",
                kind="news_provider",
                label=provider,
                value=provider,
                config_path="research_sources.news.providers",
                removable=True,
                enabled=bool(news.get("enabled", True)),
                capability="news",
            )
        )
    for url in _config_list(blogs.get("substack_urls")):
        rows.append(
            _inventory_row(
                run_index,
                source_id=_blog_source_id(url),
                family="blog",
                kind="substack",
                label=_host(url),
                value=url,
                config_path="research_sources.blogs.substack_urls",
                removable=True,
                enabled=bool(blogs.get("enabled", True)),
                capability="substack",
            )
        )
    for url in _config_list(blogs.get("rss_urls")):
        rows.append(
            _inventory_row(
                run_index,
                source_id=_blog_source_id(url),
                family="blog",
                kind="rss",
                label=_host(url),
                value=url,
                config_path="research_sources.blogs.rss_urls",
                removable=True,
                enabled=bool(blogs.get("enabled", True)),
                capability="rss",
            )
        )
    return {"rows": rows, "count": len(rows)}


def _source_run_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        source_id = str(row.get("source_id") or "")
        if not source_id:
            continue
        capability = str(row.get("capability") or "")
        key = (source_id, capability)
        counts[key] = counts.get(key, 0) + 1
        current = index.get(key)
        if current is None or str(row.get("finished_at") or row.get("started_at") or "") > str(current.get("finished_at") or current.get("started_at") or ""):
            index[key] = dict(row)
    for key, count in counts.items():
        index[key]["run_count"] = count
    return index


def _inventory_row(
    run_index: dict[tuple[str, str], dict[str, Any]],
    *,
    source_id: str,
    family: str,
    kind: str,
    label: str,
    value: str,
    config_path: str,
    removable: bool,
    enabled: bool,
    capability: str,
) -> dict[str, Any]:
    run = run_index.get((source_id, capability), {})
    status = str(run.get("status") or ("configured" if enabled else "paused"))
    return {
        "source_id": source_id,
        "family": family,
        "kind": kind,
        "label": label,
        "value": value,
        "config_path": config_path,
        "removable": removable,
        "enabled": enabled,
        "latest_status": status,
        "latest_finished_at": run.get("finished_at"),
        "latest_capability": run.get("capability"),
        "latest_failure_detail": run.get("failure_detail"),
        "latest_item_count": int(run.get("item_count") or 0),
        "latest_ticker_count": int(run.get("ticker_count") or 0),
        "observed_run_count": int(run.get("run_count") or 0),
    }


def _config_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items: Iterable[Any] = re.split(r"[\n,]+", value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _blog_source_id(url: str) -> str:
    return slug(f"blog_{_host(url)}")


def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc or url
    except Exception:
        netloc = url
    return netloc.replace("www.", "") or "blog"




def agent_control_payload(config: dict[str, Any]) -> dict[str, Any]:
    agents = config.get("agents", {}) if isinstance(config.get("agents"), dict) else {}
    return {
        "config": jsonable(agents),
        "runtime": _runtime_metadata(config).get("agents", {}),
        "scheduler": scheduler_status(config),
        "model_overrides": {
            "codex_model": os.environ.get("MARKET_CODEX_MODEL", ""),
            "codex_reasoning_effort": os.environ.get("MARKET_CODEX_REASONING_EFFORT", ""),
            "codex_timeout_seconds": os.environ.get("MARKET_CODEX_TIMEOUT_SECONDS", "90"),
            "openai_model": os.environ.get("MARKET_OPENAI_MODEL", "gpt-5.2"),
            "openai_auth_mode": os.environ.get("MARKET_OPENAI_AUTH_MODE", "api_key_or_access_token"),
            "openai_max_output_tokens": os.environ.get("MARKET_OPENAI_MAX_OUTPUT_TOKENS", "2000"),
        },
    }
