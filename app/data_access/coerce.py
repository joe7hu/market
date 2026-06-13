"""Generic row, JSON, numeric, and text coercion helpers."""

from __future__ import annotations
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Iterable



def _positive_number(value: Any, name: str, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return parsed




def _optional_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError as exc:
        raise ValueError("purchase_date must be YYYY-MM-DD") from exc




def _int_value(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback




def _first_row(tables: dict[str, list[dict[str, Any]]], *keys: str) -> dict[str, Any]:
    for key in keys:
        rows = tables.get(key) or []
        if rows:
            return rows[0]
    return {}




def _latest_row(rows: list[dict[str, Any]], date_keys: tuple[str, ...]) -> dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=lambda row: max((_timestamp(row.get(key)) for key in date_keys), default=0.0))




def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0




def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}




def _number(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return fallback
    return fallback




def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return json.dumps(jsonable(value), sort_keys=True)




def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, tuple):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str) and value.strip():
        parsed = _parsed_json(value)
        if isinstance(parsed, list):
            return [_text(item) for item in parsed if _text(item)]
        return [item.strip() for item in value.replace("|", ";").split(";") if item.strip()]
    return []




def _parsed_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None




def _text_join(value: Any) -> str:
    items = _text_list(value)
    if items:
        return " ".join(items)
    return _text(value)




def _fmt_money(value: float) -> str:
    if not value:
        return "-"
    return f"${value:,.2f}" if abs(value) < 1000 else f"${value:,.0f}"




def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"




def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None




def normalize_rows(table: Any) -> list[dict[str, Any]]:
    """Convert common table shapes into JSON-ready row dictionaries."""

    if table is None:
        return []
    if hasattr(table, "to_dict"):
        try:
            records = table.to_dict(orient="records")
            return [_row_dict(row) for row in records]
        except TypeError:
            pass
    if isinstance(table, dict):
        if "rows" in table:
            return normalize_rows(table["rows"])
        return [_row_dict(table)]
    if isinstance(table, Iterable) and not isinstance(table, (str, bytes)):
        return [_row_dict(row) for row in table]
    return [_row_dict(table)]




def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value




def _row_dict(row: Any) -> dict[str, Any]:
    if is_dataclass(row):
        return jsonable(asdict(row))
    if isinstance(row, dict):
        return jsonable(row)
    if hasattr(row, "_asdict"):
        return jsonable(row._asdict())
    if hasattr(row, "dict"):
        return jsonable(row.dict())
    if hasattr(row, "model_dump"):
        return jsonable(row.model_dump())
    if hasattr(row, "__dict__"):
        return jsonable(vars(row))
    return {"value": jsonable(row)}




def _row_symbols(row: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for field in ("ticker", "symbol", "peer_symbol", "security", "name"):
        value = row.get(field)
        if isinstance(value, str) and value:
            symbols.add(value.split(":")[-1].upper())
    for field in ("symbols", "related_symbols", "bullish_symbols", "bearish_symbols", "holder_names"):
        value = row.get(field)
        if isinstance(value, list):
            symbols.update(str(item).split(":")[-1].upper() for item in value if item)
        elif isinstance(value, str):
            symbols.update(item.strip().split(":")[-1].upper() for item in value.replace(";", ",").split(",") if item.strip())
    history = row.get("ticker_history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                symbols.update(_row_symbols(item))
    return symbols




def _matching_ticker_rows(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    ticker_fields = ("ticker", "symbol", "security", "name")
    matches: list[dict[str, Any]] = []
    for row in rows:
        if ticker in _row_symbols(row):
            matches.append(row)
            continue
        related = row.get("related_symbols")
        if isinstance(related, list) and any(str(item).split(":")[-1].upper() == ticker for item in related):
            matches.append(row)
            continue
        if isinstance(related, str):
            symbols = [item.strip().split(":")[-1].upper() for item in related.replace(";", ",").split(",")]
            if ticker in symbols:
                matches.append(row)
                continue
        symbols_value = row.get("symbols")
        if isinstance(symbols_value, list) and any(str(item).split(":")[-1].upper() == ticker for item in symbols_value):
            matches.append(row)
            continue
        if isinstance(symbols_value, str):
            symbols = [item.strip().split(":")[-1].upper() for item in symbols_value.replace(";", ",").split(",")]
            if ticker in symbols:
                matches.append(row)
                continue
        for field in ticker_fields:
            value = row.get(field)
            if isinstance(value, str) and value.split(":")[-1].upper() == ticker:
                matches.append(row)
                break
    return matches




def _is_empty(panel_data: PanelData) -> bool:
    if not panel_data.tables:
        return True
    for table in panel_data.tables.values():
        if table is None:
            continue
        if hasattr(table, "empty"):
            if not table.empty:
                return False
            continue
        try:
            if len(table) > 0:
                return False
        except TypeError:
            return False
    return True




def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
