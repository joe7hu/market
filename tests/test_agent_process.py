from datetime import date

from investment_panel.infrastructure.postgres.agent_process import jsonable


def test_jsonable_serializes_database_dates_for_agent_requests() -> None:
    assert jsonable({"as_of": date(2026, 9, 22)}) == {"as_of": "2026-09-22"}
