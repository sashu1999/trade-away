from datetime import datetime, timezone

from trade_away.db import BarRow, Store

T0 = datetime(2026, 10, 2, 14, 30, tzinfo=timezone.utc)


def bar(symbol="AAPL", ts=T0, close=100.0):
    return BarRow(symbol, "stock", "1Min", ts, close, close, close, close, 1000, None, "iex")


def test_upsert_bars_replaces_same_key():
    store = Store(":memory:")
    store.upsert_bars([bar(close=100.0)])
    store.upsert_bars([bar(close=101.0)])
    rows = store.query("SELECT close FROM bars")
    assert [r["close"] for r in rows] == [101.0]


def test_naive_timestamps_are_treated_as_utc():
    store = Store(":memory:")
    store.upsert_bars([bar(ts=datetime(2026, 10, 2, 14, 30))])
    assert store.query("SELECT ts FROM bars")[0]["ts"] == "2026-10-02T14:30:00+00:00"


def test_universe_keeps_rank_order_and_replaces_same_day():
    store = Store(":memory:")
    store.replace_universe("2026-10-01", [("OLD", 10, 1e8)])
    store.replace_universe("2026-10-02", [("SPY", 600, 3e10), ("AAPL", 250, 1e10)])
    store.replace_universe("2026-10-02", [("SPY", 600, 3e10), ("NVDA", 180, 2e10), ("AAPL", 250, 1e10)])
    assert store.latest_universe() == ["SPY", "NVDA", "AAPL"]
    assert store.latest_universe(limit=2) == ["SPY", "NVDA"]


def test_events_skip_duplicates():
    store = Store(":memory:")
    event = ("n1", "news", "AAPL", T0, "Apple beats", "https://x", "alpaca:benzinga")
    assert store.insert_events([event]) == 1
    assert store.insert_events([event]) == 0
