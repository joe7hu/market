"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

import re


AGENT_THESIS_VERSION = "option-thesis-agent-v1"
DEFAULT_AGENT_THESIS_REQUEST_LIMIT = 12
PRICE_RE = re.compile(r"(?:below|under|breaks below|stop(?: at)?|invalidation(?: at)?|\$)\s*\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


class AgentThesisValidationError(ValueError):
    """Raised when an agent thesis does not satisfy the structured contract."""


STOP_WORDS = {
    "about",
    "after",
    "before",
    "below",
    "consecutive",
    "expected",
    "growth",
    "improve",
    "improves",
    "next",
    "quarter",
    "quarters",
    "recover",
    "recovers",
    "related",
    "should",
    "stock",
    "target",
    "watch",
    "without",
}
