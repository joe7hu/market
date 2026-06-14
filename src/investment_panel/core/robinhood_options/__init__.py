"""Read-only Robinhood option-chain collection (package facade).

Two responsibility submodules sit behind this facade: :mod:`auth` owns the MCP
OAuth/PKCE token dance, and :mod:`collector` owns chain collection and the
``store_options_chain`` normalization. Import the public names from this package;
internal helpers live in the submodules.
"""

from __future__ import annotations

from investment_panel.core.robinhood_options.auth import (
    RobinhoodAuthRequired,
    _authorization_server_metadata,
    authorize_robinhood_mcp,
    load_robinhood_access_token,
)
from investment_panel.core.robinhood_options.collector import (
    RobinhoodClient,
    RobinhoodMcpClient,
    collect_robinhood_option_chains,
    option_quote_row,
    select_robinhood_expiries,
)

__all__ = [
    "RobinhoodAuthRequired",
    "RobinhoodClient",
    "RobinhoodMcpClient",
    "authorize_robinhood_mcp",
    "collect_robinhood_option_chains",
    "load_robinhood_access_token",
    "option_quote_row",
    "select_robinhood_expiries",
]
