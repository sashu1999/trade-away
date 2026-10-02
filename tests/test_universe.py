import pandas as pd

from trade_away.universe import screen


def daily(symbol, close, volume, days=20):
    return pd.DataFrame({
        "symbol": symbol,
        "ts": pd.date_range("2026-09-01", periods=days, freq="D", tz="UTC"),
        "close": close,
        "volume": volume,
    })


def test_screen_filters_price_liquidity_and_history_then_ranks():
    df = pd.concat([
        daily("BIG", 100, 1_000_000),      # $100M a day
        daily("MID", 50, 600_000),         # $30M a day
        daily("THIN", 50, 100_000),        # $5M a day: too thin
        daily("PENNY", 2, 50_000_000),     # under $5
        daily("NEW", 100, 1_000_000, 5),   # only 5 days of history
    ])
    out = screen(df, min_price=5, min_dollar_volume=20_000_000)
    assert out["symbol"].tolist() == ["BIG", "MID"]
    assert out.iloc[0]["avg_dollar_volume"] == 100_000_000


def test_screen_uses_only_the_lookback_window():
    old_spike = daily("X", 10, 100_000_000, 40)
    old_spike.loc[old_spike.index[-20:], "volume"] = 100  # recent volume collapsed
    out = screen(old_spike, min_price=5, min_dollar_volume=20_000_000)
    assert out.empty


def test_screen_empty_input():
    assert screen(pd.DataFrame(), 5, 1).empty
