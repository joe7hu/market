from app.routers.panel import book_action_queue


def test_book_queue_ranks_current_opportunities_and_cash() -> None:
    rows = [
        {"source": "capital_action", "ticker": "AAA", "trade_rank": 2, "research_rank": 1},
        {"source": "capital_action", "ticker": "BBB", "trade_rank": 1, "research_rank": 2},
        {"source": "capital_action", "ticker": "CCC", "trade_rank": None, "research_rank": 3},
    ]

    queue = book_action_queue(rows)

    assert [row["ticker"] for row in queue] == ["BBB", "AAA", "CCC", None]
    assert [row["book_rank"] for row in queue] == [1, 2, 3, 4]
    assert queue[-1]["source"] == "cash"
    assert queue[-1]["action"] == "CASH"
