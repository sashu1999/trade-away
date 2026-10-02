"""Hard risk limits. Plain code that every order passes through; nothing upstream can override it.

Limits follow the system design doc's "Risk guardrails" table. Sells that close
an existing long position are always allowed, since they only reduce risk.
"""

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.01        # equity lost if the stop is hit
    max_position_pct: float = 0.10      # of equity, per symbol
    max_stock_exposure: float = 0.80    # of equity
    max_crypto_exposure: float = 0.30   # of equity
    max_positions: int = 10
    daily_loss_limit: float = 0.03      # no new entries after losing this much today
    max_drawdown: float = 0.15          # from peak equity: pause and wait for a human
    max_data_age: timedelta = timedelta(days=4)  # daily strategies; a weekend plus a holiday


@dataclass(frozen=True)
class Position:
    symbol: str
    asset_class: str
    qty: float
    market_value: float


@dataclass(frozen=True)
class AccountState:
    equity: float
    cash: float
    day_start_equity: float
    peak_equity: float
    positions: dict[str, Position] = field(default_factory=dict)

    def exposure(self, asset_class: str) -> float:
        return sum(p.market_value for p in self.positions.values() if p.asset_class == asset_class)

    def with_position(self, pos: Position) -> "AccountState":
        """State after a buy fills at market value, used to check several orders in one run."""
        positions = {**self.positions, pos.symbol: pos}
        return replace(self, cash=self.cash - pos.market_value, positions=positions)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    asset_class: str
    side: Literal["buy", "sell"]
    price: float
    stop: float | None
    data_ts: datetime
    strategy: str
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: float
    reason: str
    resized: bool = False


def _reject(reason: str) -> RiskDecision:
    return RiskDecision(False, 0.0, reason)


class RiskManager:
    def __init__(self, limits: RiskLimits | None = None, stock_universe: set[str] | None = None,
                 crypto_universe: set[str] | None = None):
        self.limits = limits or RiskLimits()
        self.stock_universe = stock_universe
        self.crypto_universe = crypto_universe

    def halted(self, state: AccountState) -> str | None:
        """Why new entries are blocked right now, or None."""
        lim = self.limits
        if state.peak_equity > 0 and 1 - state.equity / state.peak_equity >= lim.max_drawdown:
            return f"drawdown {1 - state.equity / state.peak_equity:.1%} hit the {lim.max_drawdown:.0%} limit; paused for review"
        if state.day_start_equity > 0 and 1 - state.equity / state.day_start_equity >= lim.daily_loss_limit:
            return f"down {1 - state.equity / state.day_start_equity:.1%} today; daily loss limit {lim.daily_loss_limit:.0%}"
        return None

    def check(self, order: OrderRequest, state: AccountState, now: datetime) -> RiskDecision:
        if order.side == "sell":
            held = state.positions.get(order.symbol)
            if held is None or held.qty <= 0:
                return _reject("no long position to sell (shorting not allowed)")
            return RiskDecision(True, held.qty, "closing position")
        return self._check_buy(order, state, now)

    def _check_buy(self, order: OrderRequest, state: AccountState, now: datetime) -> RiskDecision:
        lim = self.limits
        if reason := self.halted(state):
            return _reject(reason)
        if now - order.data_ts > lim.max_data_age:
            return _reject(f"stale data: last bar {order.data_ts:%Y-%m-%d}")
        if order.stop is None or not 0 < order.stop < order.price:
            return _reject("a stop-loss below the entry price is required")
        universe = self.crypto_universe if order.asset_class == "crypto" else self.stock_universe
        if universe is not None and order.symbol not in universe:
            return _reject("not in the allowed universe")
        if order.symbol in state.positions:
            return _reject("already holding this symbol")
        if len(state.positions) >= lim.max_positions:
            return _reject(f"already at {lim.max_positions} open positions")

        qty = state.equity * lim.risk_per_trade / (order.price - order.stop)
        reasons = []
        cap = lim.max_crypto_exposure if order.asset_class == "crypto" else lim.max_stock_exposure
        for limit_value, why in (
            (state.equity * lim.max_position_pct, "position size cap"),
            (state.equity * cap - state.exposure(order.asset_class), f"{order.asset_class} exposure cap"),
            (state.cash, "available cash (no leverage)"),
        ):
            if qty * order.price > limit_value:
                qty = max(limit_value, 0) / order.price
                reasons.append(why)

        qty = math.floor(qty) if order.asset_class == "stock" else math.floor(qty * 1e6) / 1e6
        if qty <= 0:
            return _reject("no room: " + ", ".join(reasons or ["size rounds to zero"]))
        if reasons:
            return RiskDecision(True, qty, "resized to fit " + ", ".join(reasons), resized=True)
        return RiskDecision(True, qty, f"risking {lim.risk_per_trade:.0%} of equity to the stop")
