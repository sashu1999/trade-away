from datetime import datetime, timezone

import pandas as pd

from trade_away.backtest import run_backtest
from trade_away.config import Settings
from trade_away.db import BarRow, Store
from trade_away.engine import Bot, book_state, check_stops, leaderboard, run_daily, sync_fills
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
    actions = run_daily(settings, store, broker, bots=(Bot(strat),), now=df.index[fire - 1].to_pydatetime())
    assert [a.side for a in actions] == ["buy"] and actions[0].decision.approved
    # sized against the bot's $25k book, not the $100k account: 10% cap is $2,500
    assert actions[0].decision.qty * closes[fire - 1] <= 2_500
    assert broker.orders[0][0] == "buy" and broker.orders[0][3] < closes[fire - 1]
    assert store.query("SELECT COUNT(*) AS n FROM open_trades")[0]["n"] == 1
    assert store.query("SELECT approved FROM decisions")[0]["approved"] == 1


def test_engine_dry_run_sends_nothing():
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(1, 5)]
    store, df = _store_with_bars("UP", closes)
    store.replace_universe("2026-07-01", [("UP", closes[-1], 1e9)])
    broker = FakeBroker(prices={"UP": closes[-1]})
    strat = TrendFollowing(fast=5, slow=10)
    actions = run_daily(Settings(crypto_symbols=[]), store, broker, bots=(Bot(strat),), dry_run=True,
                        now=df.index[-1].to_pydatetime())
    assert broker.orders == []
    assert store.query("SELECT COUNT(*) AS n FROM open_trades")[0]["n"] == 0
    assert all(r["dry_run"] == 1 for r in store.query("SELECT dry_run FROM decisions"))
    assert len(actions) == len(store.query("SELECT id FROM decisions"))


def test_crypto_soft_stop():
    store = Store(":memory:")
    broker = FakeBroker(prices={"BTC/USD": 50_000})
    broker.buy("BTC/USD", "crypto", 0.1, 55_000)
    store.execute("INSERT INTO open_trades VALUES ('trend','BTC/USD','trend','2026-10-01',60000,0.1,55000)")
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    store.set_latest_price("BTC/USD", "crypto", now, 50_000)
    assert check_stops(Settings(), store, broker, now=now) == ["BTC/USD"]
    assert broker.orders[-1][0] == "sell"
    assert store.query("SELECT COUNT(*) AS n FROM open_trades")[0]["n"] == 0
    assert [(r["account"], r["side"]) for r in store.query("SELECT account, side FROM book_fills")] == [("trend", "sell")]


def test_stop_leg_fill_is_booked_to_the_bot():
    store = Store(":memory:")
    broker = FakeBroker(prices={"WAIT": 100, "GONE": 100})
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    for symbol in ("WAIT", "GONE"):
        store.execute("INSERT INTO open_trades VALUES ('trend',?,'trend','2026-10-01',100,10,90)", (symbol,))
    gone_id = broker.buy("GONE", "stock", 10, 90)
    store.execute("INSERT INTO book_fills VALUES (?,'trend','GONE','buy',10,100,'2026-10-01',0)", (gone_id,))
    broker.prices["GONE"] = 101  # filled a bit above the estimate
    broker.fills[gone_id][0] = broker.fills[gone_id][0].__class__(gone_id, None, "GONE", "buy", 10, 101, now)
    broker.pending = {"WAIT"}  # order placed Friday night, fills Monday
    broker.stop_out("GONE", 90)
    sync_fills(store, broker, now)
    assert [r["symbol"] for r in store.query("SELECT symbol FROM open_trades")] == ["WAIT"]
    note = store.query("SELECT symbol, reason, price FROM decisions")
    assert [(r["symbol"], r["reason"], r["price"]) for r in note] == [("GONE", "stop-loss filled", 90)]
    # 25,000 - 10 x 101 + 10 x 90; WAIT has no fill row yet so it costs nothing in cash
    state = book_state(store, broker.account(0), "trend", 25_000, [], now)
    assert state.cash == 25_000 - 1010 + 900
    assert set(state.positions) == {"WAIT"}


def test_books_are_separate_and_one_symbol_has_one_owner():
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(1, 22)]
    store, df = _store_with_bars("UP", closes)
    store.replace_universe("2026-07-21", [("UP", closes[-1], 1e9)])
    strat = TrendFollowing(fast=5, slow=10)
    fire = next(i for i in range(12, len(df) + 1) if (s := strat.evaluate("UP", df.iloc[:i], False)) and s.side == "buy")
    store.execute("DELETE FROM bars WHERE ts > ?", (df.index[fire - 1].isoformat(),))
    broker = FakeBroker(prices={"UP": closes[fire - 1]})
    twin = TrendFollowing(fast=5, slow=10, name="twin")
    bots = (Bot(strat), Bot(twin))
    now = df.index[fire - 1].to_pydatetime()
    actions = run_daily(Settings(crypto_symbols=[]), store, broker, bots=bots, now=now)
    assert len(actions) == 1, "the second bot must skip a symbol the first one bought"
    board = {r["bot"]: r for r in leaderboard(store, broker, Settings(crypto_symbols=[]), bots, now)}
    winner = actions[0].strategy
    loser = "twin" if winner == "trend" else "trend"
    assert board[loser]["equity"] == 25_000 and board[loser]["positions"] == 0
    assert board[winner]["positions"] == 1 and abs(board[winner]["equity"] - 25_000) < 1e-6


def test_backtest_mixed_calendars_fill_exits():
    # stocks stamped 04:00Z on weekdays only, crypto stamped 00:00Z every day
    n = 140
    days = pd.date_range("2024-01-01", periods=n * 2, tz="UTC")
    weekdays = days[days.dayofweek < 5][:n]
    wave = [100 + 20 * ((i // 25) % 2 and (25 - i % 25) or i % 25) / 25 for i in range(n)]
    stock = series_frame(wave).set_axis(weekdays + pd.Timedelta(hours=4))
    crypto = series_frame(wave * 2)[: len(days)].set_axis(days)
    result = run_backtest({"S": stock, "C": crypto}, {"S": "stock", "C": "crypto"},
                          (TrendFollowing(fast=5, slow=10),), benchmark_symbol=None)
    stock_trades = [t for t in result.trades if t.symbol == "S"]
    assert stock_trades, "exit orders on the stock must fill despite crypto-only days"
    assert all(t.exit_reason != "stop" for t in stock_trades)
    # one equity point per calendar day, not two
    assert result.equity.index.is_unique and (result.equity.index == result.equity.index.normalize()).all()
