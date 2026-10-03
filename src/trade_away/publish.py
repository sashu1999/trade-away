"""Public trade tracker: a JSON snapshot of the paper account pushed to GitHub Pages.

`trade-away publish` reads the paper account from Alpaca plus the decision journal,
builds a snapshot with no keys, account numbers or order ids in it, and force-pushes
it with the static page in `site/` as a single commit on the `gh-pages` branch.
Force-pushing one commit keeps that branch tiny however often the cron job runs.
"""

import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Store, to_iso

log = logging.getLogger(__name__)

SITE_DIR = Path(__file__).resolve().parents[2] / "site"
STARTING_EQUITY = 100_000.0
MAX_TRADES = 200
BOT_INFO = {
    "trend": "Buys when the 20-day average crosses above the 50-day, sells when it crosses back",
    "dip": "Buys a sharp 2-day drop (RSI(2) under 10) in an uptrend, sells the bounce",
    "breakout": "Buys a new 20-day high, sells a new 10-day low",
    "momentum": "Holds the 10 strongest stocks of the last 6 months",
}


def _f(value) -> float | None:
    return None if value is None else float(value)


def _enum(value) -> str:
    return str(getattr(value, "value", value)).lower()


def _iso(value) -> str | None:
    if value is None:
        return None
    return to_iso(value) if isinstance(value, datetime) else str(value)


def _realized(trades: list[dict]) -> list[dict]:
    """Fill in realized P/L on each sell using average cost per symbol. Trades are oldest first."""
    qty: dict[str, float] = defaultdict(float)
    cost: dict[str, float] = defaultdict(float)
    for t in trades:
        s = t["symbol"]
        if t["side"] == "buy":
            qty[s] += t["qty"]
            cost[s] += t["qty"] * t["price"]
            t["pnl"] = None
        else:
            held = qty[s]
            if held <= 0:  # bought before the history window: no cost basis
                t["pnl"] = None
                continue
            avg = cost[s] / held
            sold = min(t["qty"], held)
            t["pnl"] = round((t["price"] - avg) * sold, 2)
            t["pnl_pct"] = round(t["price"] / avg - 1, 4)
            qty[s] -= sold
            cost[s] -= avg * sold
    return trades


