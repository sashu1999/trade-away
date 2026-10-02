import math

import pandas as pd

from trade_away.indicators import atr, rsi, sma
from trade_away.strategies import DipBuying, TrendFollowing


def frame(closes, spread=1.0):
    close = pd.Series(closes, dtype=float, index=pd.date_range("2024-01-01", periods=len(closes), tz="UTC"))
    return pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1e6})


def test_sma_and_rsi_basics():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert math.isnan(sma(s, 3).iloc[1])
    assert rsi(pd.Series(range(1, 30), dtype=float), 14).iloc[-1] == 100
    assert rsi(pd.Series(range(30, 1, -1), dtype=float), 14).iloc[-1] == 0
    assert rsi(pd.Series([5.0] * 30), 14).iloc[-1] == 50


def test_atr_constant_range():
    df = frame([100.0] * 30, spread=2.0)
    assert atr(df["high"], df["low"], df["close"]).iloc[-1] == 4.0


def test_trend_buys_on_cross_and_sells_when_it_reverses():
    strat = TrendFollowing(fast=5, slow=10)
    falling_then_rising = [100 - i for i in range(15)] + [86 + 3 * i for i in range(1, 15)]
    df = frame(falling_then_rising)
    buys = [i for i in range(strat.min_bars, len(df) + 1)
            if (sig := strat.evaluate("X", df.iloc[:i], False)) and sig.side == "buy"]
    assert len(buys) == 1  # only on the crossing bar
    sig = strat.evaluate("X", df.iloc[:buys[0]], False)
    assert sig.stop < sig.price
    assert strat.evaluate("X", df.iloc[:buys[0]], True) is None  # still trending: hold
    reversed_df = frame(falling_then_rising + [120 - 5 * i for i in range(10)])
    assert strat.evaluate("X", reversed_df, True).side == "sell"


def test_dip_buys_sharp_drop_in_uptrend_only():
    strat = DipBuying(trend=50)
    uptrend = [100 + i for i in range(60)]
    dip = frame(uptrend + [150, 140])
    sig = strat.evaluate("X", dip, False)
    assert sig and sig.side == "buy" and sig.stop < sig.price
    downtrend = frame([200 - i for i in range(60)] + [130, 120])
    assert strat.evaluate("X", downtrend, False) is None
    bounce = frame(uptrend + [150, 140, 165])
    assert strat.evaluate("X", bounce, True).side == "sell"


def test_strategies_need_enough_history():
    assert TrendFollowing().evaluate("X", frame([100.0] * 10), False) is None
    assert DipBuying().evaluate("X", frame([100.0] * 10), False) is None
