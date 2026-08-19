"""Non-authoritative research add-ons for a completed option-history run."""

from __future__ import annotations

from typing import Any

from investment_panel.database.options_distribution_shift import materialize_surface_shift
from investment_panel.database.runtime import DatabaseRuntime


def surface_shift_run_summary(
    runtime: DatabaseRuntime, symbol: str, *, snapshot_id: int,
    capture_generation_id: int, analysis_run_id: Any, model_revision: str,
    mode: str, as_of: Any
) -> dict[str, Any]:
    """Keep a research failure from invalidating the authoritative publication."""
    try:
        result = materialize_surface_shift(
            runtime, symbol=symbol, as_of=as_of, snapshot_id=snapshot_id,
            capture_generation_id=capture_generation_id,
            current_analysis_run_id=analysis_run_id, model_revision=model_revision, mode=mode,
        )
        return {"surface_shift_state": result["evidence_state"]}
    except Exception as error:
        return {
            "surface_shift_state": "unavailable",
            "surface_shift_error": type(error).__name__,
        }
