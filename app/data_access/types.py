"""Panel data containers and status types."""

from __future__ import annotations
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable

from app.data_access.coerce import normalize_rows



SETUP_INSTRUCTIONS = (
    "No investment panel data is available yet. Configure `config.yaml`, run the "
    "daily screen job that imports Arco evidence and market data, then refresh the app."
)




@dataclass(frozen=True)
class DataStatus:
    """Status summary for data loaded into the API."""

    ready: bool
    message: str
    source: str = "empty"




@dataclass
class PanelData:
    """Normalized tables consumed by API routes."""

    status: DataStatus
    tables: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def table(self, name: str) -> Any:
        return self.tables.get(name)

    def rows(self, name: str) -> list[dict[str, Any]]:
        return normalize_rows(self.table(name))
