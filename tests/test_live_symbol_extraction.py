from investment_panel.core.source_ingestion.live.common import extract_symbols
from investment_panel.jobs.update_content_sources import _symbols


def test_one_letter_symbols_require_cashtag_while_explicit_cashtags_survive() -> None:
    known = {"C", "F", "SP", "NVDA"}

    assert extract_symbols("C++ tooling, F-16 demand, and S&P breadth", known) == []
    assert extract_symbols("$C and $F are explicit positions", known) == ["C", "F"]
    assert extract_symbols("NVDA is explicit in the note", known) == ["NVDA"]


def test_content_job_uses_the_same_one_letter_guard() -> None:
    known = {"C", "F", "NVDA"}

    assert _symbols("C++ and F-16 are not ticker links", known) == []
    assert _symbols("$C, $F, and NVDA are explicit", known) == ["C", "F", "NVDA"]
