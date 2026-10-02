"""Nightly universe screen: every tradable US stock and ETF, narrowed to the liquid ones.

The agent may trade anything that passes this screen. The screen is a safety
filter (no penny stocks, nothing too thin to fill), not a watchlist.
"""

import logging
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Settings
from .db import BarRow, Store

log = logging.getLogger(__name__)

MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "NYSEARCA", "AMEX", "BATS"}
LOOKBACK_DAYS = 20
CHUNK = 200


def screen(daily: pd.DataFrame, min_price: float, min_dollar_volume: float,
           lookback: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Rank symbols by average daily dollar volume over the last `lookback` bars.

    `daily` needs columns: symbol, ts, close, volume. Symbols with fewer than
    `lookback` bars are dropped (too new to judge liquidity).
    Returns columns: symbol, close, avg_dollar_volume, sorted best first.
    """
    if daily.empty:
        return pd.DataFrame(columns=["symbol", "close", "avg_dollar_volume"])
    recent = daily.sort_values("ts").groupby("symbol").tail(lookback)
    stats = recent.assign(dollar_volume=recent["close"] * recent["volume"]).groupby("symbol").agg(
        bars=("close", "size"),
        close=("close", "last"),
        avg_dollar_volume=("dollar_volume", "mean"),
    )
    passed = stats[
        (stats["bars"] >= lookback)
        & (stats["close"] >= min_price)
        & (stats["avg_dollar_volume"] >= min_dollar_volume)
    ]
    return (
        passed.sort_values("avg_dollar_volume", ascending=False)
        .reset_index()[["symbol", "close", "avg_dollar_volume"]]
    )


def fetch_tradable_symbols(settings: Settings) -> list[str]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    client = TradingClient(settings.api_key, settings.secret_key, paper=True)
    assets = client.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    return sorted(
        a.symbol for a in assets
        if a.tradable and str(getattr(a.exchange, "value", a.exchange)) in MAJOR_EXCHANGES
    )


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_daily_bars(settings: Settings, symbols: list[str], days: int) -> pd.DataFrame:
    """Daily bars from the full-market SIP feed.

    The free plan allows SIP history as long as it ends at least 15 minutes ago.
    """
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(settings.api_key, settings.secret_key)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=days)
    frames = []
    for chunk in _chunks(symbols, CHUNK):
        req = StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end,
            feed=DataFeed.SIP, adjustment=Adjustment.ALL,
        )
        df = client.get_stock_bars(req).df
        if not df.empty:
            frames.append(df.reset_index())
        time.sleep(0.35)  # stay well under 200 calls/min
    if not frames:
        return pd.DataFrame(columns=["symbol", "ts", "open", "high", "low", "close", "volume", "vwap"])
    return pd.concat(frames).rename(columns={"timestamp": "ts"})


def daily_frame_to_rows(df: pd.DataFrame, asset_class: str, source: str) -> list[BarRow]:
    return [
        BarRow(r.symbol, asset_class, "1Day", r.ts.to_pydatetime(), r.open, r.high, r.low,
               r.close, r.volume, getattr(r, "vwap", None), source)
        for r in df.itertuples(index=False)
    ]


def run_screen(settings: Settings, store: Store) -> pd.DataFrame:
    symbols = fetch_tradable_symbols(settings)
    log.info("screening %d tradable symbols", len(symbols))
    # ~45 calendar days covers 20+ trading days
    daily = fetch_daily_bars(settings, symbols, days=45)
    store.upsert_bars(daily_frame_to_rows(daily, "stock", "sip"))
    ranked = screen(daily, settings.min_price, settings.min_dollar_volume)
    as_of = datetime.now(timezone.utc).date().isoformat()
    store.replace_universe(as_of, ranked[["symbol", "close", "avg_dollar_volume"]].itertuples(index=False))
    log.info("universe for %s: %d liquid symbols", as_of, len(ranked))
    return ranked
