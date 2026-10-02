"""Technical indicators on pandas Series. Plain pandas, no extra dependency."""

import pandas as pd


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI. Returns 0-100; NaN until there are `n` changes."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    # No losses at all: RSI is 100 (rs is inf, which the formula already maps to 100).
    return out.where(~((loss == 0) & (gain == 0)), 50.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    prev = close.shift()
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
