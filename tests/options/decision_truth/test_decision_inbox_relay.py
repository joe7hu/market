from __future__ import annotations

from investment_panel.jobs import decision_inbox


def test_fixed_owner_sender_uses_absolute_command_without_shell(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_GBRAIN_TELEGRAM_OWNER_RELAY_URL", raising=False)
    monkeypatch.setenv("MARKET_GBRAIN_TELEGRAM_OWNER_RELAY_COMMAND", "/tmp/fixed-owner-relay --bounded")
    calls: dict[str, object] = {}

    def fake_run(command, *, input, text, capture_output, timeout, check):
        calls.update({"command": command, "input": input, "text": text, "capture_output": capture_output, "timeout": timeout, "check": check})

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(decision_inbox.subprocess, "run", fake_run)
    sender = decision_inbox._fixed_owner_sender()
    sender("QQQ · BUY")

    assert calls == {
        "command": ["/tmp/fixed-owner-relay", "--bounded"],
        "input": "QQQ · BUY",
        "text": True,
        "capture_output": True,
        "timeout": 15,
        "check": False,
    }
