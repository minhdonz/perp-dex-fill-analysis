"""Retention rollup + gap marking — spec §5, §6; PRD §8.

1. Summarize completed 2h UTC windows (the 12 daily session buckets) into
   `window_summaries`, marking any window that overlaps an integrity gap
   (disconnect/gap/stale) as has_gap=1 so it is excluded from published
   numbers rather than silently averaged in.
2. Prune full-granularity rows older than the retention window (90 days,
   spec §8 [DECIDED]) — only after their windows have been summarized.

Run from cron (e.g. daily): python -m analysis.rollup --db data/fills.db
Use --no-prune to summarize without deleting.
"""
from __future__ import annotations

import argparse
import time

from collector.storage import connect

WINDOW_NS = 2 * 3600 * 1_000_000_000
RETENTION_DAYS = 90
GAP_EVENTS = ("disconnect", "gap", "stale")


def summarize(conn, now_ns: int) -> int:
    """Upsert summaries for all complete windows present in trades."""
    last_complete = (now_ns // WINDOW_NS) * WINDOW_NS  # windows ending before this
    n = conn.execute(
        f"""
        INSERT OR REPLACE INTO window_summaries
        SELECT
            t.venue, t.coin,
            (t.ts_ns / {WINDOW_NS}) * {WINDOW_NS} AS w,
            COUNT(*),
            CAST(SUM(CAST(t.size_base AS REAL)) AS TEXT),
            CAST(SUM(CAST(t.notional_usd AS REAL)) AS TEXT),
            (SELECT COUNT(*) FROM book_snapshots b
              WHERE b.venue = t.venue AND b.coin = t.coin
                AND b.ts_ns >= (t.ts_ns / {WINDOW_NS}) * {WINDOW_NS}
                AND b.ts_ns <  (t.ts_ns / {WINDOW_NS}) * {WINDOW_NS} + {WINDOW_NS}),
            SUM(t.is_liquidation),
            MIN(t.ts_ns), MAX(t.ts_ns),
            EXISTS (
                SELECT 1 FROM integrity_log il
                WHERE il.venue = t.venue
                  AND (il.coin IS NULL OR il.coin = t.coin)
                  AND il.event IN {GAP_EVENTS!r}
                  AND il.ts_ns >= (t.ts_ns / {WINDOW_NS}) * {WINDOW_NS}
                  AND il.ts_ns <  (t.ts_ns / {WINDOW_NS}) * {WINDOW_NS} + {WINDOW_NS}
            ),
            NULL
        FROM trades t
        WHERE t.ts_ns < {last_complete}
        GROUP BY t.venue, t.coin, w
        """
    ).rowcount
    conn.commit()
    return n


def prune(conn, now_ns: int) -> tuple[int, int]:
    """Drop granular rows older than retention, but never rows whose window
    hasn't been summarized yet."""
    cutoff_ns = now_ns - RETENTION_DAYS * 86400 * 1_000_000_000
    cutoff_ns = (cutoff_ns // WINDOW_NS) * WINDOW_NS  # align to window boundary
    guard = """
        AND (venue, coin, (ts_ns / {w}) * {w}) IN
            (SELECT venue, coin, window_start_ns FROM window_summaries)
    """.format(w=WINDOW_NS)
    t = conn.execute(f"DELETE FROM trades WHERE ts_ns < {cutoff_ns} {guard}").rowcount
    b = conn.execute(f"DELETE FROM book_snapshots WHERE ts_ns < {cutoff_ns} {guard}").rowcount
    conn.commit()
    return t, b


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/fills.db")
    p.add_argument("--no-prune", action="store_true")
    args = p.parse_args()

    conn = connect(args.db)
    now_ns = time.time_ns()
    n = summarize(conn, now_ns)
    print(f"summarized/updated {n} venue-coin-window rows")
    if not args.no_prune:
        t, b = prune(conn, now_ns)
        print(f"pruned {t} trades, {b} book snapshots older than {RETENTION_DAYS}d")
        if t or b:
            conn.execute("VACUUM")
    gaps = conn.execute(
        "SELECT venue, COUNT(*), SUM(has_gap) FROM window_summaries GROUP BY venue"
    ).fetchall()
    for venue, total, gapped in gaps:
        clean = 100 * (total - (gapped or 0)) / total
        print(f"coverage {venue}: {clean:.1f}% of {total} windows clean")


if __name__ == "__main__":
    main()
