"""Detect forward-only sell-off events without pulling option chains."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.option_events import OptionEventRepository


def run(config_path: str | None = "config.yaml", *, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the effective universe every five minutes using retained spot data."""

    config = load_config(config_path)
    reference = now or datetime.now(UTC)
    repository = OptionEventRepository(runtime_for_config(config))
    detected = repository.detect_events(now=reference)
    return {
        **detected,
        "status": detected.get("status") or "ok",
        "checked_at": reference.isoformat(),
        "capture_health": repository.capture_health(now=reference),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":
    main()
