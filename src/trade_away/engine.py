"""The Phase 1 trading loop: rules-only strategies through the risk layer to the paper broker.

`run_daily` runs once after the US close (cron). It handles exits first, then
new entries, and journals every idea whether or not the risk layer approved it.
`check_stops` runs every few minutes to enforce crypto stops, which Alpaca
cannot attach to crypto orders.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .backtest import frame_from_rows
from .config import Settings
from .db import Store, to_iso
from .execution import Broker
from .risk import AccountState, OrderRequest, Position, RiskDecision, RiskLimits, RiskManager
from .strategies import DEFAULT_STRATEGIES, Strategy

log = logging.getLogger(__name__)
ACCOUNT = "rules"


@dataclass(frozen=True)
class Action:
    side: str
    symbol: str
    strategy: str
    decision: RiskDecision
    reason: str


def _peak_equity(store: Store, account: str) -> float:
    row = store.query("SELECT MAX(equity) AS peak FROM equity_log WHERE account = ?", (account,))
    return float(row[0]["peak"] or 0)


def _journal(store: Store, now: datetime, account: str, strategy: str, symbol: str, side: str,
             price: float | None, stop: float | None, decision: RiskDecision, reason: str,
             order_id: str | None, dry_run: bool) -> None:
    store.execute(
        "INSERT INTO decisions (ts, account, strategy, symbol, side, price, stop, qty, approved, "
        "risk_note, reason, order_id, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (to_iso(now), account, strategy, symbol, side, price, stop, decision.qty,
         int(decision.approved), decision.reason, reason, order_id, int(dry_run)),
    )


def _reconcile(store: Store, broker: Broker, state: AccountState, account: str, now: datetime) -> None:
    """Drop open trades the broker no longer holds (a broker-side stop filled).

    An entry still waiting for the open has no position yet but does have an open order, so it stays.
    """
    working = broker.open_order_symbols()
    for row in store.query("SELECT symbol, strategy FROM open_trades WHERE account = ?", (account,)):
        if row["symbol"] not in state.positions and row["symbol"] not in working:
            store.execute("DELETE FROM open_trades WHERE account = ? AND symbol = ?", (account, row["symbol"]))
            _journal(store, now, account, row["strategy"], row["symbol"], "sell", None, None,
                     RiskDecision(True, 0, "position gone at broker"), "stop-loss filled", None, False)


def run_daily(settings: Settings, store: Store, broker: Broker, *,
              strategies: tuple[Strategy, ...] = DEFAULT_STRATEGIES,
              limits: RiskLimits | None = None, top_n: int = 100,
              crypto_only: bool = False, dry_run: bool = False, now: datetime | None = None,
              account: str = ACCOUNT) -> list[Action]:
    now = now or datetime.now(timezone.utc)
    universe = store.latest_universe(limit=top_n)
    risk = RiskManager(limits, stock_universe=set(universe), crypto_universe=set(settings.crypto_symbols))
    state = broker.account(_peak_equity(store, account))
    if not dry_run:
        store.execute("INSERT OR REPLACE INTO equity_log VALUES (?,?,?,?)",
                      (account, to_iso(now), state.equity, state.cash))
        _reconcile(store, broker, state, account, now)
    by_name = {s.name: s for s in strategies}
    actions: list[Action] = []

    # Exits first: they free up room and only ever reduce risk.
    for row in store.query("SELECT symbol, strategy FROM open_trades WHERE account = ?", (account,)):
        symbol, strat = row["symbol"], by_name.get(row["strategy"])
        bars = frame_from_rows(store.daily_bars(symbol))
        if strat is None or bars.empty or symbol not in state.positions:
            continue
        signal = strat.evaluate(symbol, bars, in_position=True)
        if not signal or signal.side != "sell":
            continue
        pos = state.positions[symbol]
        order = OrderRequest(symbol, pos.asset_class, "sell", signal.price, None, bars.index[-1], strat.name, signal.reason)
        decision = risk.check(order, state, now)
        order_id = None
        if decision.approved and not dry_run:
            order_id = broker.close(symbol, pos.asset_class)
            store.execute("DELETE FROM open_trades WHERE account = ? AND symbol = ?", (account, symbol))
        _journal(store, now, account, strat.name, symbol, "sell", signal.price, None, decision, signal.reason, order_id, dry_run)
        actions.append(Action("sell", symbol, strat.name, decision, signal.reason))

    # Entries: universe in liquidity order, then crypto. First strategy to fire on a symbol wins.
    # Skip symbols we already own or have an unfilled entry for (orders placed after hours fill at the open).
    candidates = [] if crypto_only else [(s, "stock") for s in universe]
    candidates += [(s, "crypto") for s in settings.crypto_symbols]
    pending = {r["symbol"] for r in store.query("SELECT symbol FROM open_trades WHERE account = ?", (account,))}
    for symbol, asset_class in candidates:
        if symbol in state.positions or symbol in pending:
            continue
        bars = frame_from_rows(store.daily_bars(symbol))
        if bars.empty:
            continue
        for strat in strategies:
            signal = strat.evaluate(symbol, bars, in_position=False)
            if not signal or signal.side != "buy":
                continue
            order = OrderRequest(symbol, asset_class, "buy", signal.price, signal.stop, bars.index[-1], strat.name, signal.reason)
            decision = risk.check(order, state, now)
            order_id = None
            if decision.approved:
                if not dry_run:
                    order_id = broker.buy(symbol, asset_class, decision.qty, signal.stop)
                    store.execute("INSERT OR REPLACE INTO open_trades VALUES (?,?,?,?,?,?,?)",
                                  (account, symbol, strat.name, to_iso(now), signal.price, decision.qty, signal.stop))
                state = state.with_position(Position(symbol, asset_class, decision.qty, decision.qty * signal.price))
            _journal(store, now, account, strat.name, symbol, "buy", signal.price, signal.stop, decision,
                     signal.reason, order_id, dry_run)
            actions.append(Action("buy", symbol, strat.name, decision, signal.reason))
            break
    return actions


def check_stops(settings: Settings, store: Store, broker: Broker, *, account: str = ACCOUNT,
                now: datetime | None = None) -> list[str]:
    """Close crypto positions whose latest streamed price is at or below their stop."""
    now = now or datetime.now(timezone.utc)
    closed = []
    rows = store.query(
        "SELECT t.symbol, t.strategy, t.stop, p.price FROM open_trades t "
        "JOIN latest_prices p ON p.symbol = t.symbol WHERE t.account = ? AND p.asset_class = 'crypto'",
        (account,),
    )
    for row in rows:
        if row["price"] > row["stop"]:
            continue
        order_id = broker.close(row["symbol"], "crypto")
        store.execute("DELETE FROM open_trades WHERE account = ? AND symbol = ?", (account, row["symbol"]))
        _journal(store, now, account, row["strategy"], row["symbol"], "sell", row["price"], row["stop"],
                 RiskDecision(True, 0, "stop-loss"), f"price {row['price']:.2f} at or below stop {row['stop']:.2f}",
                 order_id, False)
        closed.append(row["symbol"])
    return closed
