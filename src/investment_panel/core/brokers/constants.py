"""Broker status, IBKR tick, and account-tag constants."""

from __future__ import annotations



BROKER_BLOCKING_STATUSES = {
    "gateway_offline",
    "stale_session",
    "quote_entitlement_failure",
    "quote_only",
    "rate_limited",
    "stale_data",
    "malformed_symbol",
    "session_failure",
    "missing_dependency",
    "account_mode_mismatch",
    "disabled",
    "missing",
}


ADVISORY_AUTHORITY = "advisory_paper_only"


IBKR_ACCOUNT_TAGS = ",".join(
    [
        "TotalCashValue",
        "BuyingPower",
        "NetLiquidation",
        "InitMarginReq",
        "MaintMarginReq",
        "ExcessLiquidity",
        "UnrealizedPnL",
        "RealizedPnL",
    ]
)


IBKR_GENERIC_TICKS = "100,101,104,106,165,221,233"


IBKR_TICK_PRICE_FIELDS = {
    1: "bid",
    2: "ask",
    4: "last",
    9: "close",
    37: "mark_price",
    66: "bid",
    67: "ask",
    68: "last",
    75: "close",
}


IBKR_TICK_SIZE_FIELDS = {
    0: "bid_size",
    3: "ask_size",
    5: "last_size",
    8: "volume",
    21: "average_volume",
    27: "call_open_interest",
    28: "put_open_interest",
    29: "call_volume",
    30: "put_volume",
    74: "volume",
    87: "average_option_volume",
}


IBKR_TICK_GENERIC_FIELDS = {
    23: "historical_volatility",
    24: "implied_volatility",
    37: "mark_price",
}
