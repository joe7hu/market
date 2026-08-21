"""PostgreSQL authority for append-only portfolio transactions and projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from investment_panel.core.config import AppConfig
from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.user_state import DEFAULT_OWNED_THESIS
from investment_panel.database.authority import runtime_for_config
from psycopg.types.json import Jsonb


POSITION_TYPES = {"opening_balance", "buy", "sell", "transfer_in", "transfer_out"}
CASH_TYPES = {"dividend", "fee"}
TRANSACTION_TYPES = POSITION_TYPES | CASH_TYPES | {"split"}
MARKET_TIMEZONE = ZoneInfo("America/New_York")


def preview_portfolio_transaction(config: AppConfig, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_transaction(fields)
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        _reject_backdated_transaction(connection, normalized)
        position = _position_for_symbol(connection, normalized.get("symbol"))
    preview = _transaction_preview(normalized, position)
    preview["position_version"] = _position_version(position)
    return preview


def record_portfolio_transaction(config: AppConfig, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_transaction(fields)
    runtime = runtime_for_config(config)
    with runtime.transaction() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [normalized["idempotency_key"]],
        )
        existing = connection.execute(
            "SELECT transaction.id FROM app.portfolio_transaction transaction WHERE idempotency_key = %s",
            [normalized["idempotency_key"]],
        ).fetchone()
        if existing:
            existing_row = _transaction_row(connection, existing["id"])
            if not _transaction_matches(existing_row, normalized):
                raise ValueError("idempotency key is already used by a different transaction")
            return existing_row

        instrument_id = None
        symbol = normalized.get("symbol")
        if symbol:
            instrument_id = reconcile_instrument(
                connection,
                symbol,
                name=symbol,
                category="watchlist",
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"portfolio-instrument:{instrument_id}"],
            )
            connection.execute("SELECT id FROM catalog.instrument WHERE id = %s FOR UPDATE", [instrument_id])
        _reject_backdated_transaction(connection, normalized, instrument_id=instrument_id)
        position = _position_for_instrument(connection, instrument_id, lock=True) if instrument_id else None
        expected_position_version = normalized.get("expected_position_version")
        if (
            expected_position_version is not None
            and expected_position_version != _position_version(position)
        ):
            raise ValueError("portfolio position changed since preview; preview the trade again")
        preview = _transaction_preview(normalized, position)
        row = connection.execute(
            """
            INSERT INTO app.portfolio_transaction
                (instrument_id, transaction_type, quantity, price, amount, fees,
                 realized_pnl, currency, account, executed_at, notes,
                 idempotency_key, reverses_transaction_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                instrument_id,
                normalized["transaction_type"],
                normalized.get("quantity"),
                normalized.get("price"),
                preview.get("amount"),
                normalized["fees"],
                preview.get("realized_pnl", 0),
                normalized["currency"],
                normalized["account"],
                normalized["executed_at"],
                normalized["notes"],
                normalized["idempotency_key"],
                normalized.get("reverses_transaction_id"),
            ],
        ).fetchone()
        if instrument_id and normalized["transaction_type"] in POSITION_TYPES | {"split"}:
            _apply_position_preview(connection, instrument_id, normalized, preview)
            if float(preview["new_quantity"]) > 0:
                _mark_owned_thesis(connection, instrument_id)
            else:
                _mark_exited_thesis(connection, instrument_id)
        return _transaction_row(connection, row["id"])


