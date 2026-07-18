from __future__ import annotations

from investment_panel.database.panel_models import MODEL_ALIASES, QUERY_POLICIES


def test_panel_query_catalog_owns_alias_and_symbol_scope_policy() -> None:
    assert MODEL_ALIASES["ticker_memos"] == "research_packets"
    assert QUERY_POLICIES["research_packets"].symbol_scoped is True
    assert QUERY_POLICIES["research_packets"].exclude_future_rows is True
    assert QUERY_POLICIES["catalysts"].allow_symbol_less is True
    assert QUERY_POLICIES["options_ticker_signals"].custom_loader == "options_ticker_signals"


def test_every_query_alias_resolves_to_owned_policy() -> None:
    query_aliases = {
        alias: target
        for alias, target in MODEL_ALIASES.items()
        if target not in {"symbol_decision_snapshots"}
    }
    missing = sorted(target for target in query_aliases.values() if target not in QUERY_POLICIES)
    assert missing == []
