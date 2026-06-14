"""Invariants binding the app-side panel contracts to the core read-model registry.

The registry (``investment_panel.core.panel.READ_MODELS``) is the single source
of truth for which read models the API can serve. These tests fail if a scope or
endpoint contract references a table name that has no loader — the drift class
that previously left ``option_flow_features`` requested but unservable.
"""

from __future__ import annotations

import re
from pathlib import Path

from app import panel_contracts
from investment_panel.core.panel import read_model_names


def _contract_referenced_tables() -> set[str]:
    names: set[str] = set()
    for tables in panel_contracts.PANEL_SCOPE_TABLES.values():
        names.update(tables)
    names.update(panel_contracts.WATCHLIST_SECTION_TABLES)
    names.update(panel_contracts.TICKER_TABLES)
    names.update(panel_contracts.ENDPOINT_TABLES.values())
    return names


def test_every_contract_table_has_a_loader() -> None:
    registered = read_model_names()
    unservable = sorted(_contract_referenced_tables() - registered)
    assert unservable == [], (
        f"panel contracts reference read models with no registry loader: {unservable}"
    )


def test_repair_tables_are_real_read_models() -> None:
    registered = read_model_names()
    for repair_set in (panel_contracts.DECISION_REPAIR_TABLES, panel_contracts.SOURCE_REPAIR_TABLES):
        missing = sorted(repair_set - registered)
        assert missing == [], f"repair tables with no registry loader: {missing}"


def test_frontend_snapshot_keys_cover_backend_panel_manifest() -> None:
    api_panel_data = Path("frontend/src/apiPanelData.ts").read_text(encoding="utf-8")
    table_keys_block = re.search(r"const TABLE_KEYS: Record<string, keyof PanelData> = \{(?P<body>.*?)\n\};", api_panel_data, re.S)
    assert table_keys_block is not None
    frontend_keys = dict(re.findall(r"\s*([a-zA-Z0-9_]+): \"([a-zA-Z0-9_]+)\"", table_keys_block.group("body")))

    expected = {
        table_name: panel_contracts.frontend_key_for_table(table_name)
        for table_name in panel_contracts.panel_snapshot_table_names()
    }

    missing = sorted(set(expected) - set(frontend_keys))
    mismatched = {
        table_name: {"expected": expected_key, "actual": frontend_keys.get(table_name)}
        for table_name, expected_key in expected.items()
        if frontend_keys.get(table_name) != expected_key
    }
    assert missing == []
    assert mismatched == {}
