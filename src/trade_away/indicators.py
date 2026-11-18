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


def return_correlation(a: pd.Series, b: pd.Series, n: int = 60) -> float | None:
    """Correlation of daily returns over the last `n` shared days; None if there is too little overlap."""
    joined = pd.concat([a.pct_change(), b.pct_change()], axis=1, join="inner").dropna().tail(n)
    if len(joined) < n // 2:
        return None
    value = joined.iloc[:, 0].corr(joined.iloc[:, 1])
    return None if pd.isna(value) else float(value)
