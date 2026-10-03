"""Rules-only strategies for the Phase 1 baseline.

Each strategy looks at one symbol's daily bars, up to and including the last
closed bar, and returns an entry or exit signal. Orders act on the next bar's
open, so there is no look-ahead. Each one runs as its own bot with its own book
(see engine.BOTS), so they can be compared head to head.

A strategy that ranks symbols against each other (momentum) also has
`prepare(bars_by_symbol)`, which returns a copy that knows the ranking.
"""

from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

import pandas as pd

from .indicators import atr, rsi, sma

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    strategy: str
    price: float           # last close, the reference price for sizing
    stop: float | None     # protective stop for entries
    reason: str


class Strategy(Protocol):
    name: str
    min_bars: int

    def evaluate(self, symbol: str, bars: pd.DataFrame, in_position: bool) -> Signal | None:
        """bars: daily OHLCV indexed by time, oldest first, columns open/high/low/close/volume."""


@dataclass(frozen=True)
class TrendFollowing:
    """Buy when the fast average crosses above the slow one; sell when it crosses back."""

    fast: int = 20
    slow: int = 50
    atr_stop: float = 3.0
    name: str = "trend"

    @property
    def min_bars(self) -> int:
        return self.slow + 2

    def evaluate(self, symbol: str, bars: pd.DataFrame, in_position: bool) -> Signal | None:
        if len(bars) < self.min_bars:
            return None
        close = bars["close"]
        fast, slow = sma(close, self.fast), sma(close, self.slow)
        above_now, above_before = fast.iloc[-1] > slow.iloc[-1], fast.iloc[-2] > slow.iloc[-2]
        price = float(close.iloc[-1])
        if not in_position and above_now and not above_before and price > slow.iloc[-1]:
            stop = price - self.atr_stop * float(atr(bars["high"], bars["low"], close).iloc[-1])
            return Signal(symbol, "buy", self.name, price, stop,
                          f"SMA{self.fast} crossed above SMA{self.slow}; stop {self.atr_stop}x ATR")
        if in_position and not above_now:
            return Signal(symbol, "sell", self.name, price, None, f"SMA{self.fast} back below SMA{self.slow}")
        return None


@dataclass(frozen=True)
class DipBuying:
    """Connors-style RSI(2) mean reversion: buy a sharp dip inside an uptrend, sell the bounce."""

    rsi_period: int = 2
    entry_rsi: float = 10.0
    trend: int = 200
    exit_sma: int = 5
    atr_stop: float = 2.0
    name: str = "dip"

    @property
    def min_bars(self) -> int:
        return self.trend + 1

    def evaluate(self, symbol: str, bars: pd.DataFrame, in_position: bool) -> Signal | None:
        if len(bars) < self.min_bars:
            return None
        close = bars["close"]
        price = float(close.iloc[-1])
        if in_position:
            if price > sma(close, self.exit_sma).iloc[-1]:
                return Signal(symbol, "sell", self.name, price, None, f"closed above SMA{self.exit_sma}")
            return None
        r = float(rsi(close, self.rsi_period).iloc[-1])
        if price > sma(close, self.trend).iloc[-1] and r < self.entry_rsi:
            stop = price - self.atr_stop * float(atr(bars["high"], bars["low"], close).iloc[-1])
            return Signal(symbol, "buy", self.name, price, stop,
                          f"RSI({self.rsi_period})={r:.1f} below {self.entry_rsi} above SMA{self.trend}")
        return None


@dataclass(frozen=True)
class Breakout:
    """Turtle-style channel breakout: buy a new N-day high, sell a new shorter-term low."""

    entry: int = 20
    exit: int = 10
    atr_stop: float = 2.0
    name: str = "breakout"

    @property
    def min_bars(self) -> int:
        return max(self.entry, self.exit, 15) + 1

    def evaluate(self, symbol: str, bars: pd.DataFrame, in_position: bool) -> Signal | None:
        if len(bars) < self.min_bars:
            return None
        close = bars["close"]
        price = float(close.iloc[-1])
        if in_position:
            low = float(bars["low"].iloc[-self.exit - 1:-1].min())
            if price < low:
                return Signal(symbol, "sell", self.name, price, None, f"closed below the {self.exit}-day low {low:.2f}")
            return None
        high = float(bars["high"].iloc[-self.entry - 1:-1].max())
        if price > high:
            stop = price - self.atr_stop * float(atr(bars["high"], bars["low"], close).iloc[-1])
            return Signal(symbol, "buy", self.name, price, stop,
                          f"closed above the {self.entry}-day high {high:.2f}; stop {self.atr_stop}x ATR")
        return None


def momentum_ranks(bars: dict[str, pd.DataFrame], lookback: int, skip: int) -> pd.DataFrame:
    """Rank of each symbol's return from `lookback` to `skip` bars ago, per day (1 = strongest).

    Only uses closes up to each day, so a rank never peeks ahead.
    """
    closes = pd.DataFrame({s: df["close"].set_axis(df.index.normalize()) for s, df in bars.items() if len(df)})
    if closes.empty:
        return closes
    ret = closes.shift(skip) / closes.shift(lookback) - 1
    return ret.rank(axis=1, ascending=False)


@dataclass(frozen=True)
class Momentum:
    """Cross-sectional momentum: hold the strongest performers of the last ~6 months (skipping the
    most recent month), only while they trade above their 200-day average. Sell once a holding
    drops out of the top `keep`, so it doesn't churn on small rank changes."""

    lookback: int = 126
    skip: int = 21
    top: int = 10
    keep: int = 30
    trend: int = 200
    atr_stop: float = 4.0
    name: str = "momentum"
    ranks: pd.DataFrame | None = field(default=None, compare=False, repr=False)

    @property
    def min_bars(self) -> int:
        return max(self.lookback, self.trend) + 1

    def prepare(self, bars: dict[str, pd.DataFrame]) -> "Momentum":
        return replace(self, ranks=momentum_ranks(bars, self.lookback, self.skip))

    def _rank(self, symbol: str, day) -> float | None:
        if self.ranks is None or symbol not in self.ranks or day not in self.ranks.index:
            return None
        r = self.ranks.at[day, symbol]
        return None if pd.isna(r) else float(r)

    def evaluate(self, symbol: str, bars: pd.DataFrame, in_position: bool) -> Signal | None:
        if len(bars) < self.min_bars:
            return None
        close = bars["close"]
        price = float(close.iloc[-1])
        rank = self._rank(symbol, bars.index[-1].normalize())
        above = price > sma(close, self.trend).iloc[-1]
        if in_position:
            if not above:
                return Signal(symbol, "sell", self.name, price, None, f"fell below SMA{self.trend}")
            if rank is not None and rank > self.keep:
                return Signal(symbol, "sell", self.name, price, None, f"momentum rank {rank:.0f}, outside the top {self.keep}")
            return None
        if rank is not None and rank <= self.top and above:
            stop = price - self.atr_stop * float(atr(bars["high"], bars["low"], close).iloc[-1])
            return Signal(symbol, "buy", self.name, price, stop,
                          f"momentum rank {rank:.0f} (top {self.top}), above SMA{self.trend}")
        return None


def prepare(strategy: Strategy, bars: dict[str, pd.DataFrame]) -> Strategy:
    return strategy.prepare(bars) if hasattr(strategy, "prepare") else strategy


DEFAULT_STRATEGIES: tuple[Strategy, ...] = (TrendFollowing(), DipBuying(), Breakout(), Momentum())
