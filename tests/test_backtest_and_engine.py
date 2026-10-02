from datetime import datetime, timezone

import pandas as pd

from trade_away.backtest import run_backtest
from trade_away.config import Settings
from trade_away.db import BarRow, Store
from trade_away.engine import check_stops, run_daily
from trade_away.execution import FakeBroker
from trade_away.strategies import TrendFollowing


def series_frame(closes, start="2024-01-01", spread=1.0):
    close = pd.Series(closes, dtype=float, index=pd.date_range(start, periods=len(closes), tz="UTC"))
    return pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1e6})


V_SHAPE = [100 - i for i in range(30)] + [70 + 2 * i for i in range(1, 60)]


def test_backtest_trend_trade_makes_money_on_a_rally():
    bars = {"UP": series_frame(V_SHAPE)}
    result = run_backtest(bars, {"UP": "stock"}, (TrendFollowing(fast=5, slow=10),), benchmark_symbol=None)
    m = result.metrics()
    assert len(result.trades) == 1 and result.trades[0].entry > 0
    assert m["total_return"] > 0
    assert result.equity.index[0] == bars["UP"].index[0]


def test_backtest_stop_exits_at_stop_price():
    closes = V_SHAPE[:45] + [10.0] * 5  # crash right after entry
    result = run_backtest({"X": series_frame(closes)}, {"X": "stock"},
                          (TrendFollowing(fast=5, slow=10),), benchmark_symbol=None, cost=0)
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.exit == 10.0  # gapped below the stop: filled at the open
    assert result.metrics()["max_drawdown"] > 0


def test_backtest_flat_market_no_trades():
    result = run_backtest({"F": series_frame([50.0] * 300)}, {"F": "stock"}, benchmark_symbol=None)
    assert result.trades == [] and result.metrics()["total_return"] == 0


def _store_with_bars(symbol, closes, asset="stock"):
    store = Store(":memory:")
    df = series_frame(closes, start="2026-06-01")
    store.upsert_bars([BarRow(symbol, asset, "1Day", ts.to_pydatetime(), r.open, r.high, r.low, r.close,
                              r.volume, None, "sip") for ts, r in df.iterrows()])
    return store, df


def test_engine_buys_through_risk_and_journals():
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(1, 22)]
    store, df = _store_with_bars("UP", closes)
    store.replace_universe("2026-07-21", [("UP", closes[-1], 1e9)])
    # find the day the crossover fires and run the engine on that history
    strat = TrendFollowing(fast=5, slow=10)
    fire = next(i for i in range(12, len(df) + 1) if (s := strat.evaluate("UP", df.iloc[:i], False)) and s.side == "buy")
    store.execute("DELETE FROM bars WHERE ts > ?", (df.index[fire - 1].isoformat(),))
    broker = FakeBroker(prices={"UP": closes[fire - 1]})
    settings = Settings(crypto_symbols=[])
    actions = run_daily(settings, store, broker, strategies=(strat,), now=df.index[fire - 1].to_pydatetime())
    assert [a.side for a in actions] == ["buy"] and actions[0].decision.approved
    assert broker.orders[0][0] == "buy" and broker.orders[0][3] < closes[fire - 1]
    assert store.query("SELECT COUNT(*) AS n FROM open_trades")[0]["n"] == 1
    assert store.query("SELECT approved FROM decisions")[0]["approved"] == 1


def test_engine_dry_run_sends_nothing():
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(1, 5)]
    store, df = _store_with_bars("UP", closes)
    store.replace_universe("2026-07-01", [("UP", closes[-1], 1e9)])
    broker = FakeBroker(prices={"UP": closes[-1]})
    strat = TrendFollowing(fast=5, slow=10)
    actions = run_daily(Settings(crypto_symbols=[]), store, broker, strategies=(strat,), dry_run=True,
                        now=df.index[-1].to_pydatetime())
    assert broker.orders == []
    assert store.query("SELECT COUNT(*) AS n FROM open_trades")[0]["n"] == 0
    assert all(r["dry_run"] == 1 for r in store.query("SELECT dry_run FROM decisions"))
    assert len(actions) == len(store.query("SELECT id FROM decisions"))


def test_crypto_soft_stop():
    store = Store(":memory:")
    broker = FakeBroker(prices={"BTC/USD": 50_000})
    broker.buy("BTC/USD", "crypto", 0.1, 55_000)
    store.execute("INSERT INTO open_trades VALUES ('rules','BTC/USD','trend','2026-10-01',60000,0.1,55000)")
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    store.set_latest_price("BTC/USD", "crypto", now, 50_000)
    assert check_stops(Settings(), store, broker, now=now) == ["BTC/USD"]
    assert broker.orders[-1][0] == "sell"
    assert store.query("SELECT COUNT(*) AS n FROM open_trades")[0]["n"] == 0


def test_reconcile_keeps_unfilled_entries_and_drops_stopped_out_ones():
    store = Store(":memory:")
    store.execute("INSERT INTO open_trades VALUES ('rules','WAIT','trend','2026-10-03',100,10,90)")
    store.execute("INSERT INTO open_trades VALUES ('rules','GONE','trend','2026-09-01',100,10,90)")
    broker = FakeBroker()
    broker.pending = {"WAIT"}  # order placed Friday night, fills Monday
    run_daily(Settings(crypto_symbols=[]), store, broker, now=datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert [r["symbol"] for r in store.query("SELECT symbol FROM open_trades")] == ["WAIT"]
    note = store.query("SELECT symbol, reason FROM decisions")
    assert [(r["symbol"], r["reason"]) for r in note] == [("GONE", "stop-loss filled")]
