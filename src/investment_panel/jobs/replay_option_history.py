"""Replay one immutable option capture generation without mutating prior evidence."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.analysis.history_v3 import MODEL_REVISION
from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_history_v3 import OptionHistoryV3Materializer


def run(
    capture_generation_id: int,
    *,
    snapshot_id: int,
    model_revision: str = MODEL_REVISION,
    config_path: str | None = "config.yaml",
) -> dict[str, Any]:
    runtime = runtime_for_config(load_config(config_path))
    return OptionHistoryV3Materializer(runtime).materialize(
        snapshot_id=snapshot_id,
        capture_generation_id=capture_generation_id,
        model_revision=model_revision,
        code_version="history-v3-replay",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_generation_id", type=int)
    parser.add_argument("--snapshot-id", required=True, type=int)
    parser.add_argument("--model-revision", default=MODEL_REVISION)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.capture_generation_id, snapshot_id=args.snapshot_id, model_revision=args.model_revision, config_path=args.config), default=str))


if __name__ == "__main__":
    main()
