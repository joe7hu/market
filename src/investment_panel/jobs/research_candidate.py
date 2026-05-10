"""Generate a packet and deterministic memo for one ticker."""

from __future__ import annotations

import argparse
import json

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.research import build_research_packet, generate_deterministic_memo, store_report, write_packet


def run(symbol: str, config_path: str | None = None) -> dict[str, str]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        packet = build_research_packet(con, symbol.upper())
        packet_path = write_packet(packet, config.packet_dir)
        report = generate_deterministic_memo(packet)
        report_id = store_report(con, symbol.upper(), "research_memo", report, packet)
    return {"symbol": symbol.upper(), "packet_path": str(packet_path), "report_id": report_id}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.symbol, args.config), indent=2))


if __name__ == "__main__":
    main()

