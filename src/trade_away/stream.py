"""Live market data: Alpaca stock (IEX) and crypto websockets into SQLite.

Each stream runs in its own thread. Minute bars are stored as they close and
every trade updates the latest price table, which the dashboard and later the
agent read.
"""

import logging
import threading
from typing import Any

from .config import Settings
from .db import BarRow, Store

log = logging.getLogger(__name__)


class Recorder:
    """Websocket handlers that write to the store. Kept separate so tests can call them."""

    def __init__(self, store: Store, asset_class: str, source: str):
        self.store = store
        self.asset_class = asset_class
        self.source = source

    async def on_bar(self, bar: Any) -> None:
        self.store.upsert_bars([BarRow(
            bar.symbol, self.asset_class, "1Min", bar.timestamp, bar.open, bar.high,
            bar.low, bar.close, bar.volume, getattr(bar, "vwap", None), self.source,
        )])
        self.store.set_latest_price(bar.symbol, self.asset_class, bar.timestamp, bar.close)

    async def on_trade(self, trade: Any) -> None:
        self.store.set_latest_price(trade.symbol, self.asset_class, trade.timestamp, trade.price)


def pick_stock_symbols(store: Store, settings: Settings) -> list[str]:
    """Until the agent chooses its own live list, stream the most liquid names from the screen."""
    return store.latest_universe(limit=settings.max_stream_symbols)


def _run(stream: Any, name: str) -> threading.Thread:
    def target() -> None:
        try:
            stream.run()  # blocks; the SDK reconnects on dropped connections
        except Exception:
            log.exception("%s stream stopped", name)

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def start_streams(settings: Settings, store: Store, stock_symbols: list[str]) -> list[threading.Thread]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import CryptoDataStream, StockDataStream

    threads = []
    if stock_symbols:
        stocks = StockDataStream(settings.api_key, settings.secret_key, feed=DataFeed.IEX)
        rec = Recorder(store, "stock", "iex")
        stocks.subscribe_bars(rec.on_bar, *stock_symbols)
        stocks.subscribe_trades(rec.on_trade, *stock_symbols)
        threads.append(_run(stocks, "stocks"))
        log.info("streaming %d stocks: %s", len(stock_symbols), ", ".join(stock_symbols))
    else:
        log.warning("no stock symbols to stream; run `trade-away screen` first")

    if settings.crypto_symbols:
        crypto = CryptoDataStream(settings.api_key, settings.secret_key)
        rec = Recorder(store, "crypto", "crypto_us")
        crypto.subscribe_bars(rec.on_bar, *settings.crypto_symbols)
        crypto.subscribe_trades(rec.on_trade, *settings.crypto_symbols)
        threads.append(_run(crypto, "crypto"))
        log.info("streaming crypto: %s", ", ".join(settings.crypto_symbols))
    return threads
