"""The trading loop: competing strategy bots sharing one Alpaca paper account.

Each bot runs one strategy on its own virtual book (BOOK_EQUITY to start), so the
strategies can be compared head to head. A book's cash comes from its own fills
in `book_fills`; its positions are its rows in `open_trades`, valued at the
broker's prices. Every order still goes through the hard risk layer, checked
against that bot's book. A symbol belongs to one bot at a time: the others skip
it while it's held, since the broker account can only hold one position per symbol.

`run_daily` runs once after the US close (cron). For each bot it handles exits
first, then new entries, and journals every idea whether or not the risk layer
approved it. `check_stops` runs every few minutes to enforce crypto stops, which
Alpaca cannot attach to crypto orders.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .backtest import frame_from_rows
from .config import Settings
from .db import Store, to_iso
from .execution import Broker
from .risk import AccountState, OrderRequest, Position, RiskDecision, RiskLimits, RiskManager
from .strategies import Breakout, DipBuying, Momentum, Strategy, TrendFollowing, prepare

log = logging.getLogger(__name__)
BOOK_EQUITY = 25_000.0


@dataclass(frozen=True)
class Bot:
    strategy: Strategy
    stocks: bool = True
    crypto: bool = True
    start: float = BOOK_EQUITY

    @property
    def name(self) -> str:
        return self.strategy.name


BOTS: tuple[Bot, ...] = (
    Bot(TrendFollowing()),
    Bot(DipBuying()),
    Bot(Breakout()),
    Bot(Momentum(), crypto=False),  # ranks stocks against each other
)


@dataclass(frozen=True)
class Action:
    side: str
    symbol: str
    strategy: str
    decision: RiskDecision
    reason: str


def _journal(store: Store, now: datetime, account: str, strategy: str, symbol: str, side: str,
             price: float | None, stop: float | None, decision: RiskDecision, reason: str,
             order_id: str | None, dry_run: bool) -> None:
    store.execute(
        "INSERT INTO decisions (ts, account, strategy, symbol, side, price, stop, qty, approved, "
        "risk_note, reason, order_id, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (to_iso(now), account, strategy, symbol, side, price, stop, decision.qty,
         int(decision.approved), decision.reason, reason, order_id, int(dry_run)),
    )


def _record(store: Store, order_id: str, account: str, symbol: str, side: str, qty: float, price: float,
            now: datetime) -> None:
    """Book an order at its estimated price; sync_fills corrects it once it fills."""
    store.execute("INSERT OR IGNORE INTO book_fills VALUES (?,?,?,?,?,?,?,0)",
                  (order_id, account, symbol, side, qty, price, to_iso(now)))


def _migrate_rules_account(store: Store, now: datetime) -> None:
    """Hand any Phase 1 'rules' positions to the bot named after their strategy."""
    for row in store.query("SELECT * FROM open_trades WHERE account = 'rules'"):
        store.execute("UPDATE open_trades SET account = ? WHERE account = 'rules' AND symbol = ?",
                      (row["strategy"], row["symbol"]))
        store.execute("INSERT OR IGNORE INTO book_fills VALUES (?,?,?,?,?,?,?,1)",
                      (f"legacy-{row['symbol']}", row["strategy"], row["symbol"], "buy", row["qty"],
                       row["entry"], row["opened_ts"]))


def sync_fills(store: Store, broker: Broker, now: datetime | None = None) -> None:
    """Replace estimated prices with real fills, and book stop-loss legs that filled at the broker."""
    now = now or datetime.now(timezone.utc)
    for row in store.query("SELECT order_id FROM book_fills WHERE filled = 0"):
        for f in broker.order_fills(row["order_id"]):
            if f.order_id == row["order_id"]:
                store.execute("UPDATE book_fills SET qty = ?, price = ?, ts = ?, filled = 1 WHERE order_id = ?",
                              (f.qty, f.price, to_iso(f.ts or now), f.order_id))
    # A stock entry's stop rides with it at Alpaca; when it fills, the position just disappears.
    state = broker.account(0)
    working = broker.open_order_symbols()
    for row in store.query("SELECT account, symbol, strategy FROM open_trades"):
        if row["symbol"] in state.positions or row["symbol"] in working:
            continue
        entry = store.query("SELECT order_id, price FROM book_fills WHERE account = ? AND symbol = ? AND side = 'buy' "
                            "ORDER BY ts DESC LIMIT 1", (row["account"], row["symbol"]))
        legs = [] if not entry else [f for f in broker.order_fills(entry[0]["order_id"]) if f.parent_id]
        for f in legs:
            store.execute("INSERT OR IGNORE INTO book_fills VALUES (?,?,?,?,?,?,?,1)",
                          (f.order_id, row["account"], f.symbol, f.side, f.qty, f.price, to_iso(f.ts or now)))
        store.execute("DELETE FROM open_trades WHERE account = ? AND symbol = ?", (row["account"], row["symbol"]))
        price = legs[0].price if legs else None
        _journal(store, now, row["account"], row["strategy"], row["symbol"], "sell", price, None,
                 RiskDecision(True, 0, "position gone at broker"), "stop-loss filled",
                 legs[0].order_id if legs else None, False)
        if not legs:
            log.warning("%s %s left the account but no stop fill was found; book cash may be off",
                        row["account"], row["symbol"])


def book_state(store: Store, broker_state: AccountState, account: str, start: float,
               crypto_symbols: list[str], now: datetime) -> AccountState:
    """A bot's virtual account: its cash from its own fills, its positions at broker prices."""
    flows = store.query("SELECT COALESCE(SUM(CASE side WHEN 'buy' THEN -qty * price ELSE qty * price END), 0) AS net "
                        "FROM book_fills WHERE account = ?", (account,))
    cash = start + float(flows[0]["net"])
    positions = {}
    for row in store.query("SELECT symbol, entry, qty FROM open_trades WHERE account = ?", (account,)):
        held = broker_state.positions.get(row["symbol"])
        asset_class = "crypto" if row["symbol"] in crypto_symbols else "stock"
        positions[row["symbol"]] = held or Position(row["symbol"], asset_class, row["qty"], row["qty"] * row["entry"])
    equity = cash + sum(p.market_value for p in positions.values())
    log_rows = store.query("SELECT MAX(equity) AS peak FROM equity_log WHERE account = ?", (account,))
    before = store.query("SELECT equity FROM equity_log WHERE account = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                         (account, now.date().isoformat()))
    peak = max(float(log_rows[0]["peak"] or 0), start, equity)
    day_start = float(before[0]["equity"]) if before else equity
    return AccountState(equity, cash, day_start, peak, positions)


def log_equity(store: Store, account: str, state: AccountState, now: datetime) -> None:
    store.execute("INSERT OR REPLACE INTO equity_log VALUES (?,?,?,?)", (account, to_iso(now), state.equity, state.cash))


def run_daily(settings: Settings, store: Store, broker: Broker, *, bots: tuple[Bot, ...] = BOTS,
              limits: RiskLimits | None = None, top_n: int = 100, crypto_only: bool = False,
              dry_run: bool = False, now: datetime | None = None) -> list[Action]:
    now = now or datetime.now(timezone.utc)
    universe = store.latest_universe(limit=top_n)
    risk = RiskManager(limits, stock_universe=set(universe), crypto_universe=set(settings.crypto_symbols))
    if not dry_run:
        _migrate_rules_account(store, now)
        sync_fills(store, broker, now)
    broker_state = broker.account(0)

    bars = {s: frame_from_rows(store.daily_bars(s)) for s in [*universe, *settings.crypto_symbols]}
    bars = {s: df for s, df in bars.items() if not df.empty}
    owners = {r["symbol"]: r["account"] for r in store.query("SELECT symbol, account FROM open_trades")}
    actions: list[Action] = []

    # Rotate who goes first each day, so no bot always gets first pick of a shared signal.
    order = list(bots)
    shift = now.toordinal() % len(order) if order else 0
    for bot in order[shift:] + order[:shift]:
        strat = prepare(bot.strategy, {s: df for s, df in bars.items() if s not in settings.crypto_symbols})
        state = book_state(store, broker_state, bot.name, bot.start, settings.crypto_symbols, now)
        if not dry_run:
            log_equity(store, bot.name, state, now)

        # Exits first: they free up room and only ever reduce risk.
        for symbol in [s for s, owner in owners.items() if owner == bot.name]:
            df = bars.get(symbol)
            if df is None:
                df = frame_from_rows(store.daily_bars(symbol))  # dropped out of the universe: still manage it
            if df.empty or symbol not in broker_state.positions:
                continue
            signal = strat.evaluate(symbol, df, in_position=True)
            if not signal or signal.side != "sell":
                continue
            pos = state.positions[symbol]
            req = OrderRequest(symbol, pos.asset_class, "sell", signal.price, None, df.index[-1], bot.name, signal.reason)
            decision = risk.check(req, state, now)
            order_id = None
            if decision.approved and not dry_run:
                order_id = broker.close(symbol, pos.asset_class)
                _record(store, order_id, bot.name, symbol, "sell", pos.qty, signal.price, now)
                store.execute("DELETE FROM open_trades WHERE account = ? AND symbol = ?", (bot.name, symbol))
            _journal(store, now, bot.name, bot.name, symbol, "sell", signal.price, None, decision, signal.reason,
                     order_id, dry_run)
            actions.append(Action("sell", symbol, bot.name, decision, signal.reason))

        # Entries: universe in liquidity order, then crypto. A symbol another bot holds is off limits.
        candidates = [] if crypto_only or not bot.stocks else [(s, "stock") for s in universe]
        candidates += [(s, "crypto") for s in settings.crypto_symbols] if bot.crypto else []
        for symbol, asset_class in candidates:
            df = bars.get(symbol)
            if df is None or symbol in owners or symbol in broker_state.positions:
                continue
            signal = strat.evaluate(symbol, df, in_position=False)
            if not signal or signal.side != "buy":
                continue
            req = OrderRequest(symbol, asset_class, "buy", signal.price, signal.stop, df.index[-1], bot.name, signal.reason)
            decision = risk.check(req, state, now)
            order_id = None
            if decision.approved:
                if not dry_run:
                    order_id = broker.buy(symbol, asset_class, decision.qty, signal.stop)
                    _record(store, order_id, bot.name, symbol, "buy", decision.qty, signal.price, now)
                    store.execute("INSERT OR REPLACE INTO open_trades VALUES (?,?,?,?,?,?,?)",
                                  (bot.name, symbol, bot.name, to_iso(now), signal.price, decision.qty, signal.stop))
                state = state.with_position(Position(symbol, asset_class, decision.qty, decision.qty * signal.price))
                owners[symbol] = bot.name
            _journal(store, now, bot.name, bot.name, symbol, "buy", signal.price, signal.stop, decision,
                     signal.reason, order_id, dry_run)
            actions.append(Action("buy", symbol, bot.name, decision, signal.reason))
    return actions


def check_stops(settings: Settings, store: Store, broker: Broker, *, now: datetime | None = None) -> list[str]:
    """Close crypto positions whose latest streamed price is at or below their stop, for every bot."""
    now = now or datetime.now(timezone.utc)
    closed = []
    rows = store.query(
        "SELECT t.account, t.symbol, t.strategy, t.qty, t.stop, p.price FROM open_trades t "
        "JOIN latest_prices p ON p.symbol = t.symbol WHERE p.asset_class = 'crypto'"
    )
    for row in rows:
        if row["price"] > row["stop"]:
            continue
        order_id = broker.close(row["symbol"], "crypto")
        _record(store, order_id, row["account"], row["symbol"], "sell", row["qty"], row["price"], now)
        store.execute("DELETE FROM open_trades WHERE account = ? AND symbol = ?", (row["account"], row["symbol"]))
        _journal(store, now, row["account"], row["strategy"], row["symbol"], "sell", row["price"], row["stop"],
                 RiskDecision(True, 0, "stop-loss"), f"price {row['price']:.2f} at or below stop {row['stop']:.2f}",
                 order_id, False)
        closed.append(row["symbol"])
    return closed


def leaderboard(store: Store, broker: Broker, settings: Settings, bots: tuple[Bot, ...] = BOTS,
                now: datetime | None = None, record: bool = False) -> list[dict]:
    """Current standing of every bot's book."""
    now = now or datetime.now(timezone.utc)
    broker_state = broker.account(0)
    out = []
    for bot in bots:
        state = book_state(store, broker_state, bot.name, bot.start, settings.crypto_symbols, now)
        if record:
            log_equity(store, bot.name, state, now)
        out.append({"bot": bot.name, "start": bot.start, "equity": state.equity, "cash": state.cash,
                    "positions": len(state.positions)})
    return out
