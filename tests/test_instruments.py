from investment_panel.core.instruments import infer_asset_class, normalize_symbol


def test_symbol_normalization_handles_crypto_aliases() -> None:
    assert normalize_symbol("btcusd") == "BTC-USD"
    assert normalize_symbol("ETH") == "ETH-USD"


def test_asset_class_inference_handles_non_equity_market_symbols() -> None:
    assert infer_asset_class("SPY") == "etf"
    assert infer_asset_class("BITO") == "etf"
    assert infer_asset_class("IXIC") == "index"
    assert infer_asset_class("USDJPY") == "fx"
    assert infer_asset_class("005930") == "foreign_equity"
    assert infer_asset_class("MSFT") == "equity"
