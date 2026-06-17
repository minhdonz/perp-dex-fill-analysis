"""Per-window advertised-vs-realized gap precompute → gap_windows (PRD P1).

For each completed 2h UTC window × venue × coin × clip-rung, reconstruct clips
and measure them against the book, storing the median advertised/realized/gap
(bps) + book_age + clip count. This is the time-series backbone: the gap can't
be recomputed once books are thinned (rollup.thin_books at 21d), so it MUST run
before thinning.

Idempotent: skips windows already in gap_windows unless --recompute. Only
processes COMPLETE windows (ended before now). Gap-marked windows (has_gap from
window_summaries) are stored flagged, excluded downstream — never averaged in.

  python -m analysis.gap_rollup --db data/fills.db          # incremental
  python -m analysis.gap_rollup --db data/fills.db --recompute
"""
from __future__ import annotations

import argparse
import time
from statistics import median

from analysis.reconstruct import (
    BookMatcher, group_clips, load_trades, measure, rung_for,
)
from collector.storage import connect

WINDOW_NS = 2 * 3600 * 1_000_000_000
ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "SPCX"]


def windows_to_do(conn, venue, coin, last_complete_ns, recompute):
    """Distinct complete 2h windows that have trades, minus those already done."""
    rows = conn.execute(
        f"SELECT DISTINCT (ts_ns/{WINDOW_NS})*{WINDOW_NS} w FROM trades "
        f"WHERE venue=? AND coin=? AND ts_ns < ? ORDER BY w",
        (venue, coin, last_complete_ns),
    ).fetchall()
    wins = [r[0] for r in rows]
    if recompute:
        return wins
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT window_start_ns FROM gap_windows WHERE venue=? AND coin=?",
        (venue, coin))}
    return [w for w in wins if w not in done]


def window_has_gap(conn, venue, coin, w):
    row = conn.execute(
        "SELECT has_gap FROM window_summaries WHERE venue=? AND coin=? AND window_start_ns=?",
        (venue, coin, w)).fetchone()
    return int(row[0]) if row else 0


def rollup_window(conn, venue, coin, w, matcher, window_ms):
    rows = load_trades(conn, venue, coin, w, w + WINDOW_NS)
    if not rows:
        return 0
    clips = group_clips(rows, venue, coin, window_ms)
    by_rung: dict[int, list] = {}
    for c in clips:
        r = measure(c, matcher)
        if "skip" in r:
            continue
        rung = rung_for(float(c.notional))
        if rung is not None:
            by_rung.setdefault(rung, []).append(r)
    if not by_rung:
        return 0
    hg = window_has_gap(conn, venue, coin, w)
    out = []
    for rung, ms in by_rung.items():
        out.append((
            venue, coin, w, rung, len(ms),
            median(x["advertised_bps"] for x in ms),
            median(x["realized_bps"] for x in ms),
            median(x["realized_bps"] - x["advertised_bps"] for x in ms),
            median(x["book_age_ms"] for x in ms),
            hg,
        ))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO gap_windows (venue, coin, window_start_ns, rung, "
            "n_clips, med_advertised_bps, med_realized_bps, med_gap_bps, med_book_age_ms, "
            "has_gap) VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def run(conn, venues=ALL_VENUES, assets=ALL_ASSETS, window_ms=150,
        recompute=False, verbose=True) -> int:
    """Precompute gap_windows for completed windows. Callable from rollup.py
    (must run before thin_books) and from main()."""
    last_complete = (time.time_ns() // WINDOW_NS) * WINDOW_NS
    total_cells = 0
    for venue in venues:
        for coin in assets:
            wins = windows_to_do(conn, venue, coin, last_complete, recompute)
            if not wins:
                continue
            matcher = BookMatcher(conn, venue, coin)  # one book index per venue/coin
            cells = sum(rollup_window(conn, venue, coin, w, matcher, window_ms)
                        for w in wins)
            total_cells += cells
            if verbose:
                print(f"  {venue:<11} {coin:<5} {len(wins):>4} windows -> {cells:>5} rung-cells")
    return total_cells


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--venues", nargs="+", default=ALL_VENUES)
    ap.add_argument("--assets", nargs="+", default=ALL_ASSETS)
    ap.add_argument("--window-ms", type=int, default=150)
    ap.add_argument("--recompute", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    total = run(conn, args.venues, args.assets, args.window_ms, args.recompute)
    print(f"gap_rollup: {total} rung-cells written")


if __name__ == "__main__":
    main()
