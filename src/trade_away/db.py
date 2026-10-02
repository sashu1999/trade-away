"""SQLite storage for bars, latest prices, the screened universe and events.

Timestamps are stored as ISO-8601 UTC strings so they sort correctly as text.
"""

import os
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    asset_class TEXT NOT NULL,          -- 'stock' or 'crypto'
    timeframe   TEXT NOT NULL,          -- '1Min', '1Day'
    ts          TEXT NOT NULL,          -- bar start, UTC ISO-8601
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, vwap REAL,
    source      TEXT NOT NULL,          -- 'iex', 'sip', 'crypto_us'
    PRIMARY KEY (symbol, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS latest_prices (
    symbol      TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    ts          TEXT NOT NULL,
    price       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS universe (
    as_of             TEXT NOT NULL,    -- date of the screen
    symbol            TEXT NOT NULL,
    close             REAL NOT NULL,
    avg_dollar_volume REAL NOT NULL,
    rank              INTEGER NOT NULL,
    PRIMARY KEY (as_of, symbol)
);

CREATE TABLE IF NOT EXISTS events (
    id       TEXT PRIMARY KEY,          -- source-specific unique id
    kind     TEXT NOT NULL,             -- 'news', 'filing', 'insider', 'macro', ...
    symbol   TEXT,
    ts       TEXT NOT NULL,
    headline TEXT NOT NULL,
    url      TEXT,
    source   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events (symbol, ts);
"""


@dataclass(frozen=True)
class BarRow:
    symbol: str
    asset_class: str
    timeframe: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    source: str


def to_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def upsert_bars(self, bars: Iterable[BarRow]) -> int:
        rows = [
            (b.symbol, b.asset_class, b.timeframe, to_iso(b.ts), b.open, b.high,
             b.low, b.close, b.volume, b.vwap, b.source)
            for b in bars
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def set_latest_price(self, symbol: str, asset_class: str, ts: datetime, price: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO latest_prices VALUES (?,?,?,?)",
                (symbol, asset_class, to_iso(ts), price),
            )

    def replace_universe(self, as_of: str, rows: Iterable[tuple[str, float, float]]) -> int:
        """rows: (symbol, close, avg_dollar_volume), already ranked best first."""
        ranked = [(as_of, s, c, v, i + 1) for i, (s, c, v) in enumerate(rows)]
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM universe WHERE as_of = ?", (as_of,))
            self._conn.executemany("INSERT INTO universe VALUES (?,?,?,?,?)", ranked)
        return len(ranked)

    def insert_events(self, events: Iterable[tuple[str, str, str | None, datetime, str, str | None, str]]) -> int:
        """events: (id, kind, symbol, ts, headline, url, source). Duplicates are skipped."""
        rows = [(i, k, s, to_iso(t), h, u, src) for i, k, s, t, h, u, src in events]
        with self._lock, self._conn:
            cur = self._conn.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)", rows)
        return cur.rowcount

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def latest_universe(self, limit: int | None = None) -> list[str]:
        sql = "SELECT symbol FROM universe WHERE as_of = (SELECT MAX(as_of) FROM universe) ORDER BY rank"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["symbol"] for r in self.query(sql)]
