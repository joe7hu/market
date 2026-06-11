"""Deterministic 10x options radar data flywheel.

This was a single ~5.8k-line module; it is now a package split along the
pipeline's natural seams (leaves: ``constants``/``coerce``/``greeks``/``gates``;
stages: ``snapshots`` -> ``features`` -> ``candidates`` -> ``opportunities``;
plus ``shadow``/``marks``/``calibration``/``attributions``/``strategy_*`` for
the learning loop, and ``_impl`` for the ``refresh_options_radar`` orchestrator).

The full public surface is re-exported here so existing imports
(``from investment_panel.core.options_radar import X``) keep working.
"""

from __future__ import annotations

from investment_panel.core.options_radar.constants import *  # noqa: F401,F403
from investment_panel.core.options_radar.session import *  # noqa: F401,F403
from investment_panel.core.options_radar.registration import *  # noqa: F401,F403
from investment_panel.core.options_radar.dbutil import *  # noqa: F401,F403
from investment_panel.core.options_radar.indicators import *  # noqa: F401,F403
from investment_panel.core.options_radar.snapshots import *  # noqa: F401,F403
from investment_panel.core.options_radar.features import *  # noqa: F401,F403
from investment_panel.core.options_radar.features_surface import *  # noqa: F401,F403
from investment_panel.core.options_radar.scoring import *  # noqa: F401,F403
from investment_panel.core.options_radar.candidates import *  # noqa: F401,F403
from investment_panel.core.options_radar.calibration import *  # noqa: F401,F403
from investment_panel.core.options_radar.regime import *  # noqa: F401,F403
from investment_panel.core.options_radar.opportunity_scoring import *  # noqa: F401,F403
from investment_panel.core.options_radar.opportunity_contract import *  # noqa: F401,F403
from investment_panel.core.options_radar.opportunities import *  # noqa: F401,F403
from investment_panel.core.options_radar.shadow import *  # noqa: F401,F403
from investment_panel.core.options_radar.marks import *  # noqa: F401,F403
from investment_panel.core.options_radar.alerts import *  # noqa: F401,F403
from investment_panel.core.options_radar.state import *  # noqa: F401,F403
from investment_panel.core.options_radar.attributions import *  # noqa: F401,F403
from investment_panel.core.options_radar.strategy_proposals import *  # noqa: F401,F403
from investment_panel.core.options_radar.strategy_backtest import *  # noqa: F401,F403
from investment_panel.core.options_radar.strategy_promotion import *  # noqa: F401,F403
from investment_panel.core.options_radar.strategy_outcomes import *  # noqa: F401,F403
from investment_panel.core.options_radar._impl import *  # noqa: F401,F403

# Private helpers that external modules/tests import directly. ``import *`` does
# not pull leading-underscore names, so re-export them explicitly from their new
# homes.
from investment_panel.core.options_radar.features_surface import (  # noqa: F401
    _expiry_atm_iv_and_skew,
    _iv_percentile_252d,
)
from investment_panel.core.options_radar.regime import _market_regime  # noqa: F401
from investment_panel.core.options_radar.scoring import _setup_score  # noqa: F401
from investment_panel.core.options_radar.strategy_outcomes import (  # noqa: F401
    _backtest_verdict,
    _strategy_arm_significance,
    _walk_forward_folds,
)
