"""Recovery-specific panel-model seam kept out of the general catalog."""

from __future__ import annotations

from typing import Any, Iterable


RECOVERY_MODELS = frozenset({
    "option_recovery_funnel", "option_recovery_event", "option_recovery_opportunity",
    "option_recovery_family_performance", "option_recovery_agent_provenance", "option_recovery_health",
})


def recovery_panel_models(runtime: Any, names: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Load bounded PostgreSQL recovery projections without broadening the catalog owner."""

    from investment_panel.database.options_recovery_read import RecoveryReadRepository

    return RecoveryReadRepository(runtime).panel_models(names)
