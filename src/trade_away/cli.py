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
    return p


COMMANDS = {
    "check": cmd_check, "screen": cmd_screen, "backfill": cmd_backfill,
    "stream": cmd_stream, "news": cmd_news, "health": cmd_health,
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
