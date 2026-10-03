"""Paper order execution on Alpaca, behind a small interface the engine and tests share."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .config import Settings
from .risk import AccountState, Position

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fill:
    order_id: str
    parent_id: str | None  # set for a stop-loss leg of an entry order
    symbol: str
    side: str
    qty: float
    price: float
    ts: datetime


class Broker(Protocol):
    def account(self, peak_equity: float) -> AccountState: ...
    def buy(self, symbol: str, asset_class: str, qty: float, stop: float) -> str: ...
    def close(self, symbol: str, asset_class: str) -> str: ...
    def open_order_symbols(self) -> set[str]: ...
    def order_fills(self, order_id: str) -> list[Fill]:
        """What has filled so far for an order and its legs (the attached stop-loss)."""


class AlpacaPaperBroker:
    """Alpaca paper account. Refuses to run against the live endpoint."""

    def __init__(self, settings: Settings):
        from alpaca.trading.client import TradingClient

        self.client = TradingClient(settings.api_key, settings.secret_key, paper=True)
        # Alpaca reports crypto positions as "BTCUSD"; orders and our data use "BTC/USD".
        self._crypto_names = {s.replace("/", ""): s for s in settings.crypto_symbols}

    def account(self, peak_equity: float) -> AccountState:
        acct = self.client.get_account()
        equity = float(acct.equity)
        positions = {}
        for p in self.client.get_all_positions():
            crypto = "crypto" in str(getattr(p.asset_class, "value", p.asset_class)).lower()
            symbol = self._crypto_names.get(p.symbol, p.symbol) if crypto else p.symbol
            positions[symbol] = Position(symbol, "crypto" if crypto else "stock", float(p.qty), float(p.market_value))
        return AccountState(
            equity=equity,
            cash=float(acct.cash),
            day_start_equity=float(acct.last_equity),
            peak_equity=max(peak_equity, equity),
            positions=positions,
        )

    def buy(self, symbol: str, asset_class: str, qty: float, stop: float) -> str:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest

        client_id = f"ta-{uuid.uuid4().hex[:16]}"
        if asset_class == "stock":
            # The stop rides with the entry (one-triggers-other) and stays working until filled or cancelled.
            req = MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                order_class=OrderClass.OTO, stop_loss=StopLossRequest(stop_price=round(stop, 2)),
                client_order_id=client_id,
            )
        else:
            # Alpaca does not attach stop legs to crypto orders; `trade-away stops` enforces them.
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY,
                                     time_in_force=TimeInForce.GTC, client_order_id=client_id)
        order = self.client.submit_order(req)
        log.info("submitted buy %s %s qty=%s stop=%.2f -> %s", asset_class, symbol, qty, stop, order.id)
        return str(order.id)

    def open_order_symbols(self) -> set[str]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return {self._crypto_names.get(o.symbol, o.symbol) for o in orders}

    def close(self, symbol: str, asset_class: str) -> str:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        # Cancel the working stop first, or it would hold the shares and block the sale.
        for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            self.client.cancel_order_by_id(o.id)
        order = self.client.close_position(symbol.replace("/", "") if asset_class == "crypto" else symbol)
        log.info("closing %s -> %s", symbol, order.id)
        return str(order.id)

    def order_fills(self, order_id: str) -> list[Fill]:
        from alpaca.trading.requests import GetOrderByIdRequest

        order = self.client.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True))
        fills = []
        for o, parent in [(order, None)] + [(leg, str(order.id)) for leg in order.legs or []]:
            if o.filled_qty and float(o.filled_qty) > 0 and o.filled_avg_price:
                side = str(getattr(o.side, "value", o.side)).lower()
                fills.append(Fill(str(o.id), parent, self._crypto_names.get(o.symbol.replace("/", ""), o.symbol),
                                  side, float(o.filled_qty), float(o.filled_avg_price), o.filled_at))
        return fills


class FakeBroker:
    """In-memory broker for tests and dry runs: fills instantly at the given prices."""

    def __init__(self, cash: float = 100_000, prices: dict[str, float] | None = None, last_equity: float | None = None):
        self.cash = cash
        self.prices = prices or {}
        self.positions: dict[str, Position] = {}
        self.last_equity = last_equity
        self.orders: list[tuple[str, str, float, float | None]] = []
        self.pending: set[str] = set()  # symbols with unfilled orders
        self.fills: dict[str, list[Fill]] = {}
        self.entry_ids: dict[str, str] = {}

    def _fill(self, side: str, symbol: str, qty: float, parent: str | None = None) -> str:
        order_id = f"fake-{len(self.orders)}"
        fill = Fill(order_id, parent, symbol, side, qty, self.prices[symbol], datetime.now(timezone.utc))
        self.fills.setdefault(parent or order_id, []).append(fill)
        return order_id

    def account(self, peak_equity: float) -> AccountState:
        positions = {s: Position(s, p.asset_class, p.qty, p.qty * self.prices[s]) for s, p in self.positions.items()}
        equity = self.cash + sum(p.market_value for p in positions.values())
        return AccountState(equity, self.cash, self.last_equity or equity, max(peak_equity, equity), positions)

    def buy(self, symbol: str, asset_class: str, qty: float, stop: float) -> str:
        self.cash -= qty * self.prices[symbol]
        self.positions[symbol] = Position(symbol, asset_class, qty, qty * self.prices[symbol])
        self.orders.append(("buy", symbol, qty, stop))
        self.entry_ids[symbol] = self._fill("buy", symbol, qty)
        return self.entry_ids[symbol]

    def open_order_symbols(self) -> set[str]:
        return set(self.pending)

    def close(self, symbol: str, asset_class: str) -> str:
        pos = self.positions.pop(symbol)
        self.cash += pos.qty * self.prices[symbol]
        self.orders.append(("sell", symbol, pos.qty, None))
        return self._fill("sell", symbol, pos.qty)

    def stop_out(self, symbol: str, price: float) -> None:
        """Simulate the broker-side stop leg filling."""
        self.prices[symbol] = price
        pos = self.positions.pop(symbol)
        self.cash += pos.qty * price
        self.orders.append(("stop", symbol, pos.qty, None))
        self._fill("sell", symbol, pos.qty, parent=self.entry_ids[symbol])

    def order_fills(self, order_id: str) -> list[Fill]:
        return list(self.fills.get(order_id, []))