def reverse_portfolio_transaction(
    config: AppConfig,
    transaction_id: str,
    *,
    idempotency_key: str,
    notes: str = "",
) -> dict[str, Any]:
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")
    runtime = runtime_for_config(config)
    with runtime.transaction() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
        existing = connection.execute(
            "SELECT id, reverses_transaction_id FROM app.portfolio_transaction WHERE idempotency_key = %s",
            [key],
        ).fetchone()
        if existing:
            if str(existing.get("reverses_transaction_id") or "") != str(transaction_id):
                raise ValueError("idempotency key is already used by a different transaction")
            return _transaction_row(connection, existing["id"])
        target_identity = connection.execute(
            "SELECT instrument_id FROM app.portfolio_transaction WHERE id::text = %s",
            [transaction_id],
        ).fetchone()
        if not target_identity:
            raise ValueError("portfolio transaction was not found")
        instrument_id = target_identity.get("instrument_id")
        if instrument_id is not None:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"portfolio-instrument:{instrument_id}"],
            )
        target = connection.execute(
            """
            SELECT transaction.*, instrument.symbol
            FROM app.portfolio_transaction transaction
            LEFT JOIN catalog.instrument instrument ON instrument.id = transaction.instrument_id
            WHERE transaction.id::text = %s
            FOR UPDATE OF transaction
            """,
            [transaction_id],
        ).fetchone()
        if target.get("reverses_transaction_id") is not None:
            raise ValueError("a reversal transaction cannot itself be reversed")
        target_id = target["id"]
        prior_reversal = connection.execute(
            "SELECT id FROM app.portfolio_transaction WHERE reverses_transaction_id = %s",
            [target_id],
        ).fetchone()
        if prior_reversal:
            raise ValueError("portfolio transaction is already reversed")
        if instrument_id is not None:
            connection.execute("SELECT id FROM catalog.instrument WHERE id = %s FOR UPDATE", [instrument_id])
        reversal = connection.execute(
            """
            INSERT INTO app.portfolio_transaction
                (instrument_id, transaction_type, quantity, price, amount, fees,
                 realized_pnl, currency, account, executed_at, notes,
                 idempotency_key, reverses_transaction_id)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, now(), %s, %s, %s)
            RETURNING id
            """,
            [
                instrument_id,
                target["transaction_type"],
                target.get("quantity"),
                target.get("price"),
                target.get("amount"),
                target.get("fees") or 0,
                target.get("currency") or "USD",
                target.get("account") or "manual",
                str(notes or "").strip() or f"Reversal of {transaction_id}",
                key,
                target_id,
            ],
        ).fetchone()
        if instrument_id is not None and target["transaction_type"] in POSITION_TYPES | {"split"}:
            _rebuild_position_projection(connection, int(instrument_id))
        return _transaction_row(connection, reversal["id"])


def portfolio_transaction_rows(
    config: AppConfig,
    limit: int | None = 100,
    *,
    connection: Any | None = None,
) -> list[dict[str, Any]]:
    if connection is None:
        runtime = runtime_for_config(config)
        with runtime.read() as owned_connection:
            return portfolio_transaction_rows(config, limit, connection=owned_connection)
    limit_clause = "LIMIT %s" if limit is not None else ""
    parameters = [max(1, min(int(limit), 500))] if limit is not None else []
    rows = connection.execute(
            f"""
            SELECT transaction.id, instrument.symbol, transaction.transaction_type,
                   transaction.quantity, transaction.price, transaction.amount,
                   transaction.fees, transaction.realized_pnl, transaction.currency,
                   transaction.account, transaction.executed_at, transaction.notes,
                   transaction.idempotency_key, transaction.reverses_transaction_id,
                   transaction.created_at,
                   (transaction.reverses_transaction_id IS NOT NULL) AS is_reversal,
                   EXISTS (
                       SELECT 1 FROM app.portfolio_transaction reversal
                       WHERE reversal.reverses_transaction_id = transaction.id
                   ) AS is_reversed
            FROM app.portfolio_transaction transaction
            LEFT JOIN catalog.instrument instrument ON instrument.id = transaction.instrument_id
            ORDER BY transaction.executed_at DESC, transaction.created_at DESC
            {limit_clause}
            """,
            parameters,
        ).fetchall()
    return [_serialize_row(dict(row)) for row in rows]


