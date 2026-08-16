import investment_panel.database.option_ticket_read as ticket_read


def test_reconcile_radar_summary_marks_persisted_quote_as_frozen_when_market_closed(monkeypatch) -> None:
    monkeypatch.setattr(ticket_read, "is_market_open", lambda now: False)

    result = ticket_read.reconcile_radar_summary(
        [{"latest_complete_quote_time": "2026-08-13T16:00:30-04:00"}],
        [{"state": "SETUP", "execution_ready": False}],
    )

    assert result == [{
        "latest_complete_quote_time": "2026-08-13T16:00:30-04:00",
        "market_session": "closed",
        "frozen_to_last_rth": True,
        "ready_count": 0,
        "setup_count": 1,
        "watch_count": 0,
    }]


def test_reconcile_radar_summary_marks_current_session_during_rth(monkeypatch) -> None:
    monkeypatch.setattr(ticket_read, "is_market_open", lambda now: True)

    result = ticket_read.reconcile_radar_summary([{}], [])

    assert result[0]["market_session"] == "rth"
    assert result[0]["frozen_to_last_rth"] is False
