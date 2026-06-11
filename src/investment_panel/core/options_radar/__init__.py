"""Deterministic 10x options radar data flywheel.

This package was split out of a single ~5.8k-line module. ``_impl`` still holds
the bulk of the pipeline; leaf concerns are being peeled into focused submodules
(``coerce``, ``greeks``, ...). The public surface is re-exported here so existing
imports (``from investment_panel.core.options_radar import X``) keep working.
"""

from __future__ import annotations

from investment_panel.core.options_radar._impl import *  # noqa: F401,F403

# Private helpers that external modules/tests import directly. These are not
# exported by ``import *`` (leading underscore), so re-export them explicitly.
from investment_panel.core.options_radar._impl import (  # noqa: F401
    _backtest_verdict,
    _expiry_atm_iv_and_skew,
    _iv_percentile_252d,
    _market_regime,
    _setup_score,
    _strategy_arm_significance,
    _walk_forward_folds,
)
