"""IBKR provider and snapshot collection/normalization."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import importlib.util
import threading
import time
from typing import Any, Protocol
from uuid import uuid4
from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core.instruments import infer_asset_class, normalize_symbol

from investment_panel.core.brokers.constants import IBKR_ACCOUNT_TAGS, IBKR_GENERIC_TICKS, IBKR_TICK_GENERIC_FIELDS, IBKR_TICK_PRICE_FIELDS, IBKR_TICK_SIZE_FIELDS
from investment_panel.core.brokers.types import BrokerSnapshot, ProviderStatus
from investment_panel.core.brokers.coerce import parse_dt, tcp_open



class IBKRProvider:
    """Read-only adapter for Interactive Brokers TWS API / IB Gateway."""

    name = "ibkr"

    def __init__(self, config: Any):
        self.config = config

    def collect(self, symbols: list[str]) -> BrokerSnapshot:
        checked_at = datetime.now(UTC)
        if not self.config.enabled:
            return BrokerSnapshot(ProviderStatus(self.name, "disabled", "IBKR source is disabled in config.", checked_at))
        if importlib.util.find_spec("ibapi") is None:
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "missing_dependency",
                    "Install ibapi and run TWS or IB Gateway to enable IBKR sync.",
                    checked_at,
                    capabilities=ibkr_capabilities(),
                )
            )
        started = time.perf_counter()
        if not tcp_open(self.config.host, self.config.port, timeout=1.0):
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "gateway_offline",
                    f"TWS API / IB Gateway is not reachable at {self.config.host}:{self.config.port}.",
                    checked_at,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    capabilities=ibkr_capabilities(),
                )
            )
        try:
            return collect_ibkr_snapshot(self.config, symbols, checked_at, started)
        except Exception as exc:  # pragma: no cover - provider boundary
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "session_failure",
                    f"IBKR read-only API session failed: {exc}",
                    checked_at,
                    account_id=self.config.account_id,
                    account_mode="paper" if self.config.paper_only else "unknown",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    capabilities=ibkr_capabilities(),
                )
            )




def collect_ibkr_snapshot(config: Any, symbols: list[str], checked_at: datetime, started: float) -> BrokerSnapshot:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.wrapper import EWrapper

    class ReadOnlyIBApp(EWrapper, EClient):
        def __init__(self) -> None:
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self.account_done = threading.Event()
            self.positions_done = threading.Event()
            self.open_orders_done = threading.Event()
            self.executions_done = threading.Event()
            self.managed_accounts: list[str] = []
            self.account_values: dict[str, dict[str, Any]] = {}
            self.positions: list[dict[str, Any]] = []
            self.orders: list[dict[str, Any]] = []
            self.fills: list[dict[str, Any]] = []
            self.quotes: dict[int, dict[str, Any]] = {}
            self.req_symbols: dict[int, str] = {}
            self.errors: list[dict[str, Any]] = []
            self.observed_accounts: set[str] = set()
            self.session_started_at: datetime | None = None

        def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB callback name
            self.session_started_at = datetime.now(UTC)
            self.ready.set()

        def managedAccounts(self, accountsList: str) -> None:  # noqa: N802 - IB callback name
            self.managed_accounts = [item for item in accountsList.split(",") if item]
            self.observed_accounts.update(self.managed_accounts)

        def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:  # noqa: N802
            self.record_account(account)
            if not ibkr_accept_account(config, account):
                return
            values = self.account_values.setdefault(account, {"account_id": account, "raw": {}})
            values["raw"][tag] = {"value": value, "currency": currency}
            values["currency"] = currency or values.get("currency") or "USD"
            key = {
                "TotalCashValue": "cash",
                "BuyingPower": "buying_power",
                "NetLiquidation": "net_liquidation",
                "MaintMarginReq": "margin_requirement",
                "ExcessLiquidity": "excess_liquidity",
                "UnrealizedPnL": "unrealized_pnl",
                "RealizedPnL": "realized_pnl",
            }.get(tag)
            if key:
                values[key] = ibkr_number(value)

        def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802
            self.account_done.set()

        def position(self, account: str, contract: Any, position: float, avgCost: float) -> None:
            self.record_account(account)
            if not ibkr_accept_account(config, account):
                return
            symbol = ibkr_position_symbol(contract)
            if not symbol:
                return
            self.positions.append(
                {
                    "account_id": account,
                    "symbol": symbol,
                    "asset_class": ibkr_asset_class(str(getattr(contract, "secType", "") or "")),
                    "quantity": ibkr_number(position),
                    "average_cost": ibkr_number(avgCost),
                    "raw": ibkr_contract_raw(contract),
                }
            )

        def positionMulti(self, reqId: int, account: str, modelCode: str, contract: Any, pos: float, avgCost: float) -> None:  # noqa: N802
            self.position(account, contract, pos, avgCost)

        def positionMultiEnd(self, reqId: int) -> None:  # noqa: N802
            self.positions_done.set()

        def positionEnd(self) -> None:  # noqa: N802
            self.positions_done.set()

        def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:  # noqa: N802
            self.record_account(getattr(order, "account", None))
            if not ibkr_accept_account(config, getattr(order, "account", None)):
                return
            symbol = ibkr_position_symbol(contract)
            if not symbol:
                return
            self.orders.append(
                {
                    "account_id": getattr(order, "account", None),
                    "order_id": str(orderId),
                    "symbol": symbol,
                    "side": getattr(order, "action", None),
                    "order_type": getattr(order, "orderType", None),
                    "quantity": ibkr_number(getattr(order, "totalQuantity", None)),
                    "limit_price": ibkr_number(getattr(order, "lmtPrice", None)),
                    "status": getattr(orderState, "status", None),
                    "submitted_at": None,
                    "updated_at": datetime.now(UTC),
                    "raw": {"contract": ibkr_contract_raw(contract), "order": ibkr_object_raw(order), "order_state": ibkr_object_raw(orderState)},
                }
            )

        def openOrderEnd(self) -> None:  # noqa: N802
            self.open_orders_done.set()

        def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:  # noqa: N802
            self.record_account(getattr(execution, "acctNumber", None))
            if not ibkr_accept_account(config, getattr(execution, "acctNumber", None)):
                return
            symbol = ibkr_position_symbol(contract)
            if not symbol:
                return
            self.fills.append(
                {
                    "account_id": getattr(execution, "acctNumber", None),
                    "fill_id": str(getattr(execution, "execId", "") or uuid4().hex),
                    "order_id": str(getattr(execution, "orderId", "") or ""),
                    "symbol": symbol,
                    "side": getattr(execution, "side", None),
                    "quantity": ibkr_number(getattr(execution, "shares", None)),
                    "price": ibkr_number(getattr(execution, "price", None)),
                    "filled_at": ibkr_execution_time(getattr(execution, "time", None)) or datetime.now(UTC),
                    "raw": {"contract": ibkr_contract_raw(contract), "execution": ibkr_object_raw(execution)},
                }
            )

        def execDetailsEnd(self, reqId: int) -> None:  # noqa: N802
            self.executions_done.set()

        def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
            quote = self.quotes.setdefault(reqId, {"symbol": self.req_symbols.get(reqId), "raw": {}})
            quote["market_data_type"] = marketDataType
            quote["data_status"] = ibkr_market_data_status(marketDataType)
            quote["raw"]["market_data_type"] = marketDataType

        def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:  # noqa: N802
            field = IBKR_TICK_PRICE_FIELDS.get(tickType)
            quote = self.quotes.setdefault(reqId, {"symbol": self.req_symbols.get(reqId), "raw": {}})
            quote["raw"][f"tick_price_{tickType}"] = ibkr_number(price)
            if field and price is not None and float(price) >= 0:
                quote[field] = ibkr_number(price)

        def tickSize(self, reqId: int, tickType: int, size: int) -> None:  # noqa: N802
            field = IBKR_TICK_SIZE_FIELDS.get(tickType)
            quote = self.quotes.setdefault(reqId, {"symbol": self.req_symbols.get(reqId), "raw": {}})
            quote["raw"][f"tick_size_{tickType}"] = ibkr_number(size)
            value = ibkr_number(size)
            if field and value is not None and value >= 0:
                quote[field] = value

        def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:  # noqa: N802
            field = IBKR_TICK_GENERIC_FIELDS.get(tickType)
            quote = self.quotes.setdefault(reqId, {"symbol": self.req_symbols.get(reqId), "raw": {}})
            quote["raw"][f"tick_generic_{tickType}"] = ibkr_number(value)
            if field:
                quote[field] = ibkr_number(value)

        def tickString(self, reqId: int, tickType: int, value: str) -> None:  # noqa: N802
            quote = self.quotes.setdefault(reqId, {"symbol": self.req_symbols.get(reqId), "raw": {}})
            quote["raw"][f"tick_string_{tickType}"] = value

        def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:
            self.errors.append({"req_id": reqId, "code": errorCode, "message": errorString, "advanced": advancedOrderRejectJson})

        def record_account(self, account_id: Any) -> None:
            if account_id:
                self.observed_accounts.add(str(account_id))

    app = ReadOnlyIBApp()
    app.connect(config.host, config.port, config.client_id)
    thread = threading.Thread(target=app.run, name="ibkr-api-reader", daemon=True)
    thread.start()
    try:
        if not app.ready.wait(6.0):
            return ibkr_session_failure(config, checked_at, started, "IBKR socket opened but API handshake did not complete.", app.errors)

        account_req_id = 9101
        app.reqAccountSummary(account_req_id, "All", IBKR_ACCOUNT_TAGS)
        app.account_done.wait(5.0)
        try:
            app.cancelAccountSummary(account_req_id)
        except Exception:
            pass
        account_mismatch = ibkr_paper_account_mismatch(config, app, checked_at, started)
        if account_mismatch:
            return account_mismatch

        positions_req_id = 9103
        if config.account_id:
            app.reqPositionsMulti(positions_req_id, config.account_id, "")
        else:
            app.reqPositions()
        app.positions_done.wait(5.0)
        try:
            if config.account_id:
                app.cancelPositionsMulti(positions_req_id)
            else:
                app.cancelPositions()
        except Exception:
            pass

        app.reqAllOpenOrders()
        app.open_orders_done.wait(2.0)

        exec_req_id = 9102
        exec_filter = ExecutionFilter()
        if config.account_id:
            exec_filter.acctCode = config.account_id
        app.reqExecutions(exec_req_id, exec_filter)
        app.executions_done.wait(3.0)
        account_mismatch = ibkr_paper_account_mismatch(config, app, checked_at, started)
        if account_mismatch:
            return account_mismatch

        quote_symbols = ibkr_quote_symbols(symbols, app.positions, int(getattr(config, "quote_limit", 50) or 50))

        def request_quote_batch(start_req_id: int, market_data_type_id: int, batch_symbols: list[str] | None = None) -> list[int]:
            app.reqMarketDataType(market_data_type_id)
            req_ids: list[int] = []
            for offset, symbol in enumerate(batch_symbols or quote_symbols, start=start_req_id):
                contract = Contract()
                contract.symbol = symbol
                contract.secType = "STK"
                contract.exchange = "SMART"
                contract.currency = "USD"
                app.req_symbols[offset] = symbol
                app.quotes[offset] = {"symbol": symbol, "observed_at": datetime.now(UTC), "raw": {}}
                app.reqMktData(offset, contract, IBKR_GENERIC_TICKS, False, False, [])
                req_ids.append(offset)
            if req_ids:
                time.sleep(min(8.0, 2.0 + len(req_ids) * 0.12))
            for req_id in req_ids:
                try:
                    app.cancelMktData(req_id)
                except Exception:
                    pass
            return req_ids

        market_data_mode = str(getattr(config, "market_data_type", "live_or_delayed"))
        request_quote_batch(9200, ibkr_market_data_type_id(market_data_mode))
        live_or_delayed = market_data_mode.lower() == "live_or_delayed"
        if live_or_delayed and ibkr_entitlement_errors(app.errors):
            missing_symbols = ibkr_missing_quote_symbols(quote_symbols, ibkr_market_snapshots(app.quotes, app.errors, checked_at))
            if missing_symbols:
                request_quote_batch(9300, ibkr_market_data_type_id("delayed"), missing_symbols)
    finally:
        try:
            app.disconnect()
        finally:
            thread.join(timeout=1.0)

    accounts = ibkr_accounts(app.account_values, config, checked_at)
    market_snapshots = ibkr_market_snapshots(app.quotes, app.errors, checked_at)
    positions = ibkr_positions(app.positions, market_snapshots, checked_at)
    last_data_at = max(
        [checked_at]
        + [item.get("updated_at") for item in accounts if item.get("updated_at")]
        + [item.get("updated_at") for item in positions if item.get("updated_at")]
        + [item.get("observed_at") for item in market_snapshots if item.get("observed_at")]
    )
    account_id = config.account_id or (accounts[0]["account_id"] if accounts else (app.managed_accounts[0] if app.managed_accounts else None))
    entitlement_errors = ibkr_entitlement_errors(app.errors)
    status, detail = ibkr_snapshot_status(accounts, positions, app.orders, app.fills, market_snapshots, entitlement_errors)
    return BrokerSnapshot(
        status=ProviderStatus(
            "ibkr",
            status,
            detail,
            checked_at=checked_at,
            account_id=account_id,
            account_mode="paper" if config.paper_only else "unknown",
            session_started_at=app.session_started_at,
            last_data_at=last_data_at if status == "ok" else None,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            capabilities=ibkr_capabilities(),
            raw={
                "host": config.host,
                "port": config.port,
                "client_id": config.client_id,
                "managed_accounts": app.managed_accounts,
                "requested_symbols": quote_symbols,
                "errors": app.errors[-25:],
            },
        ),
        accounts=accounts,
        positions=positions,
        orders=app.orders,
        fills=app.fills,
        market_snapshots=market_snapshots,
    )




def ibkr_session_failure(config: Any, checked_at: datetime, started: float, detail: str, errors: list[dict[str, Any]]) -> BrokerSnapshot:
    return BrokerSnapshot(
        ProviderStatus(
            "ibkr",
            "session_failure",
            detail,
            checked_at,
            account_id=config.account_id,
            account_mode="paper" if config.paper_only else "unknown",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            capabilities=ibkr_capabilities(),
            raw={"host": config.host, "port": config.port, "client_id": config.client_id, "errors": errors[-25:]},
        )
    )




def ibkr_account_mode_mismatch(config: Any, checked_at: datetime, started: float, account_ids: list[str], errors: list[dict[str, Any]]) -> BrokerSnapshot:
    return BrokerSnapshot(
        ProviderStatus(
            "ibkr",
            "account_mode_mismatch",
            "IBKR paper_only is enabled, but the connected Gateway exposed non-paper account id(s); refusing to persist broker data.",
            checked_at,
            account_id=",".join(account_ids) if account_ids else config.account_id,
            account_mode="live",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            capabilities=ibkr_capabilities(),
            raw={"host": config.host, "port": config.port, "client_id": config.client_id, "account_ids": account_ids, "errors": errors[-25:]},
        )
    )




def ibkr_number(value: Any) -> float | None:
    try:
        if value is None or value == "" or str(value).lower() in {"nan", "none", "unset"}:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 1.7976931348623157e308:
        return None
    return number




def ibkr_asset_class(sec_type: str) -> str:
    return {"STK": "equity", "OPT": "option", "FUT": "future", "CASH": "fx", "CRYPTO": "crypto", "BOND": "bond"}.get(sec_type.upper(), "unknown")




def ibkr_position_symbol(contract: Any) -> str:
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    underlying = normalize_symbol(str(getattr(contract, "symbol", "") or ""))
    if sec_type in {"", "STK"}:
        return underlying
    local_symbol = str(getattr(contract, "localSymbol", "") or "").strip().upper()
    if local_symbol:
        return "_".join(local_symbol.split())
    con_id = getattr(contract, "conId", None)
    if con_id:
        return f"{underlying}:{sec_type}:{con_id}"
    return f"{underlying}:{sec_type}" if underlying and sec_type else underlying




def ibkr_contract_raw(contract: Any) -> dict[str, Any]:
    return {
        "con_id": getattr(contract, "conId", None),
        "symbol": getattr(contract, "symbol", None),
        "sec_type": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "primary_exchange": getattr(contract, "primaryExchange", None),
        "currency": getattr(contract, "currency", None),
        "local_symbol": getattr(contract, "localSymbol", None),
    }




def ibkr_object_raw(obj: Any) -> dict[str, Any]:
    return {key: value for key, value in vars(obj).items() if not key.startswith("_") and isinstance(value, (str, int, float, bool, type(None)))}




def ibkr_execution_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(text[:17], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None




def ibkr_market_data_type_id(value: str) -> int:
    return {"live": 1, "frozen": 2, "delayed": 3, "delayed_frozen": 4, "live_or_delayed": 1}.get(value.lower(), 1)




def ibkr_market_data_status(value: int) -> str:
    return {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}.get(value, "unknown")




def ibkr_snapshot_status(
    accounts: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    market_snapshots: list[dict[str, Any]],
    entitlement_errors: list[dict[str, Any]],
) -> tuple[str, str]:
    if accounts or positions or orders or fills:
        detail = f"IBKR read-only sync ok: {len(accounts)} accounts, {len(positions)} positions, {len(market_snapshots)} quote snapshots."
        if entitlement_errors:
            detail += " Some quote requests reported entitlement limits or delayed-data fallback."
        return "ok", detail
    if market_snapshots:
        return "quote_only", f"IBKR quote sync ok: {len(market_snapshots)} quote snapshots, but no account, position, order, or fill data arrived."
    if entitlement_errors:
        return "quote_entitlement_failure", "IBKR session connected, but quote requests returned market-data entitlement errors."
    return "session_failure", "IBKR API session completed, but no account, position, or quote data arrived."




def ibkr_accept_account(config: Any, account_id: Any) -> bool:
    configured = str(getattr(config, "account_id", "") or "").strip()
    if not configured:
        return True
    return str(account_id or "").strip() == configured




def ibkr_paper_account_mismatch(config: Any, app: Any, checked_at: datetime, started: float) -> BrokerSnapshot | None:
    if not getattr(config, "paper_only", True):
        return None
    account_ids = sorted(
        {
            str(account_id)
            for account_id in list(getattr(app, "observed_accounts", set()) or set())
            + list(getattr(app, "managed_accounts", []) or [])
            + list(getattr(app, "account_values", {}).keys())
            if account_id
        }
    )
    live_accounts = [account_id for account_id in account_ids if not ibkr_paper_account_id(account_id)]
    if not live_accounts:
        return None
    return ibkr_account_mode_mismatch(config, checked_at, started, live_accounts, getattr(app, "errors", []))




def ibkr_paper_account_id(account_id: str) -> bool:
    return account_id.upper().startswith("DU")




def ibkr_quote_symbols(symbols: list[str], positions: list[dict[str, Any]], limit: int) -> list[str]:
    candidates = [str(row.get("symbol") or "") for row in positions] + symbols
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        symbol = normalize_symbol(candidate)
        if not symbol or symbol in seen or not ibkr_stock_quote_symbol(symbol):
            continue
        seen.add(symbol)
        normalized.append(symbol)
        if len(normalized) >= max(1, limit):
            break
    return normalized




def ibkr_stock_quote_symbol(symbol: str) -> bool:
    compact = symbol.replace(".", "").replace("-", "")
    return bool(compact.isalpha() and len(compact) <= 5)




def ibkr_accounts(values: dict[str, dict[str, Any]], config: Any, checked_at: datetime) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for account_id, row in values.items():
        unrealized = ibkr_number(row.get("unrealized_pnl")) or 0.0
        realized = ibkr_number(row.get("realized_pnl")) or 0.0
        accounts.append(
            {
                "account_id": account_id,
                "account_mode": "paper" if config.paper_only else "unknown",
                "currency": row.get("currency") or "USD",
                "cash": row.get("cash"),
                "buying_power": row.get("buying_power"),
                "net_liquidation": row.get("net_liquidation"),
                "margin_requirement": row.get("margin_requirement"),
                "excess_liquidity": row.get("excess_liquidity"),
                "day_pnl": None,
                "total_pnl": unrealized + realized if unrealized or realized else None,
                "updated_at": checked_at,
                "raw": row.get("raw") or {},
            }
        )
    return accounts




def ibkr_positions(positions: list[dict[str, Any]], market_snapshots: list[dict[str, Any]], checked_at: datetime) -> list[dict[str, Any]]:
    quote_by_symbol = {row["symbol"]: row for row in market_snapshots if row.get("symbol")}
    rows: list[dict[str, Any]] = []
    for position in positions:
        symbol = position.get("symbol")
        quote = quote_by_symbol.get(symbol, {})
        price = quote.get("last") or quote.get("close")
        quantity = ibkr_number(position.get("quantity"))
        market_value = quantity * float(price) if quantity is not None and price is not None else None
        rows.append(
            {
                **position,
                "market_price": price,
                "market_value": market_value,
                "updated_at": quote.get("observed_at") or checked_at,
            }
        )
    return rows




def ibkr_market_snapshots(quotes: dict[int, dict[str, Any]], errors: list[dict[str, Any]], checked_at: datetime) -> list[dict[str, Any]]:
    errors_by_req: dict[int, list[dict[str, Any]]] = {}
    for error in errors:
        req_id = int(error.get("req_id") or -1)
        errors_by_req.setdefault(req_id, []).append(error)
    rows: list[dict[str, Any]] = []
    for req_id, quote in quotes.items():
        symbol = normalize_symbol(str(quote.get("symbol") or ""))
        has_price = any(quote.get(field) is not None for field in ["bid", "ask", "last", "close", "mark_price"])
        has_size = quote.get("volume") is not None
        if not symbol or not (has_price or has_size):
            continue
        market_data_status = quote.get("data_status") or ibkr_market_data_status(int(quote.get("market_data_type") or 0))
        entitlement_status = "limited" if ibkr_entitlement_errors(errors_by_req.get(req_id, [])) else "ok"
        rows.append(
            {
                "symbol": symbol,
                "observed_at": quote.get("observed_at") or checked_at,
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "last": quote.get("last") or quote.get("mark_price"),
                "close": quote.get("close"),
                "volume": quote.get("volume"),
                "entitlement_status": entitlement_status,
                "data_status": market_data_status,
                "raw": {**quote.get("raw", {}), "market_data_status": market_data_status},
            }
        )
    return rows




def ibkr_missing_quote_symbols(symbols: list[str], market_snapshots: list[dict[str, Any]]) -> list[str]:
    seen = {normalize_symbol(str(row.get("symbol") or "")) for row in market_snapshots if row.get("symbol")}
    return [symbol for symbol in symbols if normalize_symbol(symbol) not in seen]




def ibkr_entitlement_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entitlement_codes = {354, 10089, 10090, 10167, 10168}
    return [error for error in errors if int(error.get("code") or 0) in entitlement_codes]




def ibkr_health(con: Any) -> dict[str, Any]:
    rows = query_rows(con, "SELECT * FROM broker_provider_status WHERE provider = 'ibkr' LIMIT 1")
    if not rows:
        return {"provider": "ibkr", "status": "missing", "usable": False, "detail": "IBKR has not synced yet."}
    row = rows[0]
    status = str(row.get("status") or "missing")
    last_data = parse_dt(row.get("last_data_at") or row.get("checked_at"))
    stale = bool(last_data and datetime.now(UTC) - last_data > timedelta(minutes=15))
    if stale and status == "ok":
        status = "stale_data"
    return {
        "provider": "ibkr",
        "status": status,
        "usable": status == "ok" and not stale,
        "detail": row.get("detail"),
        "account_id": row.get("account_id"),
        "account_mode": row.get("account_mode"),
        "checked_at": row.get("checked_at"),
        "last_data_at": row.get("last_data_at"),
    }




def ibkr_capabilities() -> list[str]:
    return ["positions", "cash", "buying_power", "margin_risk", "pnl", "orders", "fills", "account_mode", "market_snapshots", "options", "scanner"]
