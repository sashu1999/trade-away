"""Command line entry point: `trade-away <command>`."""

import argparse
import logging
import signal
import sys
import threading

from .config import load_settings
from .db import Store


def _require_keys(settings) -> None:
    if not settings.has_keys:
        sys.exit("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set (see .env.example)")


def cmd_check(settings, store, args) -> None:
    from alpaca.trading.client import TradingClient

    from .news import check_news_access

    _require_keys(settings)
    account = TradingClient(settings.api_key, settings.secret_key, paper=True).get_account()
    print(f"paper account {account.account_number}: equity ${float(account.equity):,.2f}, "
          f"crypto status {account.crypto_status}")
    ok, message = check_news_access(settings)
    print(message)


def cmd_screen(settings, store, args) -> None:
    from .universe import run_screen

    _require_keys(settings)
    ranked = run_screen(settings, store)
    print(ranked.head(args.show).to_string(index=False))
    print(f"{len(ranked)} symbols passed the screen")


def cmd_backfill(settings, store, args) -> None:
    from .backfill import run_backfill

    _require_keys(settings)
    print(run_backfill(settings, store, top_n=args.top, years=args.years))


def cmd_stream(settings, store, args) -> None:
    from .stream import pick_stock_symbols, start_streams

    _require_keys(settings)
    threads = start_streams(settings, store, pick_stock_symbols(store, settings))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set() and any(t.is_alive() for t in threads):
            stop.wait(5)
    except KeyboardInterrupt:
        pass
    if not stop.is_set() and not any(t.is_alive() for t in threads):
        sys.exit("all streams stopped")  # non-zero so systemd restarts us


def cmd_news(settings, store, args) -> None:
    from .news import fetch_news

    _require_keys(settings)
    symbols = args.symbols.split(",") if args.symbols else None
    print(f"{fetch_news(settings, store, symbols, hours=args.hours)} new events")


def cmd_health(settings, store, args) -> None:
    from .health import crypto_gaps, last_bar_ages

    gaps = crypto_gaps(store, settings.crypto_symbols, hours=args.hours)
    for gap in gaps:
        print(f"GAP {gap.symbol}: {gap.start:%Y-%m-%d %H:%M} to {gap.end:%H:%M} UTC ({gap.minutes:.0f} min)")
    for symbol, age in sorted(last_bar_ages(store).items()):
        print(f"{symbol:10} last bar {age.total_seconds() / 60:.0f} min ago")
    print("no crypto gaps" if not gaps else f"{len(gaps)} crypto gaps in the last {args.hours}h")
    if gaps:
        sys.exit(1)


def cmd_backtest(settings, store, args) -> None:
    from .backtest import frame_from_rows, run_backtest
    from .strategies import DEFAULT_STRATEGIES

    strategies = tuple(s for s in DEFAULT_STRATEGIES if not args.strategy or s.name == args.strategy)
    symbols = {s: "stock" for s in store.latest_universe(limit=args.top)}
    symbols.update({s: "crypto" for s in settings.crypto_symbols})
    bars = {s: frame_from_rows(store.daily_bars(s, limit=args.days)) for s in symbols}
    bars = {s: df for s, df in bars.items() if not df.empty}
    if not bars:
        sys.exit("no daily bars in the database; run `trade-away screen` and `trade-away backfill` first")
    if "SPY" not in bars and (spy := frame_from_rows(store.daily_bars("SPY", limit=args.days))).size:
        bars["SPY"], symbols["SPY"] = spy, "stock"
    result = run_backtest(bars, symbols, strategies, cost=args.cost)
    for key, value in result.metrics().items():
        print(f"{key:18} {value:,.4f}" if isinstance(value, float) else f"{key:18} {value}")
    if result.rejections:
        print("risk rejections:", ", ".join(f"{k} x{v}" for k, v in sorted(result.rejections.items())))


def cmd_run(settings, store, args) -> None:
    from .engine import run_daily
    from .execution import AlpacaPaperBroker

    _require_keys(settings)
    actions = run_daily(settings, store, AlpacaPaperBroker(settings), top_n=args.top,
                        crypto_only=args.crypto_only, dry_run=args.dry_run)
    for a in actions:
        verdict = f"APPROVED qty={a.decision.qty:g}" if a.decision.approved else "REJECTED"
        print(f"{a.side:4} {a.symbol:10} {a.strategy:6} {verdict}: {a.decision.reason} | {a.reason}")
    print(f"{len(actions)} signals{' (dry run, nothing sent)' if args.dry_run else ''}")


def cmd_stops(settings, store, args) -> None:
    from .engine import check_stops
    from .execution import AlpacaPaperBroker

    _require_keys(settings)
    closed = check_stops(settings, store, AlpacaPaperBroker(settings))
    print(f"stopped out: {', '.join(closed)}" if closed else "no stops hit")


def cmd_publish(settings, store, args) -> None:
    from .publish import run_publish

    _require_keys(settings)
    snap = run_publish(settings, store, out=args.out, push=not args.no_push)
    s = snap["summary"]
    print(f"equity ${s['equity']:,.2f}, {s['open_positions']} positions, {s['trades']} trades"
          f"{'' if args.no_push else ' -> published'}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trade-away")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify keys, paper account and news access")
    s = sub.add_parser("screen", help="nightly universe screen of all US stocks and ETFs")
    s.add_argument("--show", type=int, default=20)
    b = sub.add_parser("backfill", help="daily history for top screened names and crypto")
    b.add_argument("--top", type=int, default=200)
    b.add_argument("--years", type=float, default=2)
    sub.add_parser("stream", help="run the live stock and crypto streams")
    n = sub.add_parser("news", help="fetch recent news into the events table")
    n.add_argument("--symbols", default="")
    n.add_argument("--hours", type=int, default=24)
    h = sub.add_parser("health", help="report stream gaps (Phase 0 gate)")
    h.add_argument("--hours", type=float, default=24 * 7)
    bt = sub.add_parser("backtest", help="backtest the rules-only strategies on stored daily bars")
    bt.add_argument("--top", type=int, default=100)
    bt.add_argument("--days", type=int, default=750)
    bt.add_argument("--strategy", choices=["trend", "dip"])
    bt.add_argument("--cost", type=float, default=0.001, help="per-side cost haircut")
    r = sub.add_parser("run", help="daily rules-only trading run on the paper account")
    r.add_argument("--top", type=int, default=100)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--crypto-only", action="store_true", help="weekend runs: no new stock entries")
    sub.add_parser("stops", help="enforce crypto stop-losses from the latest streamed prices")
    pub = sub.add_parser("publish", help="push a public snapshot of the paper account to GitHub Pages")
    pub.add_argument("--out", help="also write the snapshot JSON to this file")
    pub.add_argument("--no-push", action="store_true", help="build the snapshot without pushing it")
    return p


COMMANDS = {
    "check": cmd_check, "screen": cmd_screen, "backfill": cmd_backfill,
    "stream": cmd_stream, "news": cmd_news, "health": cmd_health,
    "backtest": cmd_backtest, "run": cmd_run, "stops": cmd_stops,
    "publish": cmd_publish,
}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        COMMANDS[args.command](settings, store, args)
    finally:
        store.close()


if __name__ == "__main__":
    main()
