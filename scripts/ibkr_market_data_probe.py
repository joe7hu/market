"""Read-only IBKR market-data entitlement probe.

Diagnostic only: connects to a running IB Gateway / TWS paper session, qualifies
a liquid stock and one near-ATM option, and reports whether live (or delayed)
stock and option market data — including greeks — are available. It NEVER places
orders or changes broker state.

Usage:
    uv run --with ibapi python scripts/ibkr_market_data_probe.py
    uv run --with ibapi python scripts/ibkr_market_data_probe.py --port 7497 --symbol SPY

Ports: IB Gateway paper 4002, TWS paper 7497 (live: 4001 / 7496).

Output is the status object documented in the GBrain `ibkr-market-data-probe`
skill: transport, api_login, account, stock/option contract + data, delayed
fallback, and a next_action recommendation.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from datetime import datetime, timezone

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
except ImportError:  # pragma: no cover - dependency guard
    raise SystemExit("ibapi is not installed. Run with: uv run --with ibapi python scripts/ibkr_market_data_probe.py")


ENTITLEMENT_CODES = {10089, 10090, 354, 10167, 10168}
DELAYED_OK_CODES = {10167, 10168}
INFO_CODES = {2104, 2106, 2107, 2108, 2158, 2119, 2100}


class ProbeApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self.accounts: list[str] = []
        self.contract_details: dict[int, list] = {}
        self.opt_params: list[dict] = []
        self.ticks: dict[int, dict] = {}
        self.greeks: dict[int, dict] = {}
        self.errors: list[tuple[int, int, str]] = []
        self._done: dict[int, threading.Event] = {}

    # --- lifecycle ---
    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._ready = True

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
        self.accounts = [a for a in accountsList.split(",") if a]

    def error(self, *args) -> None:  # noqa: N802 - signature varies across ibapi versions
        # Common shapes: (reqId, code, msg) or (reqId, code, msg, advancedJson)
        reqId = args[0] if len(args) > 0 else -1
        code = args[1] if len(args) > 1 else -1
        msg = args[2] if len(args) > 2 else ""
        try:
            self.errors.append((int(reqId), int(code), str(msg)))
        except (TypeError, ValueError):
            self.errors.append((-1, -1, str(args)))

    # --- contract details ---
    def contractDetails(self, reqId: int, contractDetails) -> None:  # noqa: N802
        self.contract_details.setdefault(reqId, []).append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._signal(reqId)

    # --- option parameters ---
    def securityDefinitionOptionParameter(  # noqa: N802
        self, reqId, exchange, underlyingConId, tradingClass, multiplier, expirations, strikes
    ) -> None:
        self.opt_params.append(
            {
                "exchange": exchange,
                "tradingClass": tradingClass,
                "expirations": sorted(expirations),
                "strikes": sorted(strikes),
            }
        )

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:  # noqa: N802
        self._signal(reqId)

    # --- market data ---
    def tickPrice(self, reqId: int, tickType: int, price: float, attrib) -> None:  # noqa: N802
        self.ticks.setdefault(reqId, {})[f"price_{tickType}"] = price

    def tickSize(self, reqId: int, tickType: int, size) -> None:  # noqa: N802
        self.ticks.setdefault(reqId, {})[f"size_{tickType}"] = size

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:  # noqa: N802
        self.ticks.setdefault(reqId, {})[f"gen_{tickType}"] = value

    def tickOptionComputation(self, reqId: int, tickType: int, *args) -> None:  # noqa: N802
        # args: (tickAttrib?, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice)
        vals = list(args)
        self.greeks.setdefault(reqId, {})[f"opt_{tickType}"] = vals

    # --- helpers ---
    def _signal(self, reqId: int) -> None:
        self._done.setdefault(reqId, threading.Event()).set()

    def wait(self, reqId: int, timeout: float) -> bool:
        ev = self._done.setdefault(reqId, threading.Event())
        return ev.wait(timeout)


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def classify_data(ticks: dict, errors: list, req_ids: set[int]) -> str:
    relevant = [e for e in errors if e[0] in req_ids]
    got_quote = any(any(k.startswith("price_") for k in ticks.get(r, {})) for r in req_ids)
    if got_quote:
        return "ok"
    if any(c in DELAYED_OK_CODES for _, c, _ in relevant):
        return "entitlement_missing_delayed_available"
    if any(c in ENTITLEMENT_CODES for _, c, _ in relevant):
        return "entitlement_missing"
    return "failed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="default: first open of 4002,7497,4001,7496")
    parser.add_argument("--client-id", type=int, default=88)
    parser.add_argument("--symbol", default="SPY")
    args = parser.parse_args()

    status = {
        "transport": "gateway_offline",
        "api_login": "failed",
        "account": "missing",
        "stock_contract": "failed",
        "live_stock_data": "failed",
        "delayed_fallback": "not_needed",
        "option_chain_defs": "failed",
        "option_contract": "failed",
        "live_option_data": "failed",
        "delayed_option_data": "not_tested",
        "option_open_interest": "missing",
        "option_volume": "missing",
        "option_greeks": "missing",
        "next_action": "",
    }

    candidate_ports = [args.port] if args.port else [4002, 7497, 4001, 7496]
    port = next((p for p in candidate_ports if port_open(args.host, p)), None)
    if port is None:
        status["next_action"] = "Start IB Gateway/TWS paper, enable API (Settings > API > Enable Socket Clients), trust 127.0.0.1, use port 4002 (paper Gateway)."
        _report(status, [])
        return
    status["transport"] = "ok"

    app = ProbeApp()
    app.connect(args.host, port, args.client_id)
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    time.sleep(2.5)

    if not app.isConnected():
        status["next_action"] = "Socket open but API login failed: check paper login, another active session, and 'Enable ActiveX and Socket Clients'."
        _report(status, app.errors)
        app.disconnect()
        return
    status["api_login"] = "ok"
    if app.accounts:
        status["account"] = app.accounts[0]

    # Qualify stock
    stk = Contract()
    stk.symbol = args.symbol
    stk.secType = "STK"
    stk.exchange = "SMART"
    stk.currency = "USD"
    app.reqContractDetails(1, stk)
    app.wait(1, 8)
    stk_details = app.contract_details.get(1) or []
    if not stk_details:
        status["next_action"] = "Stock contract qualification failed; fix the contract request before blaming subscriptions."
        _report(status, app.errors)
        app.disconnect()
        return
    status["stock_contract"] = "ok"
    stk_conid = stk_details[0].contract.conId

    def underlying_price() -> float | None:
        t = app.ticks.get(11, {})
        for key in ("price_4", "price_68", "price_75", "price_9", "price_66"):  # last/delayed-last/delayed-close/close/delayed-bid
            v = t.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        bid, ask = t.get("price_1") or t.get("price_66"), t.get("price_2") or t.get("price_67")
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None

    # Live first; on entitlement miss, switch to delayed for the rest of the probe.
    app.reqMarketDataType(1)
    app.reqMktData(11, stk, "", False, False, [])
    time.sleep(4)
    status["live_stock_data"] = classify_data(app.ticks, app.errors, {11})
    using_delayed = False
    if status["live_stock_data"].startswith("entitlement_missing") or status["live_stock_data"] == "failed":
        app.cancelMktData(11)
        app.reqMarketDataType(3)  # delayed
        app.reqMktData(11, stk, "", False, False, [])
        time.sleep(4)
        status["delayed_fallback"] = classify_data(app.ticks, app.errors, {11})
        using_delayed = status["delayed_fallback"].startswith("ok") or status["delayed_fallback"] == "entitlement_missing_delayed_available"

    spot = underlying_price()

    # Option definitions
    app.reqSecDefOptParams(2, args.symbol, "", "STK", stk_conid)
    app.wait(2, 8)
    if app.opt_params:
        status["option_chain_defs"] = "ok"
        # reqSecDefOptParams returns one set per exchange/tradingClass. Adjusted
        # classes (e.g. "2SPY") carry only a couple of strikes; the real chain is
        # the tradingClass matching the symbol with the full strike list. Pick the
        # set with the most strikes.
        smart = max(app.opt_params, key=lambda p: len(p["strikes"]))
        expiries = smart["expirations"]
        # Prefer a monthly expiry ~20-90 days out (better liquidity, real strikes).
        def days_out(e: str) -> int:
            try:
                return (datetime.strptime(e, "%Y%m%d").date() - datetime.now().date()).days
            except ValueError:
                return 9999
        monthlies = [e for e in expiries if 20 <= days_out(e) <= 120]
        expiry = monthlies[0] if monthlies else (expiries[len(expiries) // 2] if expiries else None)
        strikes = [s for s in smart["strikes"] if s > 0]
        if expiry and strikes:
            # reqSecDefOptParams already gave the full strike list; pick the one
            # nearest spot directly (a true ATM contract, the most liquid) and
            # qualify it. No enumeration needed.
            strike = min(strikes, key=lambda s: abs(s - spot)) if spot else strikes[len(strikes) // 2]
            opt = Contract()
            opt.symbol = args.symbol
            opt.secType = "OPT"
            opt.exchange = "SMART"
            opt.currency = "USD"
            opt.lastTradeDateOrContractMonth = expiry
            opt.strike = float(strike)
            opt.right = "C"
            opt.multiplier = "100"
            opt.tradingClass = smart["tradingClass"]
            app.reqContractDetails(3, opt)
            app.wait(3, 8)
            if app.contract_details.get(3):
                status["option_contract"] = "ok"
                status["chosen_option"] = f"{args.symbol} {expiry} C{strike} (spot~{spot})"
                # Streaming (snapshot=False) — IBKR rejects snapshot with generic
                # ticks. 100 option volume, 101 option open interest, 104/106 vol.
                if using_delayed:
                    app.reqMarketDataType(3)
                app.reqMktData(12, opt, "100,101,104,106", False, False, [])
                time.sleep(8)
                app.cancelMktData(12)
                opt_status = classify_data(app.ticks, app.errors, {12})
                status["live_option_data" if not using_delayed else "delayed_option_data"] = opt_status
                otk = app.ticks.get(12, {})
                status["option_open_interest"] = "ok" if any(k in otk for k in ("size_27", "size_28", "gen_101", "size_101")) else "missing"
                status["option_volume"] = "ok" if any(k in otk for k in ("size_8", "gen_100", "size_100")) else "missing"
                status["option_greeks"] = "ok" if app.greeks.get(12) else "missing"
                status["_raw_option_ticks"] = dict(otk)
                status["_raw_option_greeks"] = dict(app.greeks.get(12, {}))

    # next action
    opt_data = status.get("live_option_data") if status.get("live_option_data") != "failed" else status.get("delayed_option_data")
    have_liquidity = status.get("option_open_interest") == "ok" or status.get("option_volume") == "ok"
    if opt_data == "ok" and have_liquidity:
        mode = "live" if status.get("live_option_data") == "ok" else "delayed"
        status["next_action"] = f"{mode} option data with OI/volume available: build the IBKR option collector and persist OI/volume/greeks into options_liquidity (mark rows {mode})."
    elif opt_data == "ok":
        status["next_action"] = "Option quotes arrive but OI/volume ticks did not — inspect _raw_option_ticks; OI may need a different tick/genericTickList or live entitlement."
    elif status.get("option_contract") == "ok":
        status["next_action"] = "Option contract qualified but no market data — enable/share OPRA options entitlement (delayed or live) for the API, then re-probe."
    elif status["option_chain_defs"] == "ok":
        status["next_action"] = "Chain defs ok but contract qualification failed — fix the option contract request before blaming subscriptions."
    else:
        status["next_action"] = "Investigate option definitions before building the collector."

    _report(status, app.errors)
    app.disconnect()


def _report(status: dict, errors: list) -> None:
    print(f"# IBKR market-data probe @ {datetime.now(timezone.utc).isoformat()}")
    verdict = status.get("next_action", "")
    print(f"\nverdict: {verdict}\n")
    for k, v in status.items():
        if k == "next_action" or k.startswith("_"):
            continue
        print(f"{k}: {v}")
    if status.get("_raw_option_ticks") is not None:
        print(f"\nraw_option_ticks: {status.get('_raw_option_ticks')}")
        print(f"raw_option_greeks: {status.get('_raw_option_greeks')}")
    raw = [e for e in errors if e[1] not in INFO_CODES]
    if raw:
        print("\nraw_errors:")
        for reqId, code, msg in raw[:12]:
            print(f"- {code} (req {reqId}): {msg}")


if __name__ == "__main__":
    main()
