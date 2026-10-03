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


def _frame(closes, spread=1.0):
    import pandas as pd

    close = pd.Series(closes, dtype=float, index=pd.date_range("2024-01-01", periods=len(closes), tz="UTC"))
    return pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1e6})


def test_breakout_buys_new_high_and_sells_new_low():
    from trade_away.strategies import Breakout

    b = Breakout(entry=20, exit=10)
    flat = [100.0] * 30
    assert b.evaluate("X", _frame(flat), in_position=False) is None
    buy = b.evaluate("X", _frame(flat + [105.0]), in_position=False)
    assert buy and buy.side == "buy" and buy.stop < 105
    sell = b.evaluate("X", _frame(flat + [95.0]), in_position=True)
    assert sell and sell.side == "sell"


def test_momentum_buys_only_the_leaders():
    from trade_away.strategies import Momentum

    n = 260
    bars = {f"S{i}": _frame([100 * (1 + 0.0005 * i) ** d for d in range(n)]) for i in range(1, 6)}
    m = Momentum(top=2, keep=3).prepare(bars)
    assert m.evaluate("S5", bars["S5"], in_position=False).side == "buy"
    assert m.evaluate("S1", bars["S1"], in_position=False) is None
    assert m.evaluate("S1", bars["S1"], in_position=True).side == "sell"  # rank 5 is outside the top 3
    assert Momentum().evaluate("S5", bars["S5"], in_position=False) is None  # no ranking, no trade


def test_momentum_ranks_do_not_look_ahead():
    from trade_away.strategies import momentum_ranks

    bars = {"A": _frame([100.0] * 50), "B": _frame([100.0] * 50)}
    before = momentum_ranks(bars, 20, 5)
    bars["B"].iloc[-1, bars["B"].columns.get_loc("close")] = 1000.0
    after = momentum_ranks(bars, 20, 5)
    assert before.iloc[:-1].equals(after.iloc[:-1])
