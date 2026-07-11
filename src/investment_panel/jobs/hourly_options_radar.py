"""PostgreSQL hourly option-publication refresh without file locks."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.jobs import refresh_options_radar


def run(config_path: str | None = None, symbols: list[str] | None = None, **_kwargs: Any) -> dict[str, Any]:
    result = refresh_options_radar.run_signal_only(config_path, symbols=symbols)
    return {"cadence": "hourly_deterministic", "agent_workers": "daily_premarket_only", **result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols), indent=2, default=str))


if __name__ == "__main__":
    main()
