"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

import hashlib


THIRTEEN_F_FORMS = {"13F-HR", "13F-HR/A"}
THIRTEEN_F_CAVEAT = (
    "Form 13F is a delayed quarterly disclosure, generally due up to 45 days after quarter end; "
    "it reports long positions in covered US securities as of the report date and does not show "
    "current holdings, shorts, many derivatives, cost basis, or full trade intent."
)
PUBLIC_DISCLOSURE_CAVEAT = (
    "Replica portfolios are deterministic estimates from public disclosure records. Congressional disclosures "
    "often report amount ranges, delayed filing dates, and transaction intent rather than exact live positions."
)


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
