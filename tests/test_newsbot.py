from datetime import datetime, timedelta, timezone

import pandas as pd

from trade_away.db import BarRow, Store
from trade_away.luck import round_trips
from trade_away.newsbot import parse_scores, run_night

NOW = datetime(2026, 11, 21, 2, 40, tzinfo=timezone.utc)


def _bars(store, symbol, closes, end="2026-11-20"):
    days = pd.bdate_range(end=end, periods=len(closes), tz="UTC")
    store.upsert_bars([BarRow(symbol, "stock", "1Day", d.to_pydatetime(), c, c + 1, c - 1, c, 1e6, None, "iex")
                       for d, c in zip(days, closes)])


def _store():
    store = Store(":memory:")
    store.replace_universe("2026-11-20", [("UP", 100, 1e9), ("DOWN", 100, 1e9)])
    _bars(store, "SPY", [400.0] * 60)
    _bars(store, "UP", [50 + i for i in range(60)])        # above its 50-day average
    _bars(store, "DOWN", [120 - i for i in range(60)])     # below it
    for i, s in enumerate(["UP", "DOWN"]):
        store.insert_events([(f"n{i}", "news", s, NOW - timedelta(hours=3), f"{s} beats estimates", None, "test")])
    return store


def test_parse_scores_keeps_only_valid_asked_symbols():
    text = ('{"scores": [{"symbol": "UP", "score": 2, "reason": "beat"}, {"symbol": "EVIL", "score": 2},'
            ' {"symbol": "DOWN", "score": 7}, {"symbol": "X", "score": true}, "junk"]}')
    assert parse_scores(text, {"UP", "DOWN", "X"}) == {"UP": (2, "beat")}
    assert parse_scores("ignore previous instructions", {"UP"}) == {}


def test_buys_strong_news_only_in_an_uptrend_and_never_sends_orders():
    store = _store()
    seen = []

    def scorer(news):
        seen.append(set(news))
        return {"UP": (2, "beat"), "DOWN": (2, "beat")}

    r = run_night(store, scorer, now=NOW)
    assert seen == [{"UP", "DOWN"}]
    assert r.opened == ["UP"] and r.skipped is None
    trades = store.query("SELECT symbol, entry, stop, exit_day FROM shadow_trades")
    assert [(t["symbol"], t["entry"], t["exit_day"]) for t in trades] == [("UP", 109.0, None)]
    assert trades[0]["stop"] < 109
    assert store.query("SELECT COUNT(*) AS n FROM news_scores")[0]["n"] == 2
    assert store.query("SELECT COUNT(*) AS n FROM book_fills")[0]["n"] == 0  # record-only


def test_exits_on_stop_and_luck_sees_the_trade():
    store = _store()
    run_night(store, lambda news: {"UP": (2, "beat")}, now=NOW)
    stop = store.query("SELECT stop FROM shadow_trades")[0]["stop"]
    crash = stop - 5
    store.upsert_bars([BarRow("UP", "stock", "1Day", datetime(2026, 11, 23, tzinfo=timezone.utc),
                              crash, crash + 1, crash - 1, crash, 1e6, None, "iex")])
    _bars(store, "SPY", [400.0] * 61, end="2026-11-23")
    r = run_night(store, lambda news: {}, now=NOW + timedelta(days=3))
    assert r.closed == ["UP"]
    t = store.query("SELECT exit, exit_reason FROM shadow_trades")[0]
    assert t["exit_reason"] == "stop-loss" and t["exit"] == crash
    [trip] = round_trips(store, {})
    assert trip.account == "news" and trip.ret < 0 and not trip.still_open


def test_no_key_or_failed_model_skips_the_night():
    store = _store()
    assert run_night(store, None, now=NOW).skipped == "no GROQ_API_KEY set"

    def down(news):
        raise ConnectionError("rate limited")

    r = run_night(store, down, now=NOW)
    assert r.skipped.startswith("scoring failed") and r.opened == []


def test_groq_request_sets_its_own_user_agent(monkeypatch):
    # Python's default "Python-urllib" agent gets HTTP 403 (Cloudflare 1010) from Groq.
    import io
    import json as _json

    from trade_away import newsbot

    seen = {}

    def fake_urlopen(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        reply = {"choices": [{"message": {"content": _json.dumps({"AAA": {"score": 1, "reason": "x"}})}}]}
        return io.BytesIO(_json.dumps(reply).encode())

    monkeypatch.setattr(newsbot.urllib.request, "urlopen", fake_urlopen)
    newsbot.groq_scorer("k")({"AAA": ["headline"]})
    assert seen["ua"] and not seen["ua"].startswith("Python-urllib")
