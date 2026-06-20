"""Editable config.yaml mutations for app settings surfaces."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable


def update_agent_settings_config(config_path: str | Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the editable agent-command block without rewriting the whole file."""

    path = _resolve_config_path(config_path)
    raw = _read_yaml_config(path)
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    next_agents = dict(agents)
    for key in ("option_thesis", "option_postmortem"):
        if key not in payload:
            continue
        current = next_agents.get(key) if isinstance(next_agents.get(key), dict) else {}
        next_agents[key] = {**current, **_sanitize_agent_settings(payload[key])}
    if "option_agent" in payload:
        current = next_agents.get("option_agent") if isinstance(next_agents.get("option_agent"), dict) else {}
        sanitized = _sanitize_option_agent_settings(payload["option_agent"])
        if isinstance(sanitized.get("context_sources"), dict) and isinstance(current.get("context_sources"), dict):
            sanitized["context_sources"] = {**current["context_sources"], **sanitized["context_sources"]}
        next_agents["option_agent"] = {**current, **sanitized}
    raw["agents"] = next_agents
    _write_yaml_top_level_block(path, "agents", {"agents": next_agents})
    return raw


def update_research_sources_config(config_path: str | Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the editable research/social-source block."""

    path = _resolve_config_path(config_path)
    raw = _read_yaml_config(path)
    current = raw.get("research_sources") if isinstance(raw.get("research_sources"), dict) else {}
    next_block = dict(current)
    if "x" in payload:
        prev = next_block.get("x") if isinstance(next_block.get("x"), dict) else {}
        next_block["x"] = {**prev, **_sanitize_research_x(payload["x"])}
    if "news" in payload:
        prev = next_block.get("news") if isinstance(next_block.get("news"), dict) else {}
        next_block["news"] = {**prev, **_sanitize_research_news(payload["news"])}
    if "blogs" in payload:
        prev = next_block.get("blogs") if isinstance(next_block.get("blogs"), dict) else {}
        next_block["blogs"] = {**prev, **_sanitize_research_blogs(payload["blogs"])}
    raw["research_sources"] = next_block
    _write_yaml_top_level_block(path, "research_sources", {"research_sources": next_block})
    return raw


def _resolve_config_path(config_path: str | Path | None) -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    if not config_path:
        return repo_root / "config.yaml"
    path = Path(config_path)
    return path if path.is_absolute() else repo_root / path


def _read_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("PyYAML is required to update config.yaml") from exc
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a mapping")
    return raw


def _write_yaml_top_level_block(path: Path, key: str, block: dict[str, Any]) -> None:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("PyYAML is required to update config.yaml") from exc
    rendered = yaml.safe_dump(block, sort_keys=False, default_flow_style=False).rstrip()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    start = next((idx for idx, line in enumerate(lines) if re.match(rf"^{re.escape(key)}:\s*$", line)), None)
    if start is None:
        suffix = "\n\n" if original.strip() else ""
        path.write_text(f"{original.rstrip()}{suffix}{rendered}\n", encoding="utf-8")
        return
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line and not line.startswith((" ", "\t", "#")):
            break
        end += 1
    next_lines = [*lines[:start], *rendered.splitlines(), *lines[end:]]
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def _sanitize_agent_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("agent settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = bool(value["enabled"])
    if "command" in value:
        command = str(value["command"] or "").strip()
        if len(command) > 240:
            raise ValueError("agent command is too long")
        clean["command"] = command
    if "timeout_seconds" in value:
        clean["timeout_seconds"] = _bounded_int(value["timeout_seconds"], "timeout_seconds", minimum=10, maximum=900)
    if "limit" in value:
        clean["limit"] = _bounded_int(value["limit"], "limit", minimum=0, maximum=50)
    return clean


def _sanitize_option_agent_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("agent settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = bool(value["enabled"])
    if "command" in value:
        command = str(value["command"] or "").strip()
        if len(command) > 240:
            raise ValueError("agent command is too long")
        clean["command"] = command
    if "timeout_seconds" in value:
        clean["timeout_seconds"] = _bounded_int(value["timeout_seconds"], "timeout_seconds", minimum=10, maximum=900)
    if "thesis_limit" in value:
        clean["thesis_limit"] = _bounded_int(value["thesis_limit"], "thesis_limit", minimum=0, maximum=50)
    if "postmortem_limit" in value:
        clean["postmortem_limit"] = _bounded_int(value["postmortem_limit"], "postmortem_limit", minimum=0, maximum=50)
    if "provider" in value:
        provider = str(value["provider"] or "").strip().lower()
        if provider not in {"codex", "openai"}:
            raise ValueError("provider must be 'codex' or 'openai'")
        clean["provider"] = provider
    if "model" in value:
        clean["model"] = _clean_token(value["model"], "model", maximum=80)
    if "reasoning_effort" in value:
        effort = str(value["reasoning_effort"] or "").strip().lower()
        if effort and effort not in {"low", "medium", "high", "minimal"}:
            raise ValueError("reasoning_effort must be low, medium, high, or minimal")
        clean["reasoning_effort"] = effort
    if "auto_run_seconds" in value:
        clean["auto_run_seconds"] = _bounded_int(value["auto_run_seconds"], "auto_run_seconds", minimum=0, maximum=604800)
    if "max_runs_per_day" in value:
        clean["max_runs_per_day"] = _bounded_int(value["max_runs_per_day"], "max_runs_per_day", minimum=0, maximum=48)
    if "context_sources" in value:
        sources = value["context_sources"]
        if not isinstance(sources, dict):
            raise ValueError("context_sources must be an object of name -> bool")
        allowed = {"fundamentals", "technicals", "ownership", "news", "social_signals", "catalysts", "portfolio", "decision"}
        clean["context_sources"] = {key: bool(val) for key, val in sources.items() if key in allowed}
    return clean


def _sanitize_research_x(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("x settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = bool(value["enabled"])
    if "list_id" in value:
        clean["list_id"] = _clean_token(value["list_id"], "list_id", maximum=64)
    if "priority_handles" in value:
        clean["priority_handles"] = _clean_str_list(value["priority_handles"], "priority_handles", max_items=50, strip_prefix="@")
    if "limit" in value:
        clean["limit"] = _bounded_int(value["limit"], "limit", minimum=1, maximum=200)
    if "account_fetch_cap" in value:
        clean["account_fetch_cap"] = _bounded_int(value["account_fetch_cap"], "account_fetch_cap", minimum=0, maximum=50)
    return clean


def _sanitize_research_news(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("news settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = bool(value["enabled"])
    if "providers" in value:
        clean["providers"] = _clean_str_list(value["providers"], "providers", max_items=20)
    if "limit" in value:
        clean["limit"] = _bounded_int(value["limit"], "limit", minimum=1, maximum=200)
    return clean


def _sanitize_research_blogs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("blogs settings must be an object")
    clean: dict[str, Any] = {}
    if "enabled" in value:
        clean["enabled"] = bool(value["enabled"])
    if "substack_urls" in value:
        clean["substack_urls"] = _clean_str_list(value["substack_urls"], "substack_urls", max_items=50)
    if "rss_urls" in value:
        clean["rss_urls"] = _clean_str_list(value["rss_urls"], "rss_urls", max_items=50)
    return clean


def _clean_token(value: Any, name: str, *, maximum: int) -> str:
    token = str(value or "").strip()
    if len(token) > maximum:
        raise ValueError(f"{name} is too long")
    return token


def _clean_str_list(value: Any, name: str, *, max_items: int, strip_prefix: str = "") -> list[str]:
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
            token = token[len(strip_prefix):]
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
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed
