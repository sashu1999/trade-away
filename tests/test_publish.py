import json
import subprocess
from datetime import datetime, timezone

from trade_away.publish import build_snapshot, push_site

NOW = datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)


def snapshot():
    fills = [
        {"order_id": "o1", "ts": "2026-10-01T13:30:00+00:00", "symbol": "AAPL", "side": "buy",
         "qty": "10", "price": "200", "type": "market"},
        {"order_id": "o2", "ts": "2026-10-02T13:30:00+00:00", "symbol": "MSFT", "side": "buy",
         "qty": "5", "price": "400", "type": "market"},
        {"order_id": "o3", "ts": "2026-10-03T15:00:00+00:00", "symbol": "AAPL", "side": "sell",
         "qty": "10", "price": "210", "type": "market"},
        {"order_id": "leg", "ts": "2026-10-04T15:00:00+00:00", "symbol": "MSFT", "side": "sell",
         "qty": "5", "price": "380", "type": "stop"},
    ]
    decisions = {"o1": {"strategy": "trend", "reason": "20d crossed above 50d"},
                 "o3": {"strategy": "trend", "reason": "20d fell below 50d"}}
    positions = [{"symbol": "BTC/USD", "asset_class": "crypto", "qty": "0.1", "avg_entry": "60000",
                  "price": "66000", "market_value": "6600", "unrealized_pl": "600"}]
    history = [("2026-10-01", 100_000.0), ("2026-10-02", 0.0), ("2026-10-03", 101_000.0)]
    bench = [("2026-09-30", 500.0), ("2026-10-01", 500.0), ("2026-10-05", 505.0)]
    account = {"equity": "100500", "cash": "93900", "last_equity": "100000",
               "account_number": "PA123SECRET"}
    return build_snapshot(account, positions, fills, history, decisions, bench, NOW)


def test_snapshot_trades_pnl_and_reasons():
    s = snapshot()
    trades = s["trades"]
    assert [t["symbol"] for t in trades] == ["MSFT", "AAPL", "MSFT", "AAPL"]  # newest first
    assert trades[0]["reason"] == "stop-loss" and trades[0]["pnl"] == -100.0
    assert trades[1]["strategy"] == "trend" and trades[1]["pnl"] == 100.0 and trades[1]["pnl_pct"] == 0.05
    summary = s["summary"]
    assert summary["closed_trades"] == 2 and summary["win_rate"] == 0.5 and summary["realized_pl"] == 0
    assert summary["total_return"] == 0.005 and summary["day_change"] == 0.005


def test_snapshot_curve_benchmark_and_positions():
    s = snapshot()
    assert [p["date"] for p in s["equity_curve"]] == ["2026-10-01", "2026-10-03", "2026-10-05"]
    assert s["equity_curve"][-1]["equity"] == 100_500
    assert s["benchmark"][0] == {"date": "2026-10-01", "equity": 100_000}
    assert s["summary"]["benchmark_return"] == 0.01
    pos = s["positions"][0]
    assert pos["weight"] == round(6600 / 100_500, 4) and pos["unrealized_pct"] == 0.1


def test_snapshot_leaks_no_ids():
    text = json.dumps(snapshot())
    for secret in ("PA123SECRET", "account_number", "order_id", '"o1"', '"leg"'):
        assert secret not in text


def test_push_site_force_pushes_one_commit(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>")
    for _ in range(2):
        push_site(snapshot(), str(remote), site_dir=site)
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "gh-pages"],
                         capture_output=True, text=True, check=True).stdout
    assert len(log.splitlines()) == 1
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "gh-pages"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert sorted(files) == [".nojekyll", "data.json", "index.html"]


def test_snapshot_bots_leaderboard():
    s = build_snapshot(
        {"equity": "100000", "cash": "100000"}, [], [
            {"order_id": "b", "ts": "2026-10-01T13:30:00+00:00", "symbol": "AAPL", "side": "buy",
             "qty": "10", "price": "100", "type": "market"},
            {"order_id": "leg", "ts": "2026-10-02T13:30:00+00:00", "symbol": "AAPL", "side": "sell",
             "qty": "10", "price": "90", "type": "stop"}],
        [], {"b": {"strategy": "dip", "reason": "RSI"}, "leg": {"strategy": "dip", "reason": None}},
        [("2026-10-01", 500.0), ("2026-10-05", 510.0)], NOW,
        bots=[{"bot": "dip", "start": 25_000, "equity": 24_900, "positions": 0},
              {"bot": "trend", "start": 25_000, "equity": 25_500, "positions": 2}],
        bot_history={"trend": [("2026-10-01", 25_000), ("2026-10-01", 25_100), ("2026-10-02", 25_200)]},
        owners={"AAPL": "dip"})
    assert [b["bot"] for b in s["bots"]] == ["trend", "dip"]
    trend, dip = s["bots"]
    assert trend["curve"] == [{"date": "2026-10-01", "equity": 25_100}, {"date": "2026-10-02", "equity": 25_200},
                              {"date": "2026-10-05", "equity": 25_500}]
    assert dip["closed_trades"] == 1 and dip["realized_pl"] == -100 and dip["win_rate"] == 0
    assert s["trades"][0]["reason"] == "stop-loss" and s["trades"][0]["strategy"] == "dip"
    assert s["bot_benchmark"][-1]["equity"] == 25_500
