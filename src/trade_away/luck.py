"""Luck or skill: does each bot pick better stocks than random picks on the same days?

Every round trip a bot made (buy, then sell; open ones are marked at the last
close) is replayed thousands of times with a random symbol from the universe,
held over exactly the same dates. If the bot's average trade beats nearly all
of those random books, its picks look like skill; if it sits in the middle, its
results so far are what chance alone would give. Same dates means the market's
overall rise or fall cancels out: only the choice of stock is tested.
"""

import random
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from .backtest import frame_from_rows
from .db import Store


@dataclass(frozen=True)
class RoundTrip:
    account: str
    symbol: str
    entry_day: pd.Timestamp
    exit_day: pd.Timestamp
    ret: float          # actual return from fill prices (or last close while open)
    still_open: bool


@dataclass(frozen=True)
class LuckReport:
    account: str
    trades: int
    mean_return: float      # the bot's average trade
    random_mean: float      # average of the random books
    spy_mean: float | None  # SPY held over the same dates
    percentile: float       # share of random books the bot beat, 0..1

    @property
    def verdict(self) -> str:
        if self.trades < 30:
            return "too few trades to judge"
        if self.percentile >= 0.95:
            return "looks like skill"
        if self.percentile <= 0.05:
            return "worse than random picks"
        return "can't tell from luck yet"


def _close_on(df: pd.DataFrame, day: pd.Timestamp) -> float | None:
    """Close on `day`, or the last close before it."""
    upto = df.loc[:day]
    return None if upto.empty else float(upto["close"].iloc[-1])


def round_trips(store: Store, closes: dict[str, pd.DataFrame]) -> list[RoundTrip]:
    fills = store.query("SELECT account, symbol, side, qty, price, ts FROM book_fills ORDER BY ts")
    entries: dict[tuple[str, str], tuple[pd.Timestamp, float]] = {}
    out = []
    for f in fills:
        key, day = (f["account"], f["symbol"]), pd.Timestamp(f["ts"]).normalize()
        if f["side"] == "buy":
            entries[key] = (day, float(f["price"]))
        elif key in entries:
            start, price = entries.pop(key)
            out.append(RoundTrip(*key, start, day, float(f["price"]) / price - 1, False))
    for (account, symbol), (start, price) in entries.items():
        df = closes.get(symbol)
        if df is not None and not df.empty:
            out.append(RoundTrip(account, symbol, start, df.index[-1].normalize(),
                                 float(df["close"].iloc[-1]) / price - 1, True))
    return out


def _held_return(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    a, b = _close_on(df, start), _close_on(df, end)
    return None if not a or b is None else b / a - 1


def evaluate(trips: list[RoundTrip], closes: dict[str, pd.DataFrame], benchmark: str = "SPY",
             sims: int = 2000, seed: int = 7) -> list[LuckReport]:
    rng = random.Random(seed)
    pool = {s: df for s, df in closes.items() if s != benchmark and not df.empty}
    by_bot: dict[str, list[RoundTrip]] = defaultdict(list)
    for t in trips:
        by_bot[t.account].append(t)

    reports = []
    for account, mine in sorted(by_bot.items()):
        # For each trade, the returns every universe symbol would have made over the same dates.
        options = []
        for t in mine:
            rets = [r for df in pool.values() if df.index[0] <= t.entry_day
                    and (r := _held_return(df, t.entry_day, t.exit_day)) is not None]
            if rets:
                options.append(rets)
        if not options:
            continue
        bot_mean = sum(t.ret for t in mine) / len(mine)
        books = [sum(rng.choice(o) for o in options) / len(options) for _ in range(sims)]
        spy = closes.get(benchmark)
        spy_rets = [r for t in mine if spy is not None and (r := _held_return(spy, t.entry_day, t.exit_day)) is not None]
        reports.append(LuckReport(account, len(mine), bot_mean, sum(books) / sims,
                                  sum(spy_rets) / len(spy_rets) if spy_rets else None,
                                  sum(b < bot_mean for b in books) / sims))
    return reports


def run_luck(store: Store, top_n: int = 100, sims: int = 2000) -> list[LuckReport]:
    symbols = set(store.latest_universe(limit=top_n)) | {"SPY"}
    traded = {r["symbol"] for r in store.query("SELECT DISTINCT symbol FROM book_fills")}
    closes = {s: frame_from_rows(store.daily_bars(s, limit=400)) for s in symbols | traded}
    pool = {s: df for s, df in closes.items() if s in symbols}
    return evaluate(round_trips(store, closes), pool, sims=sims)
