"""IBKR option-chain collector for the 10x radar.

IBKR's OPRA feed delivers price, greeks (IV/delta/gamma/vega/theta), open
interest, and volume for an option contract through one authenticated API — no
scraping and no HTTP 429, unlike the TradingView + yfinance combination it
replaces as the radar's liquidity source.

Delayed data (``reqMarketDataType(3)``) is sufficient for the LEAP radar: these
are 1-2 year contracts, so a 15-minute delay is irrelevant, and delayed data
works without an OPRA real-time entitlement (verified via the probe in
``scripts/ibkr_market_data_probe.py``).

This module keeps the deterministic, unit-testable core (expiry/strike selection
and IBKR tick parsing) separate from the live IB API orchestration so the parsing
contract can be tested without a running Gateway.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timezone
from typing import Any

from investment_panel.core.option_scan import RADAR_BASELINE_CALL_STRIKE_OTM_HI, RADAR_CALL_STRIKE_OTM_HI, RADAR_CALL_STRIKE_OTM_LO, RADAR_LOTTERY_CALL_STRIKE_OTM_LO

# IBKR tick types we read. Live model greeks arrive as tickType 13; delayed model
# greeks as 83. Open interest for a contract arrives as 27 (call) / 28 (put).
# Volume is 8 (live) / 74 (delayed). Prices: bid/ask/last 1/2/4 live, 66/67/68
# delayed; close 9 live / 75 delayed.
LIVE_MODEL_GREEK_TICK = 13
DELAYED_MODEL_GREEK_TICK = 83
GENERIC_TICKS = "100,101,104,106"  # 100 option volume, 101 option OI, 104/106 vol


def select_leap_expiries(
    expirations: list[str],
    *,
    today: date,
    min_dte: int,
    max_dte: int,
    max_per_symbol: int,
) -> list[str]:
    """Pick up to ``max_per_symbol`` expiries inside the LEAP DTE window.

    Expirations are IBKR ``YYYYMMDD`` strings. Returns them sorted ascending.
    """

    candidates: list[tuple[int, str]] = []
    for raw in expirations:
        try:
            exp = datetime.strptime(raw, "%Y%m%d").date()
        except (TypeError, ValueError):
            continue
        dte = (exp - today).days
        if min_dte <= dte <= max_dte:
            candidates.append((dte, raw))
    candidates.sort()
    return [raw for _dte, raw in candidates[:max_per_symbol]]


def select_strikes_around_spot(strikes: list[float], spot: float | None, count: int) -> list[float]:
    """Pick the ``count`` strikes nearest ``spot`` (or the middle band if unknown)."""

    valid = sorted(s for s in strikes if s and s > 0)
    if not valid:
        return []
    if spot is None or spot <= 0:
        mid = len(valid) // 2
        lo = max(0, mid - count // 2)
        return valid[lo : lo + count]
    nearest = sorted(valid, key=lambda s: abs(s - spot))[:count]
    return sorted(nearest)


def select_leap_call_strikes(
    strikes: list[float],
    spot: float | None,
    count: int,
    *,
    otm_lo: float = RADAR_CALL_STRIKE_OTM_LO,
    otm_hi: float = RADAR_CALL_STRIKE_OTM_HI,
) -> list[float]:
    """Pick OTM call strikes spanning the baseline and lottery 10x LEAP zones.

    The baseline family still gates delta around 0.20-0.45, but the forward-test
    deep-OTM sleeve needs 0.05-0.20 delta strikes that often live 1.8-3.0x spot.
    Preserve the configured strike budget for each zone so widening the frontier
    does not thin the baseline band. Falls back to nearest-spot when spot is
    unknown or the band is empty.
    """

    valid = sorted(s for s in strikes if s and s > 0)
    if not valid:
        return []
    if spot is None or spot <= 0:
        return select_strikes_around_spot(valid, spot, count)
    baseline_hi = min(RADAR_BASELINE_CALL_STRIKE_OTM_HI, otm_hi)
    baseline = [s for s in valid if otm_lo * spot <= s <= baseline_hi * spot]
    lottery = []
    if otm_hi > baseline_hi:
        lottery_lo = max(RADAR_LOTTERY_CALL_STRIKE_OTM_LO, baseline_hi)
        lottery = [s for s in valid if lottery_lo * spot < s <= otm_hi * spot]
    picked = sorted({
        *_sample_evenly(baseline, count),
        *_sample_evenly(lottery, count),
    })
    if not picked:
        return select_strikes_around_spot(valid, spot, count)
    return picked


def _sample_evenly(values: list[float], count: int) -> list[float]:
    if count <= 0 or not values:
        return []
    if len(values) <= count:
        return values
    step = (len(values) - 1) / (count - 1) if count > 1 else 0
    return sorted({values[round(i * step)] for i in range(count)})


def select_term_structure_expiries(
    expirations: list[str],
    *,
    today: date,
    buckets: tuple[tuple[int, int], ...] = ((30, 60), (90, 180), (365, 900)),
) -> list[str]:
    """Pick one expiry per DTE bucket to span the volatility term structure.

    The LEAP radar reads a single far-dated expiry; term-structure signals (an
    inverted/flattening front, event anticipation) need short, mid and long tenors.
    Picks the expiry nearest each bucket's lower edge, de-duplicated, sorted ascending.
    """

    dated: list[tuple[int, str]] = []
    for raw in expirations:
        try:
            exp = datetime.strptime(raw, "%Y%m%d").date()
        except (TypeError, ValueError):
            continue
        dated.append(((exp - today).days, raw))
    chosen: list[str] = []
    for lo, hi in buckets:
        in_bucket = [(dte, raw) for dte, raw in dated if lo <= dte <= hi]
        if not in_bucket:
            continue
        in_bucket.sort(key=lambda item: (abs(item[0] - lo), item[0]))
        chosen.append(in_bucket[0][1])
    seen: set[str] = set()
    ordered = [raw for raw in sorted(chosen, key=lambda r: datetime.strptime(r, "%Y%m%d")) if not (raw in seen or seen.add(raw))]
    return ordered


def select_leap_put_strikes(
    strikes: list[float],
    spot: float | None,
    count: int,
    *,
    otm_lo: float = 0.75,
    otm_hi: float = 0.95,
) -> list[float]:
    """Pick OTM put strikes in the 0.75-0.95x spot band for breakdown/hedge archetypes.

    Mirrors :func:`select_leap_call_strikes` on the downside. Falls back to nearest-spot
    when spot is unknown or the band is empty.
    """

    valid = sorted(s for s in strikes if s and s > 0)
    if not valid:
        return []
    if spot is None or spot <= 0:
        return select_strikes_around_spot(valid, spot, count)
    band = [s for s in valid if otm_lo * spot <= s <= otm_hi * spot]
    if not band:
        return select_strikes_around_spot(valid, spot, count)
    if len(band) <= count:
        return band
    step = (len(band) - 1) / (count - 1) if count > 1 else 0
    return sorted({band[round(i * step)] for i in range(count)})


def pick_chain_param_set(param_sets: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    """Choose the real option chain among ``reqSecDefOptParams`` results.

    Adjusted classes (e.g. ``2SPY``) carry only a couple of strikes; the real
    chain matches the symbol's trading class with the full strike list. Prefer a
    tradingClass equal to the symbol, otherwise the set with the most strikes.
    """

    if not param_sets:
        return None
    exact = [p for p in param_sets if str(p.get("tradingClass") or "").upper() == symbol.upper()]
    pool = exact or param_sets
    return max(pool, key=lambda p: len(p.get("strikes") or []))


def _first_positive(ticks: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = ticks.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _non_negative_int(ticks: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = ticks.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def parse_option_ticks(
    ticks: dict[str, Any],
    greeks: dict[str, Any],
    *,
    option_type: str,
) -> dict[str, Any]:
    """Map raw IBKR tick/greek callbacks into a normalized option-chain row.

    ``ticks`` keys are ``price_<tickType>`` / ``size_<tickType>``; ``greeks`` keys
    are ``opt_<tickType>`` mapping to the tickOptionComputation tuple
    ``[attrib, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice]``.
    Prefers live ticks, falls back to delayed.
    """

    live_bid, delayed_bid = _first_positive(ticks, ("price_1",)), _first_positive(ticks, ("price_66",))
    live_ask, delayed_ask = _first_positive(ticks, ("price_2",)), _first_positive(ticks, ("price_67",))
    bid = live_bid or delayed_bid
    ask = live_ask or delayed_ask
    last = _first_positive(ticks, ("price_4", "price_68"))
    close = _first_positive(ticks, ("price_9", "price_75"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else (last or close)

    model = greeks.get(f"opt_{LIVE_MODEL_GREEK_TICK}") or greeks.get(f"opt_{DELAYED_MODEL_GREEK_TICK}")
    iv = delta = gamma = vega = theta = None
    if isinstance(model, (list, tuple)) and len(model) >= 8:
        iv, delta, _opt_price, _pv_div, gamma, vega, theta = model[1], model[2], model[3], model[4], model[5], model[6], model[7]

    # Open interest tick depends on right: 27 call / 28 put.
    oi_key = "size_27" if option_type == "call" else "size_28"
    open_interest = _non_negative_int(ticks, (oi_key, "gen_101", "size_101"))
    volume = _non_negative_int(ticks, ("size_8", "size_74", "gen_100", "size_100"))
    live_bid_size, delayed_bid_size = _non_negative_int(ticks, ("size_0",)), _non_negative_int(ticks, ("size_69",))
    live_ask_size, delayed_ask_size = _non_negative_int(ticks, ("size_3",)), _non_negative_int(ticks, ("size_70",))
    bid_size = live_bid_size if live_bid_size is not None else delayed_bid_size
    ask_size = live_ask_size if live_ask_size is not None else delayed_ask_size
    live_tick = all(value is not None for value in (live_bid, live_ask, live_bid_size, live_ask_size))
    delayed_tick = all(value is not None for value in (delayed_bid, delayed_ask, delayed_bid_size, delayed_ask_size))

    return {
        "option_type": option_type,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "close": close,
        "iv": _clean(iv),
        "delta": _clean(delta),
        "gamma": _clean(gamma),
        "vega": _clean(vega),
        "theta": _clean(theta),
        "open_interest": open_interest,
        "volume": volume,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "market_data_status": "live" if live_tick else "delayed" if delayed_tick else "unknown",
    }


def _clean(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value == value:  # not NaN
        return float(value)
    return None


def chain_row(symbol: str, expiry: str, strike: float, parsed: dict[str, Any], *, delayed: bool) -> dict[str, Any]:
    """Assemble a store_options_chain-compatible row from parsed ticks.

    OI/volume go into the row so they are preserved in the chain's raw JSON,
    matching how the radar reads liquidity.
    """

    return {
        "expiry": _iso_expiry(expiry),
        "strike": strike,
        "type": parsed["option_type"],
        "bid": parsed["bid"],
        "ask": parsed["ask"],
        "mid": parsed["mid"],
        "iv": parsed["iv"],
        "delta": parsed["delta"],
        "gamma": parsed["gamma"],
        "vega": parsed["vega"],
        "theta": parsed["theta"],
        "open_interest": parsed["open_interest"],
        "volume": parsed["volume"],
        "bid_size": parsed["bid_size"],
        "ask_size": parsed["ask_size"],
        "contract_symbol": f"{symbol}{expiry}{parsed['option_type'][0].upper()}{strike}",
        "market_data": parsed["market_data_status"] if parsed["market_data_status"] != "unknown" else ("delayed" if delayed else "live"),
        "market_data_status": parsed["market_data_status"],
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _iso_expiry(expiry: str) -> str:
    try:
        return datetime.strptime(expiry, "%Y%m%d").date().isoformat()
    except (TypeError, ValueError):
        return expiry


# --------------------------------------------------------------------------
# Live IB API orchestration
# --------------------------------------------------------------------------
# IBKR allows ~100 simultaneous market-data lines; stay well under and pace
# between batches to respect the message-rate limit.
DEFAULT_BATCH_SIZE = 25
# Model greeks (tickOptionComputation) are server-computed and arrive later than
# price/size ticks, so give each batch enough time for delta/IV to land.
DEFAULT_LINES_SETTLE_SECONDS = 10.0
MARKET_DATA_DELAYED = 3
MARKET_DATA_LIVE = 1


def collect_ibkr_option_chains(
    config: Any,
    symbols: list[str],
    *,
    min_dte: int = 365,
    max_dte: int = 900,
    max_expiries: int = 2,
    strikes_around_spot: int = 12,
    market_data_type: int = MARKET_DATA_DELAYED,
    batch_size: int = DEFAULT_BATCH_SIZE,
    connect_timeout: float = 6.0,
    collect_puts: bool = False,
    include_term_structure: bool = False,
) -> dict[str, Any]:
    """Collect LEAP call-option chains (price+greeks+OI+volume) from IBKR.

    Read-only: requests contract details and market-data snapshots only; never
    places orders. Returns ``{"rows": {symbol: [chain_row,...]}, "errors": [...],
    "observed_at": iso, "market_data": "delayed"|"live"}``.
    """

    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.wrapper import EWrapper

    class _App(EWrapper, EClient):
        def __init__(self) -> None:
            EClient.__init__(self, self)
            self.details: dict[int, list] = {}
            self.params: list[dict] = []
            self.ticks: dict[int, dict] = {}
            self.greeks: dict[int, dict] = {}
            self.errors: list[tuple[int, int, str]] = []
            self._events: dict[int, threading.Event] = {}

        def error(self, *a) -> None:  # noqa: N802 - signature varies by ibapi version
            rid, code, msg = (a + (-1, -1, ""))[:3]
            try:
                self.errors.append((int(rid), int(code), str(msg)))
            except (TypeError, ValueError):
                self.errors.append((-1, -1, str(a)))

        def contractDetails(self, reqId, contractDetails) -> None:  # noqa: N802
            self.details.setdefault(reqId, []).append(contractDetails)

        def contractDetailsEnd(self, reqId) -> None:  # noqa: N802
            self._events.setdefault(reqId, threading.Event()).set()

        def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId, tradingClass, multiplier, expirations, strikes) -> None:  # noqa: N802
            self.params.append({"exchange": exchange, "tradingClass": tradingClass, "expirations": sorted(expirations), "strikes": sorted(strikes)})

        def securityDefinitionOptionParameterEnd(self, reqId) -> None:  # noqa: N802
            self._events.setdefault(reqId, threading.Event()).set()

        def tickPrice(self, reqId, tickType, price, attrib) -> None:  # noqa: N802
            self.ticks.setdefault(reqId, {})[f"price_{tickType}"] = price

        def tickSize(self, reqId, tickType, size) -> None:  # noqa: N802
            self.ticks.setdefault(reqId, {})[f"size_{tickType}"] = size

        def tickGeneric(self, reqId, tickType, value) -> None:  # noqa: N802
            self.ticks.setdefault(reqId, {})[f"gen_{tickType}"] = value

        def tickOptionComputation(self, reqId, tickType, *vals) -> None:  # noqa: N802
            self.greeks.setdefault(reqId, {})[f"opt_{tickType}"] = list(vals)

        def wait(self, reqId, timeout) -> bool:
            return self._events.setdefault(reqId, threading.Event()).wait(timeout)

    app = _App()
    host = str(getattr(config, "host", "127.0.0.1"))
    port = int(getattr(config, "port", 4002))
    # Vary the client id per run so a brand-new connection is never associated with
    # a not-yet-released prior session of the same id (which the Gateway reports as
    # error 10197 "competing live session" on the shared market-data allowance).
    client_id = int(getattr(config, "client_id", 0)) + 30 + int(time.time()) % 60
    observed_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {"rows": {}, "errors": [], "observed_at": observed_at, "market_data": "delayed" if market_data_type == MARKET_DATA_DELAYED else "live"}

    app.connect(host, port, client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    time.sleep(2.5)
    if not app.isConnected():
        result["errors"].append("ibkr_connect_failed")
        return result
    try:
        app.reqMarketDataType(market_data_type)
        rid = 100
        for symbol in symbols:
            try:
                rid = _collect_symbol(app, Contract, symbol, observed_at, rid, min_dte, max_dte, max_expiries, strikes_around_spot, batch_size, result, collect_puts=collect_puts, include_term_structure=include_term_structure)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not abort the run
                result["errors"].append(f"{symbol}:{exc}")
    finally:
        # Guaranteed teardown: disconnect releases all market-data lines for this
        # session server-side; the short settle lets the Gateway free them before
        # any subsequent run connects, preventing a self-inflicted 10197.
        try:
            app.disconnect()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
    if any(c == 10197 for _r, c, _m in app.errors):
        result["errors"].append("ibkr_10197_competing_session: another IBKR session (TWS/web/mobile/other client) holds the market-data allowance")
    result["errors"].extend(f"{c}:{m}" for _r, c, m in app.errors if c not in {2104, 2106, 2107, 2108, 2158, 2119, 10167, 10089})
    return result


def _collect_symbol(app, Contract, symbol, observed_at, rid, min_dte, max_dte, max_expiries, strikes_around_spot, batch_size, result, *, collect_puts: bool = False, include_term_structure: bool = False) -> int:
    today = datetime.now().date()
    # 1. underlying conId + spot
    stk = Contract()
    stk.symbol, stk.secType, stk.exchange, stk.currency = symbol, "STK", "SMART", "USD"
    app.reqContractDetails(rid, stk)
    app.wait(rid, 8)
    details = app.details.get(rid) or []
    rid += 1
    if not details:
        result["errors"].append(f"{symbol}:no_stock_contract")
        return rid
    conid = details[0].contract.conId
    spot_rid = rid
    app.reqMktData(spot_rid, stk, "", False, False, [])
    rid += 1
    time.sleep(2.5)
    spot = _first_positive(app.ticks.get(spot_rid, {}), ("price_4", "price_68", "price_75", "price_9", "price_66"))
    # Keep the underlying streaming: tickOptionComputation needs a live undPrice
    # to produce model greeks. Cancelled at the end of the symbol's collection.

    # 2. option chain definitions
    param_rid = rid
    app.reqSecDefOptParams(param_rid, symbol, "", "STK", conid)
    app.wait(param_rid, 8)
    rid += 1
    chain = pick_chain_param_set(app.params, symbol)
    app.params.clear()
    if not chain:
        result["errors"].append(f"{symbol}:no_option_params")
        return rid
    expiries = select_leap_expiries(chain["expirations"], today=today, min_dte=min_dte, max_dte=max_dte, max_per_symbol=max_expiries)
    if include_term_structure:
        # Add short/mid tenors so the vol surface (term slope, skew change) is observable.
        term = select_term_structure_expiries(chain["expirations"], today=today)
        expiries = sorted({*expiries, *term})
    if not expiries:
        result["errors"].append(f"{symbol}:no_leap_expiries")
        return rid

    # 3. For each expiry and right, qualify the actually-listed strikes (the param-set
    # strike union is a superset; specific expiries list far fewer), then request the
    # near-spot ones. Avoids error-200 spam and gives real coverage. Calls scan the OTM
    # upside band (10x LEAP delta ~0.20-0.45); puts the 0.75-0.95x downside band.
    rights = ["C", "P"] if collect_puts else ["C"]
    plan: list[tuple[int, str, float, str]] = []  # (reqId, expiry, strike, right)
    contracts: dict[int, Any] = {}
    for expiry in expiries:
        for right in rights:
            enum = Contract()
            enum.symbol, enum.secType, enum.exchange, enum.currency = symbol, "OPT", "SMART", "USD"
            enum.lastTradeDateOrContractMonth = expiry
            enum.right = right
            enum.tradingClass = chain["tradingClass"]
            enum_rid = rid
            rid += 1
            app.reqContractDetails(enum_rid, enum)
            app.wait(enum_rid, 10)
            valid_strikes = sorted({d.contract.strike for d in (app.details.get(enum_rid) or []) if getattr(d.contract, "strike", 0)})
            if right == "P":
                chosen = select_leap_put_strikes(valid_strikes, spot, strikes_around_spot)
            else:
                chosen = select_leap_call_strikes(valid_strikes, spot, strikes_around_spot)
            for strike in chosen:
                opt = Contract()
                opt.symbol, opt.secType, opt.exchange, opt.currency = symbol, "OPT", "SMART", "USD"
                opt.lastTradeDateOrContractMonth = expiry
                opt.strike = float(strike)
                opt.right = right
                opt.multiplier = "100"
                opt.tradingClass = chain["tradingClass"]
                plan.append((rid, expiry, strike, right))
                contracts[rid] = opt
                rid += 1
    if not plan:
        result["errors"].append(f"{symbol}:no_valid_strikes")
        return rid

    rows: list[dict[str, Any]] = []
    for start in range(0, len(plan), batch_size):
        batch = plan[start : start + batch_size]
        for req_id, _expiry, _strike, _right in batch:
            app.reqMktData(req_id, contracts[req_id], GENERIC_TICKS, False, False, [])
        time.sleep(DEFAULT_LINES_SETTLE_SECONDS)
        for req_id, expiry, strike, right in batch:
            app.cancelMktData(req_id)
            option_type = "put" if right == "P" else "call"
            parsed = parse_option_ticks(app.ticks.get(req_id, {}), app.greeks.get(req_id, {}), option_type=option_type)
            if parsed["delta"] is None and parsed["mid"] is None and parsed["open_interest"] is None:
                continue  # no data arrived for this contract
            rows.append(chain_row(symbol, expiry, strike, parsed, delayed=True))
    app.cancelMktData(spot_rid)  # underlying kept live through the option batches for greeks
    if rows:
        result["rows"][symbol] = rows
    return rid


def refresh_ibkr_options(con: Any, config: Any, symbols: list[str], **kwargs: Any) -> dict[str, Any]:
    """Collect IBKR option chains and persist them with source='ibkr'."""

    from investment_panel.core.free_sources import store_options_chain

    ibkr_cfg = config.data_sources.brokers.ibkr
    collected = collect_ibkr_option_chains(ibkr_cfg, symbols, **kwargs)
    observed_at = collected["observed_at"]
    stored = 0
    for symbol, rows in collected["rows"].items():
        stored += store_options_chain(con, symbol, observed_at, rows, source="ibkr")
    return {
        "provider": "ibkr",
        "market_data": collected["market_data"],
        "symbols_with_chains": len(collected["rows"]),
        "chain_rows": stored,
        "observed_at": observed_at,
        "errors": collected["errors"][:25],
    }
