"""Canonical low-level scalar / JSON / temporal / string-list coercion primitives.

A leaf domain-helper module (depends on nothing else in the package) that holds
the generic coercion primitives previously reimplemented under different names in
each package's ``coerce.py``. Package ``coerce`` modules now import from here and
re-export under their historical local names so call sites and the public import
contract are unchanged ("move, don't rewrite").

Subtly different historical variants are kept as *distinct* functions rather than
merged, because callers depend on the observable differences (NaN handling,
``$``/``%`` stripping, returning ``0.0`` vs ``None``, dict vs dict-or-list JSON
shapes, naive vs UTC-aware datetimes, etc.).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime, time
from typing import Any


# --------------------------------------------------------------------------- #
# JSON parsing
# --------------------------------------------------------------------------- #


def parse_json(value: Any) -> Any:
    """Parse JSON to dict/list; falsy or unparseable input becomes ``{}``.

    dict/list values pass through unchanged. Any decode failure (broad except)
    yields an empty dict, never raises.
    """

    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def parse_json_dict(value: Any) -> dict[str, Any]:
    """Parse JSON expecting a dict; anything else (or failure) becomes ``{}``."""

    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def parse_json_dict_copy(value: Any) -> dict[str, Any]:
    """Parse JSON expecting a dict, returning a *copy*.

    A dict input is shallow-copied; strings are coerced via ``json.loads`` after
    ``str()``. Non-dict results and failures become ``{}``. Differs from
    :func:`parse_json_dict` by copying and by stringifying the input first.
    """

    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def parse_json_list(value: Any) -> list[dict[str, Any]]:
    """Parse JSON expecting a list of dicts; non-dict items are dropped."""

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def decode_json_value(value: Any) -> Any:
    """Decode a JSON scalar/blob; ``None``/``""`` become ``None``.

    dict/list values pass through. Unlike :func:`parse_json`, a malformed string
    *raises* (callers wrap this in their own try/except).
    """

    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


# --------------------------------------------------------------------------- #
# Numeric coercion
# --------------------------------------------------------------------------- #


def to_float_or_none(value: Any) -> float | None:
    """``float(value)`` or ``None`` for ``None``/unparseable input."""

    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def to_finite_float(value: Any) -> float | None:
    """Like :func:`to_float_or_none` but rejects NaN/inf (returns ``None``)."""

    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if number is None or not math.isfinite(number):
        return None
    return number


def float_from_comma(value: Any) -> float | None:
    """Float coercion that strips commas; ``None``/``""`` become ``None``."""

    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def number_from_any(value: Any) -> float:
    """Best-effort float, defaulting to ``0.0``; strips ``$``, ``,`` and ``%``."""

    if isinstance(value, (int, float)) and value == value:
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return 0.0
    return 0.0


def optional_number(value: Any) -> float | None:
    """Like :func:`number_from_any` but ``None``/``""``/NaN become ``None``."""

    if value in (None, ""):
        return None
    number = number_from_any(value)
    return number if number == number else None


def median(values: list[float | None]) -> float | None:
    """Median of the finite, non-``None`` values (rounded to 4dp), else ``None``."""

    cleaned = sorted(value for value in values if value is not None and value == value)
    if not cleaned:
        return None
    mid = len(cleaned) // 2
    if len(cleaned) % 2:
        return round(cleaned[mid], 4)
    return round((cleaned[mid - 1] + cleaned[mid]) / 2, 4)


def average(values: list[float | None]) -> float | None:
    """Mean of the finite, non-``None`` values (rounded to 4dp), else ``None``."""

    cleaned = [value for value in values if value is not None and value == value]
    return round(sum(cleaned) / len(cleaned), 4) if cleaned else None


def share(values: list[bool]) -> float:
    """Fraction of truthy values in ``values`` (``0.0`` for an empty list)."""

    return sum(1 for value in values if value) / len(values) if values else 0.0


def format_metric(value: float | None, unit: str) -> str:
    """Render a metric for display: ``None`` -> ``"n/a"``; units ``$``/``%``/``x``."""

    if value is None:
        return "n/a"
    if unit == "$":
        return f"${value / 1_000_000:.1f}M" if abs(value) >= 1_000_000 else f"${value:,.0f}"
    if unit == "%":
        return f"{value:+.1f}%"
    if unit == "x":
        return f"{value:.1f}x"
    return f"{value:.1f}"


def to_int_or_none(value: Any) -> int | None:
    """``int(value)`` or ``None`` for ``None``/unparseable input."""

    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Datetime / date / ISO parsing
# --------------------------------------------------------------------------- #


def parse_dt_utc(value: Any) -> datetime | None:
    """Parse to a UTC-aware ``datetime``; ``None``/``""``/bad input become ``None``.

    Accepts ``datetime`` (normalized to UTC), ``date`` (midnight UTC) and ISO
    strings (``Z`` accepted). Naive datetimes are *treated* as UTC.
    """

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.astimezone(UTC)


def parse_naive_datetime(value: Any) -> datetime | None:
    """Parse to a *naive* ``datetime`` (tzinfo stripped); empty becomes ``None``.

    Raises ``ValueError`` on a malformed non-empty string (callers handle it).
    """

    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "")
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def parse_date(value: Any) -> date | None:
    """Parse to a ``date``; empty becomes ``None``.

    Raises ``ValueError`` on a malformed non-empty string (callers handle it).
    """

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def parse_date_lenient(value: Any) -> date | None:
    """Parse to a ``date``, falling back to the first 10 chars; never raises."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def iso_string(value: Any) -> str:
    """ISO-format a ``datetime``/``date``; anything else is ``str()``-ed."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def iso_or_none(value: Any) -> str | None:
    """ISO-format a temporal value or pass through as string; ``None`` stays ``None``."""

    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def iso_or_none_strict(value: Any) -> str | None:
    """Like :func:`iso_or_none` but ``None``/``""`` both become ``None``.

    Only ``datetime``/``date`` are ISO-formatted; other values are ``str()``-ed.
    """

    if value in (None, ""):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def iso_date_string(value: Any) -> str | None:
    """Coerce to an ISO date string; unparseable text is returned unchanged."""

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text


# --------------------------------------------------------------------------- #
# String-list flattening
# --------------------------------------------------------------------------- #


def string_list(value: Any) -> list[str]:
    """Flatten a value into a list of non-empty trimmed strings.

    Lists/dicts are mapped to their (stringified) members; strings that look like
    JSON are decoded, otherwise split on ``|``/``;``/``,``.
    """

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") or stripped.startswith("{") or stripped.startswith('"'):
            try:
                return string_list(json.loads(stripped))
            except Exception:
                pass
        return [
            item.strip()
            for item in stripped.replace("|", ";").replace(",", ";").split(";")
            if item.strip()
        ]
    return [str(value).strip()] if str(value).strip() else []


def unique_strings(values: list[str]) -> list[str]:
    """De-duplicate a list of strings, preserving order and dropping falsy items."""

    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def stable_id(value: str) -> str:
    """Deterministic 24-char hex id (truncated SHA-256) for a string."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
