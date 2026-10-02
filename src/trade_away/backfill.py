"""History backfill: daily bars for stocks and crypto, topped up nightly."""

import logging
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import Store
from .universe import daily_frame_to_rows, fetch_daily_bars

log = logging.getLogger(__name__)


def backfill_stocks(settings: Settings, store: Store, symbols: list[str], years: float = 2) -> int:
    df = fetch_daily_bars(settings, symbols, days=int(365 * years))
    return store.upsert_bars(daily_frame_to_rows(df, "stock", "sip"))


def backfill_crypto(settings: Settings, store: Store, years: float = 2) -> int:
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    # Historical crypto data needs no API key.
    client = CryptoHistoricalDataClient()
    end = datetime.now(timezone.utc)
    req = CryptoBarsRequest(
        symbol_or_symbols=settings.crypto_symbols, timeframe=TimeFrame.Day,
        start=end - timedelta(days=int(365 * years)), end=end,
    )
    df = client.get_crypto_bars(req).df
    if df.empty:
        return 0
    df = df.reset_index().rename(columns={"timestamp": "ts"})
    return store.upsert_bars(daily_frame_to_rows(df, "crypto", "crypto_us"))


def run_backfill(settings: Settings, store: Store, top_n: int = 200, years: float = 2) -> dict[str, int]:
    """Daily history for the top `top_n` names of the latest screen plus all crypto pairs."""
    symbols = store.latest_universe(limit=top_n)
    counts = {"crypto": backfill_crypto(settings, store, years)}
    counts["stock"] = backfill_stocks(settings, store, symbols, years) if symbols else 0
    log.info("backfilled %s daily bars", counts)
    return counts

