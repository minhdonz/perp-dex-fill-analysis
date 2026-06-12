"""SQLite storage — spec §5.

Storage choice (spec §8 [OPEN] → decided during spike): SQLite in WAL mode.
Rationale: single $5-VPS process, append-heavy writes, analysis reads are
time-range scans per venue/coin — all squarely inside SQLite's comfort zone,
and DuckDB/Parquet can read SQLite later if the analysis layer wants columnar.

Writes are batched through an asyncio queue so websocket handlers never block
on disk. Trades dedupe on (venue, trade_id) — INSERT OR IGNORE — which makes
reconnect snapshots and REST backfill overlap safe.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from .models import BookSnapshot, MarketStats, TradeRecord

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    ts_ns INTEGER NOT NULL,
    ts_venue_raw TEXT NOT NULL,
    price TEXT NOT NULL,
    size_base TEXT NOT NULL,
    notional_usd TEXT NOT NULL,
    aggressor_side TEXT,
    taker_id TEXT,
    taker_size_before TEXT,
    trade_id TEXT NOT NULL,
    is_liquidation INTEGER NOT NULL DEFAULT 0,
    raw TEXT NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (venue, trade_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_trades_vct ON trades (venue, coin, ts_ns);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (venue, coin, date);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    ts_ns INTEGER NOT NULL,
    ts_venue_raw TEXT NOT NULL,
    bids TEXT NOT NULL,
    asks TEXT NOT NULL,
    raw TEXT NOT NULL,
    date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_vct ON book_snapshots (venue, coin, ts_ns);
CREATE INDEX IF NOT EXISTS idx_book_date ON book_snapshots (venue, coin, date);

CREATE TABLE IF NOT EXISTS market_stats (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    ts_ns INTEGER NOT NULL,
    funding_rate TEXT,
    mark_price TEXT,
    index_price TEXT,
    raw TEXT NOT NULL,
    date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stats_vct ON market_stats (venue, coin, ts_ns);

-- Append-only integrity log (spec §6): disconnects, reconnects, gaps,
-- stale feeds. Publish-time coverage statements are derived from this.
CREATE TABLE IF NOT EXISTS integrity_log (
    id INTEGER PRIMARY KEY,
    ts_ns INTEGER NOT NULL,
    venue TEXT NOT NULL,
    coin TEXT,            -- NULL = whole-connection event
    feed TEXT,            -- 'trades' | 'book' | 'stats' | NULL
    event TEXT NOT NULL,  -- connect|disconnect|gap|backfill|stale|recovered
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_integrity_ts ON integrity_log (venue, ts_ns);

-- 2h-window rollups (spec §5 retention; PRD §8.2). Populated by analysis/rollup.py.
CREATE TABLE IF NOT EXISTS window_summaries (
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    window_start_ns INTEGER NOT NULL,  -- UTC, 2h-aligned
    n_trades INTEGER NOT NULL,
    volume_base TEXT NOT NULL,
    volume_usd TEXT NOT NULL,
    n_book_snapshots INTEGER NOT NULL,
    n_liquidations INTEGER NOT NULL,
    first_trade_ns INTEGER,
    last_trade_ns INTEGER,
    has_gap INTEGER NOT NULL DEFAULT 0,  -- 1 => excluded from published numbers
    gap_detail TEXT,
    PRIMARY KEY (venue, coin, window_start_ns)
);
"""

INSERT_TRADE = """INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
INSERT_BOOK = """INSERT INTO book_snapshots
    (venue, coin, ts_ns, ts_venue_raw, bids, asks, raw, date) VALUES (?,?,?,?,?,?,?,?)"""
INSERT_STATS = """INSERT INTO market_stats
    (venue, coin, ts_ns, funding_rate, mark_price, index_price, raw, date)
    VALUES (?,?,?,?,?,?,?,?)"""
INSERT_INTEGRITY = """INSERT INTO integrity_log (ts_ns, venue, coin, feed, event, detail)
    VALUES (?,?,?,?,?,?)"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # collector + rollup cron may write concurrently; wait instead of erroring
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class Store:
    """Queue-backed batch writer. Producers call put_* (non-blocking);
    a single flush task drains to SQLite every `flush_interval` seconds."""

    def __init__(self, db_path: str | Path, flush_interval: float = 1.0):
        self.conn = connect(db_path)
        self.flush_interval = flush_interval
        self.queue: asyncio.Queue[tuple[str, tuple]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.counts = {"trade": 0, "book": 0, "stats": 0, "integrity": 0}

    def put_trade(self, rec: TradeRecord) -> None:
        self.queue.put_nowait(("trade", rec.to_row()))

    def put_book(self, rec: BookSnapshot) -> None:
        self.queue.put_nowait(("book", rec.to_row()))

    def put_stats(self, rec: MarketStats) -> None:
        self.queue.put_nowait(("stats", rec.to_row()))

    def log_integrity(self, venue: str, event: str, coin: str | None = None,
                      feed: str | None = None, detail: str = "") -> None:
        ts_ns = time.time_ns()
        self.queue.put_nowait(("integrity", (ts_ns, venue, coin, feed, event, detail)))
        log.info("[integrity] %s %s coin=%s feed=%s %s", venue, event, coin, feed, detail)

    async def run(self) -> None:
        self._task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(self.flush_interval)
                self.flush()
        except asyncio.CancelledError:
            self.flush()
            raise

    def flush(self) -> None:
        batches: dict[str, list[tuple]] = {"trade": [], "book": [], "stats": [], "integrity": []}
        while not self.queue.empty():
            kind, row = self.queue.get_nowait()
            batches[kind].append(row)
        if not any(batches.values()):
            return
        with self.conn:
            if batches["trade"]:
                self.conn.executemany(INSERT_TRADE, batches["trade"])
            if batches["book"]:
                self.conn.executemany(INSERT_BOOK, batches["book"])
            if batches["stats"]:
                self.conn.executemany(INSERT_STATS, batches["stats"])
            if batches["integrity"]:
                self.conn.executemany(INSERT_INTEGRITY, batches["integrity"])
        for k, rows in batches.items():
            self.counts[k] += len(rows)

    def close(self) -> None:
        self.flush()
        self.conn.close()
