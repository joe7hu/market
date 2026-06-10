from __future__ import annotations

import json
from pathlib import Path

from investment_panel.core.db import db, init_db
from investment_panel.core.scoring import fundamental_score


def test_fundamental_score_ignores_null_metric_values(tmp_path: Path) -> None:
    """A present-but-null metric must not crash scoring (daily_screen regression).

    Yahoo/SEC fundamentals can return a key with a null value; ``float(None)``
    previously raised and aborted the entire daily screen, freezing the radar.
    """

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    metrics = {"revenue_growth": None, "gross_margin_trend": 0.1, "fcf_margin": "bad"}
    with db(db_path) as con:
        con.execute(
            "INSERT INTO equity_fundamentals (symbol, period_end, form_type, metrics) VALUES (?, ?, ?, ?)",
            ["NVDA", "2026-03-31", "10-Q", json.dumps(metrics)],
        )
        score = fundamental_score(con, "NVDA")

    # Only the one valid numeric metric (0.1) contributes; null and non-numeric
    # values are skipped instead of crashing.
    assert score == 50.0  # 45.0 base + min(18, 0.1 * 50) == 45 + 5
