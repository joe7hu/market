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


def rematerialize_complete_captures(
    *,
    config_path: str | None = "config.yaml",
    symbol: str = "QQQ",
    model_revision: str = MODEL_REVISION,
) -> dict[str, Any]:
    """Append a fresh v3 run for the latest capture, then every complete capture.

    This deliberately never replaces raw evidence or prior analysis runs.  It is
    the release operation for a revised deterministic model and is safe to rerun.
    """

    runtime = runtime_for_config(load_config(config_path))
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT snapshot.id AS snapshot_id, generation.id AS capture_generation_id,
                   (snapshot.latest_complete_generation_id = generation.id) AS is_latest
            FROM raw.option_capture_generation generation
            JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
            WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = 'history_full'
              AND generation.capture_state = 'complete'
            ORDER BY (snapshot.latest_complete_generation_id = generation.id) DESC,
                     snapshot.slot_at DESC NULLS LAST, generation.id DESC
            """,
            [symbol.upper()],
        ).fetchall()
    materializer = OptionHistoryV3Materializer(runtime)
    results = [
        materializer.materialize(
            snapshot_id=int(row["snapshot_id"]),
            capture_generation_id=int(row["capture_generation_id"]),
            model_revision=model_revision,
            code_version="history-v3-post-fix-rematerialize",
        )
        for row in rows
    ]
    return {
        "symbol": symbol.upper(), "model_revision": model_revision,
        "rematerialized_captures": len(results),
        "latest_capture_first": bool(rows and rows[0]["is_latest"]),
        "analysis_run_ids": [result["analysis_run_id"] for result in results],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_generation_id", type=int, nargs="?")
    parser.add_argument("--snapshot-id", type=int)
    parser.add_argument("--all-complete", action="store_true")
    parser.add_argument("--model-revision", default=MODEL_REVISION)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    if args.all_complete:
        result = rematerialize_complete_captures(
            config_path=args.config, model_revision=args.model_revision,
        )
    elif args.capture_generation_id is not None and args.snapshot_id is not None:
        result = run(
            args.capture_generation_id, snapshot_id=args.snapshot_id,
            model_revision=args.model_revision, config_path=args.config,
        )
    else:
        parser.error("pass capture_generation_id with --snapshot-id, or --all-complete")
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