def build_snapshot(account: dict, positions: Iterable[dict], fills: Iterable[dict],
                   history: Iterable[tuple[str, float]], decisions: dict[str, dict],
                   benchmark: Iterable[tuple[str, float]], now: datetime,
                   starting_equity: float = STARTING_EQUITY, bots: Iterable[dict] = (),
                   bot_history: dict[str, list[tuple[str, float]]] | None = None,
                   owners: dict[str, str] | None = None) -> dict:
    """Assemble the public snapshot from plain data. Only fields listed here are published.

    account: equity, cash, last_equity. positions: symbol, asset_class, qty, avg_entry,
    price, market_value, unrealized_pl. fills: order_id, ts, symbol, side, qty, price, type.
    history: (date, equity), oldest first. decisions: order_id -> strategy, reason, stop.
    benchmark: (date, close) for SPY, oldest first. bots: bot, start, equity, positions per
    strategy bot. bot_history: bot -> (date, equity), oldest first. owners: symbol -> bot.
    """
    owners = owners or {}
    bot_history = bot_history or {}
    benchmark = list(benchmark)
    equity = float(account["equity"])
    last_equity = float(account.get("last_equity") or equity)

    trades = []
    for f in sorted(fills, key=lambda f: f["ts"]):
        d = decisions.get(f["order_id"], {})
        strategy, reason = d.get("strategy"), d.get("reason")
        if not reason and f["side"] == "sell" and f.get("type") in ("stop", "stop_limit", "trailing_stop"):
            reason = "stop-loss"
        trades.append({"ts": f["ts"], "symbol": f["symbol"], "side": f["side"],
                       "qty": float(f["qty"]), "price": float(f["price"]),
                       "strategy": strategy, "reason": reason})
    trades = _realized(trades)
    closed = [t for t in trades if t["side"] == "sell" and t["pnl"] is not None]
    wins = [t for t in closed if t["pnl"] > 0]

    pos_out = []
    for p in positions:
        pos_out.append({
            "symbol": p["symbol"], "bot": owners.get(p["symbol"]), "asset_class": p["asset_class"], "qty": float(p["qty"]),
            "avg_entry": _f(p["avg_entry"]), "price": _f(p["price"]),
            "market_value": round(float(p["market_value"]), 2),
            "weight": round(float(p["market_value"]) / equity, 4) if equity else None,
            "unrealized_pl": round(float(p["unrealized_pl"]), 2),
            "unrealized_pct": round(float(p["price"]) / float(p["avg_entry"]) - 1, 4)
            if p.get("avg_entry") and p.get("price") else None,
        })
    pos_out.sort(key=lambda p: -p["market_value"])

    curve = [{"date": d, "equity": round(e, 2)} for d, e in history if e]
    today = now.date().isoformat()
    if not curve or curve[-1]["date"] != today:
        curve.append({"date": today, "equity": round(equity, 2)})
    else:
        curve[-1]["equity"] = round(equity, 2)

    # SPY scaled to the account's first equity point, so both lines start together.
    bench = []
    if curve:
        start, base = curve[0]["date"], curve[0]["equity"]
        closes = [(d, c) for d, c in benchmark if d >= start]
        if closes:
            first = closes[0][1]
            bench = [{"date": d, "equity": round(base * c / first, 2)} for d, c in closes]

    peak, max_dd = 0.0, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_dd = max(max_dd, 1 - point["equity"] / peak if peak else 0)

    bots_out = []
    for b in bots:
        name, start, eq = b["bot"], float(b["start"]), float(b["equity"])
        days = {d: e for d, e in bot_history.get(name, [])}  # last value per day wins
        days[today] = eq
        points = [{"date": d, "equity": round(e, 2)} for d, e in sorted(days.items())]
        mine = [t for t in closed if t["strategy"] == name]
        bpeak, bdd = 0.0, 0.0
        for point in points:
            bpeak = max(bpeak, point["equity"])
            bdd = max(bdd, 1 - point["equity"] / bpeak if bpeak else 0)
        bots_out.append({
            "bot": name, "description": BOT_INFO.get(name, ""), "start": start, "equity": round(eq, 2),
            "return": round(eq / start - 1, 4), "max_drawdown": round(bdd, 4),
            "open_positions": int(b["positions"]), "closed_trades": len(mine),
            "win_rate": round(sum(t["pnl"] > 0 for t in mine) / len(mine), 4) if mine else None,
            "realized_pl": round(sum(t["pnl"] for t in mine), 2), "curve": points,
        })
    bots_out.sort(key=lambda b: -b["return"])

    # SPY on the same footing as a bot: a $25k book from the day the race started.
    bot_bench = []
    if bots_out:
        start_day = min(b["curve"][0]["date"] for b in bots_out)
        closes = [(d, c) for d, c in benchmark if d >= start_day]
        if closes:
            stake = bots_out[0]["start"]
            bot_bench = [{"date": d, "equity": round(stake * c / closes[0][1], 2)} for d, c in closes]

    return {
        "generated_at": to_iso(now),
        "mode": "paper",
        "summary": {
            "equity": round(equity, 2),
            "cash": round(float(account["cash"]), 2),
            "starting_equity": starting_equity,
            "total_return": round(equity / starting_equity - 1, 4),
            "day_change": round(equity / last_equity - 1, 4) if last_equity else 0,
            "benchmark_return": round(bench[-1]["equity"] / bench[0]["equity"] - 1, 4) if bench else None,
            "max_drawdown": round(max_dd, 4),
            "open_positions": len(pos_out),
            "trades": len(trades),
            "closed_trades": len(closed),
            "win_rate": round(len(wins) / len(closed), 4) if closed else None,
            "realized_pl": round(sum(t["pnl"] for t in closed), 2),
        },
        "positions": pos_out,
        "trades": trades[::-1][:MAX_TRADES],
        "equity_curve": curve,
        "benchmark": bench,
        "bots": bots_out,
        "bot_benchmark": bot_bench,
    }


