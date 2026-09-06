from investment_panel.jobs import postgres_refresh


def test_full_refresh_cli_uses_canonical_workflow(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(postgres_refresh, "full", lambda path, **kw: calls.append((path, kw)) or {"status": "ok"})
    monkeypatch.setattr("sys.argv", ["market-full-refresh", "--config", "custom.yaml", "--continue-on-error"])
    postgres_refresh.main_full()
    assert calls == [("custom.yaml", {"continue_on_error": True})]
    assert '"status": "ok"' in capsys.readouterr().out
