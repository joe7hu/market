"""Provider-specific request identities supplied by the deployment."""

from __future__ import annotations

import os
from typing import Any


def provider_user_agent(config: Any, provider: str) -> str:
    """Keep identifying contacts scoped to providers that require them."""
    env_name = f"MARKET_{provider.upper()}_USER_AGENT"
    return str(os.environ.get(env_name) or config.market_data.user_agent)
