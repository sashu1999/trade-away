import numpy as np
import pandas as pd

from trade_away.db import Store
from trade_away.luck import evaluate, round_trips


def frame(closes, start="2026-01-01"):
    return pd.DataFrame({"close": closes}, index=pd.date_range(start, periods=len(closes), tz="UTC"), dtype=float)


def test_round_trips_pair_buys_with_sells_and_mark_open_ones():
    store = Store(":memory:")
    store.execute("INSERT INTO book_fills VALUES ('a','dip','X','buy',1,100,'2026-01-02T21:00:00+00:00',1)")
    store.execute("INSERT INTO book_fills VALUES ('b','dip','X','sell',1,110,'2026-01-05T21:00:00+00:00',1)")
    store.execute("INSERT INTO book_fills VALUES ('c','trend','Y','buy',1,50,'2026-01-03T21:00:00+00:00',1)")
    trips = round_trips(store, {"Y": frame([50, 50, 55, 60])})
    closed = next(t for t in trips if t.symbol == "X")
    assert abs(closed.ret - 0.10) < 1e-9 and not closed.still_open
    still = next(t for t in trips if t.symbol == "Y")
    assert still.still_open and abs(still.ret - 0.20) < 1e-9


def test_a_bot_that_picks_the_best_stock_beats_random_picks():
    rng = np.random.default_rng(1)
    closes = {f"S{i}": frame(100 * np.cumprod(1 + rng.normal(0, 0.02, 120))) for i in range(30)}
    closes["SPY"] = frame(np.linspace(100, 110, 120))
    days = closes["S0"].index
    store = Store(":memory:")
    for n in range(35):  # one position per symbol at a time, as live
        a, b = days[3 * n], days[3 * n + 2]
        best = max((s for s in closes if s != "SPY"),
                   key=lambda s: closes[s]["close"].loc[b] / closes[s]["close"].loc[a])
        store.execute("INSERT INTO book_fills VALUES (?, 'smart', ?, 'buy', 1, ?, ?, 1)",
                      (f"b{n}", best, closes[best]["close"].loc[a], a.isoformat()))
        store.execute("INSERT INTO book_fills VALUES (?, 'smart', ?, 'sell', 1, ?, ?, 1)",
                      (f"s{n}", best, closes[best]["close"].loc[b], b.isoformat()))
    [report] = evaluate(round_trips(store, closes), closes, sims=500)
    assert report.trades == 35 and report.percentile > 0.99
    assert report.verdict == "looks like skill" and report.spy_mean is not None