def _normalize_transaction(fields: dict[str, Any]) -> dict[str, Any]:
    transaction_type = str(fields.get("transaction_type") or "").strip().lower()
    if transaction_type not in TRANSACTION_TYPES:
        raise ValueError(f"transaction_type must be one of {', '.join(sorted(TRANSACTION_TYPES))}")
    raw_symbol = str(fields.get("symbol") or "").strip()
    symbol = canonical_symbol(raw_symbol) if raw_symbol else ""
    if transaction_type in POSITION_TYPES | {"dividend", "split"} and not symbol:
        raise ValueError("symbol is required for this transaction type")
    quantity = _optional_nonnegative(fields.get("quantity"), "quantity")
    price = _optional_nonnegative(fields.get("price"), "price")
    fees = _optional_nonnegative(fields.get("fees"), "fees") or 0.0
    amount = _optional_nonnegative(fields.get("amount"), "amount")
    quantity = _quantize(quantity, 8)
    price = _quantize(price, 6)
    fees = _quantize(fees, 6) or 0.0
    amount = _quantize(amount, 6)
    if transaction_type in POSITION_TYPES and (quantity is None or quantity <= 0):
        raise ValueError("quantity must be greater than zero")
    if transaction_type in {"opening_balance", "buy", "sell", "transfer_in", "transfer_out"} and price is None:
        raise ValueError("price is required for position transactions")
    if transaction_type in CASH_TYPES and amount is None:
        raise ValueError("amount is required for cash transactions")
    if transaction_type in POSITION_TYPES:
        amount = _quantize(float(quantity or 0) * float(price or 0), 6)
    executed_at = _datetime(fields.get("executed_at"))
    idempotency_key = str(fields.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    currency = str(fields.get("currency") or "USD").strip().upper()
    if currency != "USD":
        raise ValueError("currency must be USD until FX conversion is supported")
    account = str(fields.get("account") or "manual").strip() or "manual"
    if account != "manual":
        raise ValueError("account must be manual until account-scoped cost basis is supported")
    return {
        "symbol": symbol or None,
        "transaction_type": transaction_type,
        "quantity": quantity,
        "price": price,
        "amount": amount,
        "fees": fees,
        "currency": currency,
        "account": account,
        "executed_at": executed_at,
        "notes": str(fields.get("notes") or "").strip(),
        "idempotency_key": idempotency_key,
        "reverses_transaction_id": fields.get("reverses_transaction_id"),
        "expected_position_version": (
            str(fields["expected_position_version"])
            if fields.get("expected_position_version") is not None
            else None
        ),
    }


def _reject_backdated_transaction(
    connection: Any,
    fields: dict[str, Any],
    *,
    instrument_id: int | None = None,
) -> None:
    if not fields.get("symbol"):
        return
    if instrument_id is None:
        instrument = connection.execute(
            "SELECT id FROM catalog.instrument WHERE symbol = %s",
            [fields["symbol"]],
        ).fetchone()
        instrument_id = int(instrument["id"]) if instrument else None
    if instrument_id is None:
        return
    latest = connection.execute(
        """
        SELECT max(transaction.executed_at) AS executed_at
        FROM app.portfolio_transaction transaction
        WHERE transaction.instrument_id = %s
          AND transaction.reverses_transaction_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM app.portfolio_transaction reversal
              WHERE reversal.reverses_transaction_id = transaction.id
          )
        """,
        [instrument_id],
    ).fetchone()
    latest_at = latest["executed_at"] if latest else None
    if latest_at and fields["executed_at"] < latest_at:
        raise ValueError(
            f"backdated transactions are not supported; latest {fields['symbol']} activity is {latest_at.isoformat()}"
        )


def _transaction_matches(existing: dict[str, Any], requested: dict[str, Any]) -> bool:
    scalar_pairs = (
        (existing.get("symbol"), requested.get("symbol")),
        (existing.get("transaction_type"), requested.get("transaction_type")),
        (existing.get("currency"), requested.get("currency")),
        (existing.get("account"), requested.get("account")),
        (existing.get("notes") or "", requested.get("notes") or ""),
    )
    if any(left != right for left, right in scalar_pairs):
        return False
    expected_amount = requested.get("amount")
    if expected_amount is None and requested.get("quantity") is not None and requested.get("price") is not None:
        expected_amount = float(requested["quantity"]) * float(requested["price"])
    if requested.get("transaction_type") == "split":
        expected_amount = 0.0
    numeric_pairs = (
        (existing.get("quantity"), requested.get("quantity")),
        (existing.get("price"), requested.get("price")),
        (existing.get("amount"), expected_amount),
        (existing.get("fees"), requested.get("fees")),
    )
    for left, right in numeric_pairs:
        if left is None or right is None:
            if left is not right:
                return False
        elif abs(float(left) - float(right)) > 1e-8:
            return False
    return _datetime(existing.get("executed_at")) == requested["executed_at"]


def _transaction_preview(fields: dict[str, Any], position: dict[str, Any] | None) -> dict[str, Any]:
    transaction_type = fields["transaction_type"]
    old_quantity = float(position.get("quantity") or 0) if position else 0.0
    old_average_cost = float(position.get("average_cost") or 0) if position else 0.0
    quantity = float(fields.get("quantity") or 0)
    price = float(fields.get("price") or 0)
    fees = float(fields.get("fees") or 0)
    amount = float(fields.get("amount") or quantity * price)
    new_quantity = old_quantity
    new_average_cost = old_average_cost
    realized_pnl = 0.0

    if transaction_type == "opening_balance" and old_quantity:
        raise ValueError("opening balance requires an empty position; record a buy instead")
    if transaction_type in {"opening_balance", "buy", "transfer_in"}:
        new_quantity = old_quantity + quantity
        new_average_cost = (
            (old_quantity * old_average_cost + quantity * price + fees) / new_quantity
            if new_quantity
            else 0.0
        )
    elif transaction_type in {"sell", "transfer_out"}:
        if quantity > old_quantity:
            raise ValueError(f"cannot {transaction_type.replace('_', ' ')} {quantity:g}; only {old_quantity:g} held")
        new_quantity = old_quantity - quantity
        realized_pnl = (price - old_average_cost) * quantity - fees if transaction_type == "sell" else 0.0
        if not new_quantity:
            new_average_cost = 0.0
    elif transaction_type == "split":
        if old_quantity <= 0:
            raise ValueError("split requires an existing position")
        if quantity <= 0:
            raise ValueError("quantity must be the positive split ratio")
        new_quantity = old_quantity * quantity
        new_average_cost = old_average_cost / quantity if quantity else old_average_cost
        amount = 0.0
    elif transaction_type in CASH_TYPES:
        amount = float(fields.get("amount") or 0)

    return {
        "symbol": fields.get("symbol"),
        "transaction_type": transaction_type,
        "old_quantity": old_quantity,
        "new_quantity": new_quantity,
        "old_average_cost": old_average_cost,
        "new_average_cost": new_average_cost,
        "amount": amount,
        "fees": fees,
        "realized_pnl": realized_pnl,
    }


def _apply_position_preview(
    connection: Any,
    instrument_id: int,
    fields: dict[str, Any],
    preview: dict[str, Any],
) -> None:
    quantity = float(preview["new_quantity"])
    if quantity <= 0:
        connection.execute("DELETE FROM app.portfolio_position WHERE instrument_id = %s", [instrument_id])
        return
    purchase_date = (
        fields["executed_at"].astimezone(MARKET_TIMEZONE).date()
        if not preview["old_quantity"]
        else None
    )
    connection.execute(
        """
        INSERT INTO app.portfolio_position
            (instrument_id, quantity, average_cost, purchase_date, notes, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (instrument_id) DO UPDATE
        SET quantity = EXCLUDED.quantity,
            average_cost = EXCLUDED.average_cost,
            purchase_date = COALESCE(app.portfolio_position.purchase_date, EXCLUDED.purchase_date),
            notes = CASE WHEN EXCLUDED.notes = '' THEN app.portfolio_position.notes ELSE EXCLUDED.notes END,
            updated_at = now()
        """,
        [instrument_id, quantity, preview["new_average_cost"], purchase_date, fields["notes"]],
    )


def _position_for_symbol(connection: Any, symbol: str | None) -> dict[str, Any] | None:
    if not symbol:
        return None
    row = connection.execute(
        """
        SELECT position.*
        FROM app.portfolio_position position
        JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
        WHERE instrument.symbol = %s
        """,
        [symbol],
    ).fetchone()
    return dict(row) if row else None


def _position_for_instrument(connection: Any, instrument_id: int | None, *, lock: bool) -> dict[str, Any] | None:
    if instrument_id is None:
        return None
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        "SELECT * FROM app.portfolio_position WHERE instrument_id = %s" + suffix,
        [instrument_id],
    ).fetchone()
    return dict(row) if row else None


def _position_version(position: dict[str, Any] | None) -> str:
    if position is None:
        return "empty"
    updated_at = position.get("updated_at")
    updated = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "")
    return "|".join(
        (
            str(position.get("quantity") or 0),
            str(position.get("average_cost") or 0),
            str(position.get("purchase_date") or ""),
            updated,
        )
    )


def _mark_owned_thesis(connection: Any, instrument_id: int) -> None:
    connection.execute(
        """
        UPDATE app.thesis
        SET thesis = jsonb_set(thesis, '{position_status}', %s, true), updated_at = now()
        WHERE instrument_id = %s AND status = 'current'
        """,
        [Jsonb("owned"), instrument_id],
    )
    connection.execute(
        """
        INSERT INTO app.thesis (instrument_id, revision, status, thesis)
        SELECT %s, 1, 'current', %s
        WHERE NOT EXISTS (
            SELECT 1 FROM app.thesis WHERE instrument_id = %s AND status = 'current'
        )
        """,
        [instrument_id, Jsonb(DEFAULT_OWNED_THESIS), instrument_id],
    )


def _mark_exited_thesis(connection: Any, instrument_id: int) -> None:
    connection.execute(
        "DELETE FROM app.thesis WHERE instrument_id = %s AND revision = 1 AND status = 'current' AND thesis = %s",
        [instrument_id, Jsonb(DEFAULT_OWNED_THESIS)],
    )
    connection.execute(
        """
        UPDATE app.thesis
        SET thesis = jsonb_set(thesis, '{position_status}', %s, true), updated_at = now()
        WHERE instrument_id = %s AND status = 'current'
        """,
        [Jsonb("exited"), instrument_id],
    )


def _rebuild_position_projection(connection: Any, instrument_id: int) -> None:
    rows = connection.execute(
        """
        SELECT transaction.*
        FROM app.portfolio_transaction transaction
        WHERE transaction.instrument_id = %s
          AND transaction.reverses_transaction_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM app.portfolio_transaction reversal
              WHERE reversal.reverses_transaction_id = transaction.id
          )
        ORDER BY transaction.executed_at, transaction.created_at, transaction.id
        """,
        [instrument_id],
    ).fetchall()
    connection.execute("DELETE FROM app.portfolio_position WHERE instrument_id = %s", [instrument_id])
    position: dict[str, Any] | None = None
    purchase_date = None
    notes = ""
    for source in rows:
        row = dict(source)
        fields = {
            "symbol": None,
            "transaction_type": row["transaction_type"],
            "quantity": float(row["quantity"]) if row.get("quantity") is not None else None,
            "price": float(row["price"]) if row.get("price") is not None else None,
            "amount": float(row["amount"]) if row.get("amount") is not None else None,
            "fees": float(row.get("fees") or 0),
            "executed_at": row["executed_at"],
            "notes": row.get("notes") or "",
        }
        preview = _transaction_preview(fields, position)
        connection.execute(
            "UPDATE app.portfolio_transaction SET realized_pnl = %s WHERE id = %s",
            [preview.get("realized_pnl") or 0, row["id"]],
        )
        if row["transaction_type"] in POSITION_TYPES | {"split"}:
            old_quantity = float(position.get("quantity") or 0) if position else 0.0
            position = {
                "quantity": preview["new_quantity"],
                "average_cost": preview["new_average_cost"],
            }
            if float(preview["new_quantity"]) <= 0:
                purchase_date = None
                notes = ""
            elif old_quantity <= 0:
                purchase_date = row["executed_at"].astimezone(MARKET_TIMEZONE).date()
            if row.get("notes"):
                notes = str(row["notes"])
    if position and float(position.get("quantity") or 0) > 0:
        connection.execute(
            """
            INSERT INTO app.portfolio_position
                (instrument_id, quantity, average_cost, purchase_date, notes, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            """,
            [instrument_id, position["quantity"], position["average_cost"], purchase_date, notes],
        )
        _mark_owned_thesis(connection, instrument_id)
    else:
        _mark_exited_thesis(connection, instrument_id)


def _transaction_row(connection: Any, transaction_id: Any) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT transaction.id, instrument.symbol, transaction.transaction_type,
               transaction.quantity, transaction.price, transaction.amount,
               transaction.fees, transaction.realized_pnl, transaction.currency,
               transaction.account, transaction.executed_at, transaction.notes,
               transaction.idempotency_key, transaction.reverses_transaction_id,
               transaction.created_at,
               (transaction.reverses_transaction_id IS NOT NULL) AS is_reversal,
               EXISTS (
                   SELECT 1 FROM app.portfolio_transaction reversal
                   WHERE reversal.reverses_transaction_id = transaction.id
               ) AS is_reversed
        FROM app.portfolio_transaction transaction
        LEFT JOIN catalog.instrument instrument ON instrument.id = transaction.instrument_id
        WHERE transaction.id = %s
        """,
        [transaction_id],
    ).fetchone()
    return _serialize_row(dict(row))


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            float(value)
            if isinstance(value, Decimal)
            else value.astimezone(UTC).isoformat()
            if isinstance(value, datetime)
            else str(value)
            if key in {"id", "reverses_transaction_id"} and value is not None
            else value
        )
        for key, value in row.items()
    }


def _optional_nonnegative(value: Any, name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _quantize(value: float | None, places: int) -> float | None:
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("executed_at must be an ISO 8601 timestamp") from exc
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if parsed > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("executed_at cannot be more than five minutes in the future")
    return parsed
