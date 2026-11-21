import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trade_away.db import Store
from trade_away.health import crypto_gaps
from trade_away.stream import Recorder

T0 = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)


def fake_bar(symbol, ts, close):
    return SimpleNamespace(symbol=symbol, timestamp=ts, open=close, high=close, low=close,
                           close=close, volume=1.5, vwap=close)


def test_recorder_stores_bars_and_latest_price():
    store = Store(":memory:")
    rec = Recorder(store, "crypto", "crypto_us")
    asyncio.run(rec.on_bar(fake_bar("BTC/USD", T0, 60_000)))
    asyncio.run(rec.on_trade(SimpleNamespace(symbol="BTC/USD", timestamp=T0 + timedelta(seconds=5), price=60_010)))
    assert store.query("SELECT close FROM bars")[0]["close"] == 60_000
    assert store.query("SELECT price FROM latest_prices")[0]["price"] == 60_010


def test_crypto_gaps_finds_holes_and_dead_streams():
    store = Store(":memory:")
    rec = Recorder(store, "crypto", "crypto_us")
    now = T0 + timedelta(hours=1)
    minutes = [m for m in range(60) if not 20 <= m < 30]  # 10-minute outage
    for m in minutes:
        asyncio.run(rec.on_bar(fake_bar("BTC/USD", T0 + timedelta(minutes=m), 1)))
    gaps = crypto_gaps(store, ["BTC/USD", "ETH/USD"], hours=1, now=now)
    btc = [g for g in gaps if g.symbol == "BTC/USD"]
    eth = [g for g in gaps if g.symbol == "ETH/USD"]
    assert len(btc) == 1 and btc[0].minutes == 11
    assert len(eth) == 1 and eth[0].minutes == 60  # never streamed


def test_no_gaps_when_every_minute_present():
    store = Store(":memory:")
    rec = Recorder(store, "crypto", "crypto_us")
    for m in range(61):
        asyncio.run(rec.on_bar(fake_bar("BTC/USD", T0 + timedelta(minutes=m), 1)))
    assert crypto_gaps(store, ["BTC/USD"], hours=1, now=T0 + timedelta(hours=1)) == []


def test_healthcheck_ping_only_when_configured(monkeypatch):
    from trade_away import cli

    calls = []
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda url, timeout: calls.append(url))
    monkeypatch.delenv("HEALTHCHECK_RUN_URL", raising=False)
    cli.ping("run")
    assert calls == []
    monkeypatch.setenv("HEALTHCHECK_RUN_URL", "https://hc-ping.com/abc/")
    cli.ping("run", "/fail")
    assert calls == ["https://hc-ping.com/abc/fail"]


def test_poll_crypto_prices_updates_only_held_crypto():
    from datetime import datetime, timezone

    from trade_away.config import Settings
    from trade_away.db import Store
    from trade_away.stream import poll_crypto_prices

    store = Store(":memory:")
    store.execute("INSERT INTO open_trades VALUES ('trend','BTC/USD','trend','2026-10-01',60000,0.1,55000)")
    store.execute("INSERT INTO open_trades VALUES ('trend','AAPL','trend','2026-10-01',100,1,90)")
    asked = []
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)

    def fetch(symbols):
        asked.append(symbols)
        return {s: (now, 54_000.0) for s in symbols}

    assert poll_crypto_prices(Settings(), store, fetch) == ["BTC/USD"]
    assert asked == [["BTC/USD"]]
    assert store.query("SELECT price FROM latest_prices WHERE symbol = 'BTC/USD'")[0]["price"] == 54_000.0

    def broken(symbols):
        raise ConnectionError("down")

    assert poll_crypto_prices(Settings(), store, broken) == []
