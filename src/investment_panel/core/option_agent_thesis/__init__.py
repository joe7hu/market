"""Private raw-source ingestion — facade. Import from this package; add a
responsibility submodule and re-export it rather than growing a god-file.
"""
from __future__ import annotations

from investment_panel.core.option_agent_thesis.constants import (
    AGENT_THESIS_VERSION,
    AgentThesisValidationError,
    DEFAULT_AGENT_THESIS_REQUEST_LIMIT,
    PRICE_RE,
    STOP_WORDS,
)
from investment_panel.core.option_agent_thesis.dbutil import (
    decode_json_fields,
    first_row,
    query_decoded,
)
from investment_panel.core.option_agent_thesis.requests import (
    agent_thesis_prompt,
    build_agent_thesis_request,
    build_ondemand_agent_request,
    build_ticker_agent_context,
    refresh_agent_thesis_requests,
    refresh_option_agent_work,
    retire_superseded_agent_thesis_requests,
)
from investment_panel.core.option_agent_thesis.thesis import (
    attach_agent_theses_to_candidates,
    normalize_agent_thesis,
    upsert_agent_thesis,
)
from investment_panel.core.option_agent_thesis.validation import (
    build_agent_thesis_validation,
    refresh_agent_thesis_validations,
)

__all__ = [
    "AGENT_THESIS_VERSION",
    "AgentThesisValidationError",
    "DEFAULT_AGENT_THESIS_REQUEST_LIMIT",
    "PRICE_RE",
    "STOP_WORDS",
    "agent_thesis_prompt",
    "attach_agent_theses_to_candidates",
    "build_agent_thesis_request",
    "build_ondemand_agent_request",
    "build_ticker_agent_context",
    "build_agent_thesis_validation",
    "decode_json_fields",
    "first_row",
    "normalize_agent_thesis",
    "query_decoded",
    "refresh_agent_thesis_requests",
    "refresh_agent_thesis_validations",
    "refresh_option_agent_work",
    "retire_superseded_agent_thesis_requests",
    "upsert_agent_thesis",
]
