"""Thesis application workflow owner."""

from __future__ import annotations

from typing import Any

from app.data_access import mutations
from investment_panel.database.thesis import record_thesis_review, thesis_history, thesis_monitor_payload


class ThesisActions:
    """Sequence thesis writes with the monitor state returned to the caller."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def save(self, symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
        saved = mutations.save_thesis(self.config, symbol, fields)
        return {"thesis": saved, "thesis_monitor": thesis_monitor_payload(self.config)}

    def review(self, symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
        reviewed = record_thesis_review(self.config, symbol, fields)
        return {"review": reviewed, "thesis_monitor": thesis_monitor_payload(self.config)}

    def history(self, symbol: str) -> dict[str, Any]:
        return thesis_history(self.config, symbol)


__all__ = ["ThesisActions"]
