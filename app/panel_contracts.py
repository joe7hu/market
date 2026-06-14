"""Compatibility facade for panel route/table contracts.

The canonical contract catalog lives in :mod:`investment_panel.core.panel.contracts`
so read-model dispatch, API payloads, and tests share one backend-owned module.
"""

from investment_panel.core.panel.contracts import *  # noqa: F401,F403
