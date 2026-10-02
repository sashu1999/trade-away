"""Stream health checks for the Phase 0 gate: the stream runs a full week without gaps.

Crypto trades around the clock, so any long hole in its minute bars means the
stream (or the server) was down. Stock bars are only expected in market hours,
so for stocks we just report how old the newest bar is.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Store


@dataclass(frozen=True)
class Gap:
    symbol: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def find_gaps(timestamps: list[datetime], symbol: str, max_gap: timedelta) -> list[Gap]:
    ordered = sorted(timestamps)
    return [
        Gap(symbol, a, b)
        for a, b in zip(ordered, ordered[1:])
        if b - a > max_gap
    ]


def crypto_gaps(store: Store, symbols: list[str], hours: float = 24,
                max_gap: timedelta = timedelta(minutes=5),
                now: datetime | None = None) -> list[Gap]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    gaps: list[Gap] = []
    for symbol in symbols:
        rows = store.query(
            "SELECT ts FROM bars WHERE symbol = ? AND timeframe = '1Min' AND ts >= ? ORDER BY ts",
            (symbol, since.isoformat()),
        )
        stamps = [datetime.fromisoformat(r["ts"]) for r in rows]
        # Treat the window edges as observations so a stream that died (or never started) shows up.
        gaps += find_gaps([since, *stamps, now], symbol, max_gap)
    return gaps


def last_bar_ages(store: Store, now: datetime | None = None) -> dict[str, timedelta]:
    now = now or datetime.now(timezone.utc)
    rows = store.query("SELECT symbol, MAX(ts) AS ts FROM bars WHERE timeframe = '1Min' GROUP BY symbol")
    return {r["symbol"]: now - datetime.fromisoformat(r["ts"]) for r in rows}
