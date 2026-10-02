from datetime import datetime, timedelta, timezone

from trade_away.risk import AccountState, OrderRequest, Position, RiskLimits, RiskManager

NOW = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)


def state(equity=100_000, cash=None, day_start=None, peak=None, positions=None):
    return AccountState(equity, equity if cash is None else cash, day_start or equity, peak or equity, positions or {})


def buy(symbol="AAPL", price=100.0, stop=95.0, asset="stock", age=timedelta(hours=1)):
    return OrderRequest(symbol, asset, "buy", price, stop, NOW - age, "trend", "test")


def test_sizes_to_one_percent_risk():
    d = RiskManager().check(buy(price=100, stop=80), state(), NOW)
    # $1,000 risk / $20 per share = 50 shares = $5,000, under every cap
    assert d.approved and d.qty == 50 and not d.resized


def test_position_cap_resizes():
    d = RiskManager().check(buy(price=100, stop=99), state(), NOW)
    # risk sizing says 1,000 shares ($100k); the 10% cap allows $10k
    assert d.approved and d.qty == 100 and d.resized


def test_crypto_exposure_cap_and_fractional_qty():
    held = {"BTC/USD": Position("BTC/USD", "crypto", 0.4, 25_000)}
    d = RiskManager().check(buy("ETH/USD", 3000, 2900, "crypto"), state(positions=held), NOW)
    assert d.approved and abs(d.qty * 3000 - 5_000) < 1  # only $5k left under the 30% cap
    assert d.qty != int(d.qty)


def test_rejections():
    rm = RiskManager(stock_universe={"AAPL"})
    assert not rm.check(buy(stop=None), state(), NOW).approved
    assert not rm.check(buy(stop=101), state(), NOW).approved
    assert not rm.check(buy("PENNY"), state(), NOW).approved
    assert not rm.check(buy(age=timedelta(days=10)), state(), NOW).approved
    assert not rm.check(buy(), state(positions={"AAPL": Position("AAPL", "stock", 1, 100)}), NOW).approved
    full = {f"S{i}": Position(f"S{i}", "stock", 1, 100) for i in range(10)}
    assert not RiskManager().check(buy(), state(positions=full), NOW).approved
    assert not rm.check(buy(), state(cash=0), NOW).approved


def test_daily_loss_and_drawdown_halt_entries_but_not_exits():
    rm = RiskManager()
    down_today = state(equity=96_500, day_start=100_000)
    assert "daily loss" in rm.check(buy(), down_today, NOW).reason
    in_drawdown = state(equity=84_000, peak=100_000)
    assert "drawdown" in rm.check(buy(), in_drawdown, NOW).reason
    held = {"AAPL": Position("AAPL", "stock", 10, 1_000)}
    sell = OrderRequest("AAPL", "stock", "sell", 100, None, NOW, "trend", "exit")
    d = rm.check(sell, state(equity=84_000, peak=100_000, positions=held), NOW)
    assert d.approved and d.qty == 10


def test_no_shorting():
    sell = OrderRequest("AAPL", "stock", "sell", 100, None, NOW, "trend", "exit")
    assert not RiskManager().check(sell, state(), NOW).approved


def test_custom_limits():
    d = RiskManager(RiskLimits(risk_per_trade=0.005)).check(buy(price=100, stop=80), state(), NOW)
    assert d.qty == 25
