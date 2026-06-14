"""Settings and agent-control payloads and config writes."""

from __future__ import annotations
import os
from pathlib import Path
import re
from typing import Any, Iterable

from app.data_access.config import project_root
from app.data_access.coerce import jsonable
from app.data_access.payloads import _runtime_metadata, status_payload



def settings_payload(config: dict[str, Any], panel_data: PanelData) -> dict[str, Any]:
    return {
        "status": status_payload(panel_data),
        "config": jsonable(config),
        "agents": agent_control_payload(config),
        "integration": {
            "core_modules": ["investment_panel.core.panel"],
            "helper_names": ["load_panel_data", "load_ticker_dossier_data"],
            "duckdb_path": config.get("database", {}).get("duckdb_path"),
            "arco_raw_dir": config.get("arco", {}).get("raw_dir"),
            "birdclaw_command": config.get("birdclaw", {}).get("command") or "Not configured",
        },
    }




def agent_control_payload(config: dict[str, Any]) -> dict[str, Any]:
    agents = config.get("agents", {}) if isinstance(config.get("agents"), dict) else {}
    return {
        "config": jsonable(agents),
        "runtime": _runtime_metadata(config).get("agents", {}),
        "scheduler": {
            "enabled": os.environ.get("MARKET_SCHEDULER_ENABLED", "1"),
            "agent_refresh_seconds": os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0"),
            "radar_refresh_seconds": os.environ.get("MARKET_RADAR_REFRESH_SECONDS", "900"),
            "source_refresh_seconds": os.environ.get("MARKET_SOURCE_REFRESH_SECONDS", "3600"),
            "learning_refresh_seconds": os.environ.get("MARKET_LEARNING_REFRESH_SECONDS", "21600"),
            "radar_option_source": os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood"),
        },
        "model_overrides": {
            "codex_model": os.environ.get("MARKET_CODEX_MODEL", ""),
            "codex_reasoning_effort": os.environ.get("MARKET_CODEX_REASONING_EFFORT", ""),
            "codex_timeout_seconds": os.environ.get("MARKET_CODEX_TIMEOUT_SECONDS", "90"),
            "openai_model": os.environ.get("MARKET_OPENAI_MODEL", "gpt-5.2"),
            "openai_auth_mode": os.environ.get("MARKET_OPENAI_AUTH_MODE", "api_key_or_access_token"),
            "openai_max_output_tokens": os.environ.get("MARKET_OPENAI_MAX_OUTPUT_TOKENS", "2000"),
        },
    }




def update_agent_settings_config(config_path: str | Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the editable agent-command block without rewriting the whole file."""

    path = Path(config_path) if config_path else project_root() / "config.yaml"
    if not path.is_absolute():
        path = project_root() / path
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
        next_agents["option_agent"] = {**current, **_sanitize_option_agent_settings(payload["option_agent"])}
    raw["agents"] = next_agents
    _write_yaml_top_level_block(path, "agents", {"agents": next_agents})
    return raw




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
    return clean


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed




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
