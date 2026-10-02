"""Rules-only strategies for the Phase 1 baseline.

Each strategy looks at one symbol's daily bars, up to and including the last
closed bar, and returns an entry or exit signal. Orders act on the next bar's
open, so there is no look-ahead. These are the two starter strategies from the
Trading Basics research: trend following and short-term dip buying.
"""

from dataclasses import dataclass
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


DEFAULT_STRATEGIES: tuple[Strategy, ...] = (TrendFollowing(), DipBuying())