def fetch_snapshot(settings: Settings, store: Store, now: datetime | None = None) -> dict:
    """Read the paper account from Alpaca and the journal from SQLite, then build the snapshot."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

    from .engine import leaderboard, sync_fills
    from .execution import AlpacaPaperBroker

    now = now or datetime.now(timezone.utc)
    broker = AlpacaPaperBroker(settings)
    sync_fills(store, broker, now)  # real fill prices and stop-outs since the last run
    client = broker.client
    crypto_names = {s.replace("/", ""): s for s in settings.crypto_symbols}

    acct = client.get_account()
    account = {"equity": acct.equity, "cash": acct.cash, "last_equity": acct.last_equity}

    positions = []
    for p in client.get_all_positions():
        crypto = "crypto" in _enum(p.asset_class)
        positions.append({
            "symbol": crypto_names.get(p.symbol, p.symbol) if crypto else p.symbol,
            "asset_class": "crypto" if crypto else "stock", "qty": p.qty,
            "avg_entry": p.avg_entry_price, "price": p.current_price,
            "market_value": p.market_value, "unrealized_pl": p.unrealized_pl,
        })

    fills = []
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, nested=False))
    for o in orders:
        if o.filled_at is None or not o.filled_qty or float(o.filled_qty) == 0:
            continue
        fills.append({
            "order_id": str(o.id), "ts": _iso(o.filled_at),
            "symbol": crypto_names.get(o.symbol.replace("/", ""), o.symbol),
            "side": _enum(o.side), "qty": o.filled_qty, "price": o.filled_avg_price,
            "type": _enum(o.order_type or o.type),
        })

    history = []
    try:
        ph = client.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D"))
        for ts, eq in zip(ph.timestamp or [], ph.equity or []):
            if eq:
                history.append((datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), float(eq)))
    except Exception as exc:  # fall back to our own log rather than publish nothing
        log.warning("portfolio history unavailable (%s); using equity_log", exc)
        rows = store.query("SELECT substr(ts, 1, 10) AS d, SUM(equity) AS equity FROM equity_log "
                           "GROUP BY ts ORDER BY ts")  # the bots' books add up to the account
        history = list({r["d"]: float(r["equity"]) for r in rows}.items())

    decisions = {
        r["order_id"]: {"strategy": r["strategy"], "reason": r["reason"]}
        for r in store.query("SELECT order_id, strategy, reason FROM decisions "
                             "WHERE order_id IS NOT NULL AND dry_run = 0")
    }
    # Stop-loss legs and crypto stops have no decision row with their order id; the book knows the bot.
    for r in store.query("SELECT order_id, account FROM book_fills"):
        decisions.setdefault(r["order_id"], {"strategy": r["account"], "reason": None})
    benchmark = [(r["ts"][:10], float(r["close"])) for r in store.daily_bars("SPY", limit=400)]

    bots = leaderboard(store, broker, settings, now=now, record=True)
    bot_history: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in store.query("SELECT account, substr(ts, 1, 10) AS d, equity FROM equity_log ORDER BY ts"):
        bot_history[r["account"]].append((r["d"], float(r["equity"])))
    owners = {r["symbol"]: r["account"] for r in store.query("SELECT symbol, account FROM open_trades")}
    return build_snapshot(account, positions, fills, history, decisions, benchmark, now,
                          bots=bots, bot_history=bot_history, owners=owners)


def push_site(snapshot: dict, remote: str, branch: str = "gh-pages", token: str = "",
              site_dir: Path = SITE_DIR) -> None:
    """Force-push the static page plus data.json as one orphan commit on `branch`.

    The token reaches git through GIT_ASKPASS, so it never appears in the remote URL,
    on the command line or in git's error messages.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "site"
        shutil.copytree(site_dir, work)
        (work / "data.json").write_text(json.dumps(snapshot, separators=(",", ":")))
        (work / ".nojekyll").write_text("")

        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if token:
            askpass = Path(tmp) / "askpass.sh"
            askpass.write_text('#!/bin/sh\nprintf "%s" "$TRADE_AWAY_PAGES_TOKEN"\n')
            askpass.chmod(stat.S_IRWXU)
            env.update(GIT_ASKPASS=str(askpass), TRADE_AWAY_PAGES_TOKEN=token)

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=work, env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        git("init", "-q")
        git("checkout", "-q", "-b", branch)
        git("add", "-A")
        git("-c", "user.name=trade-away bot", "-c", "user.email=trade-away@users.noreply.github.com",
            "commit", "-q", "-m", f"Snapshot {snapshot['generated_at']}")
        try:
            git("push", "-q", "--force", remote, f"HEAD:{branch}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git push to {branch} failed: {exc.stderr.strip()}") from None


def run_publish(settings: Settings, store: Store, out: str | None = None, push: bool = True) -> dict:
    snapshot = fetch_snapshot(settings, store)
    if out:
        Path(out).write_text(json.dumps(snapshot, indent=2))
    if push:
        token = os.environ.get("PAGES_TOKEN", "")
        remote = os.environ.get("PAGES_REMOTE", "https://x-access-token@github.com/sashu1999/trade-away.git")
        if not token and "PAGES_REMOTE" not in os.environ:
            raise SystemExit("PAGES_TOKEN is not set (see .env.example)")
        push_site(snapshot, remote, os.environ.get("PAGES_BRANCH", "gh-pages"), token)
    return snapshot
