"""Daily-bar backtester for the rules-only strategies, using the same risk layer as live trading.

Timeline for each day t:
  1. orders decided after day t-1's close fill at day t's open, with a cost haircut
  2. protective stops are checked against day t's low (gap below the stop fills at the open)
  3. positions are marked at day t's close
  4. strategies look at bars up to t and queue orders for day t+1's open

Backtests are for rejecting bad strategies quickly, never for approving them.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .risk import AccountState, OrderRequest, Position, RiskLimits, RiskManager
from .strategies import DEFAULT_STRATEGIES, Strategy

HISTORY = 260  # bars handed to a strategy; the longest lookback is 200


@dataclass
class Trade:
    symbol: str
    strategy: str
    entry_date: datetime
    entry: float
    qty: float
    stop: float
    exit_date: datetime | None = None
    exit: float | None = None
    exit_reason: str = ""

    @property
    def pnl(self) -> float:
        return 0.0 if self.exit is None else (self.exit - self.entry) * self.qty


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    benchmark: pd.Series | None = None
    rejections: dict[str, int] = field(default_factory=dict)

    def metrics(self) -> dict[str, float]:
        return compute_metrics(self.equity, self.trades, self.benchmark)


def compute_metrics(equity: pd.Series, trades: list[Trade], benchmark: pd.Series | None = None) -> dict[str, float]:
    returns = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    periods_per_year = len(returns) / years  # ~252 for stocks only, more once crypto adds weekends
    total = equity.iloc[-1] / equity.iloc[0] - 1
    closed = [t for t in trades if t.exit is not None]
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [-t.pnl for t in closed if t.pnl <= 0]
    out = {
        "total_return": total,
        "cagr": (1 + total) ** (1 / years) - 1 if total > -1 else -1.0,
        "sharpe": float(returns.mean() / returns.std() * math.sqrt(periods_per_year)) if returns.std() > 0 else 0.0,
        "max_drawdown": float((1 - equity / equity.cummax()).max()),
        "trades": len(closed),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "profit_factor": sum(wins) / sum(losses) if losses and sum(losses) > 0 else float("inf") if wins else 0.0,
    }
    if benchmark is not None and len(benchmark) > 1:
        out["benchmark_return"] = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1)
    return out


def run_backtest(
    bars: dict[str, pd.DataFrame],
    asset_classes: dict[str, str],
    strategies: tuple[Strategy, ...] = DEFAULT_STRATEGIES,
    limits: RiskLimits | None = None,
    starting_cash: float = 100_000,
    cost: float = 0.001,
    benchmark_symbol: str | None = "SPY",
) -> BacktestResult:
    """bars: symbol -> daily OHLCV DataFrame indexed by UTC timestamp, oldest first."""
    risk = RiskManager(limits)
    by_strategy = {s.name: s for s in strategies}
    # Stock and crypto daily bars are stamped at different hours (exchange midnight vs. UTC offsets),
    # so put every bar on its UTC calendar day; otherwise each day splits into two "dates".
    bars = {s: df.set_axis(df.index.normalize()) for s, df in bars.items()}
    dates = sorted(set().union(*(df.index for df in bars.values())))
    cash = starting_cash
    open_trades: dict[str, Trade] = {}
    closed: list[Trade] = []
    pending: list[tuple[str, str, float, str, float | None]] = []  # (side, symbol, qty, strategy, stop)
    last_close: dict[str, float] = {}
    equity_curve: dict[datetime, float] = {}
    peak = day_start = starting_cash
    rejections: dict[str, int] = {}

    def close_trade(trade: Trade, date: datetime, price: float, why: str) -> None:
        nonlocal cash
        fill = price * (1 - cost)
        cash += fill * trade.qty
        trade.exit_date, trade.exit, trade.exit_reason = date, fill, why
        closed.append(open_trades.pop(trade.symbol))

    for date in dates:
        today = {s: df.loc[date] for s, df in bars.items() if date in df.index}

        # 1. fill earlier decisions at the open of the symbol's next bar
        waiting = []
        for side, symbol, qty, strat, stop in pending:
            if symbol not in today:
                waiting.append((side, symbol, qty, strat, stop))  # market closed today (weekend, holiday)
                continue
            open_px = float(today[symbol]["open"])
            if side == "sell" and symbol in open_trades:
                close_trade(open_trades[symbol], date, open_px, "strategy exit")
            elif side == "buy" and symbol not in open_trades:
                fill = open_px * (1 + cost)
                qty = min(qty, cash / fill) if asset_classes[symbol] == "crypto" else min(qty, math.floor(cash / fill))
                if qty > 0 and stop is not None and stop < fill:
                    cash -= fill * qty
                    open_trades[symbol] = Trade(symbol, strat, date, fill, qty, stop)
        pending = waiting

        # 2. protective stops
        for symbol, trade in list(open_trades.items()):
            if symbol in today and float(today[symbol]["low"]) <= trade.stop:
                close_trade(trade, date, min(float(today[symbol]["open"]), trade.stop), "stop")

        # 3. mark to market
        for symbol, row in today.items():
            last_close[symbol] = float(row["close"])
        equity = cash + sum(t.qty * last_close[s] for s, t in open_trades.items())
        equity_curve[date] = equity
        peak = max(peak, equity)

        # 4. decide tomorrow's orders
        positions = {s: Position(s, asset_classes[s], t.qty, t.qty * last_close[s]) for s, t in open_trades.items()}
        state = AccountState(equity, cash, day_start, peak, positions)
        queued = {p[1] for p in pending}
        for symbol, trade in open_trades.items():
            if symbol in today and symbol not in queued:
                signal = by_strategy[trade.strategy].evaluate(symbol, bars[symbol].loc[:date].iloc[-HISTORY:], in_position=True)
                if signal and signal.side == "sell":
                    pending.append(("sell", symbol, trade.qty, trade.strategy, None))
        for symbol in today:
            if symbol in open_trades or symbol in queued:
                continue
            history = bars[symbol].loc[:date].iloc[-HISTORY:]
            for strat in strategies:
                signal = strat.evaluate(symbol, history, in_position=False)
                if not signal or signal.side != "buy":
                    continue
                order = OrderRequest(symbol, asset_classes[symbol], "buy", signal.price, signal.stop,
                                     date, strat.name, signal.reason)
                decision = risk.check(order, state, date)
                if decision.approved:
                    pending.append(("buy", symbol, decision.qty, strat.name, signal.stop))
                    state = state.with_position(Position(symbol, order.asset_class, decision.qty,
                                                         decision.qty * signal.price))
                    break
                key = decision.reason.split(":")[0]
                rejections[key] = rejections.get(key, 0) + 1
        day_start = equity

    equity_series = pd.Series(equity_curve).sort_index()
    benchmark = None
    if benchmark_symbol and benchmark_symbol in bars:
        b = bars[benchmark_symbol]["close"]
        benchmark = (b / b.iloc[0] * starting_cash).reindex(equity_series.index).ffill()
    return BacktestResult(equity_series, closed + list(open_trades.values()), benchmark, rejections)


def frame_from_rows(rows: list) -> pd.DataFrame:
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df.index = pd.to_datetime(df.pop("ts"), utc=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)

