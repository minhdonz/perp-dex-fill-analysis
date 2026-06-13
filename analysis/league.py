"""Cross-venue league table — PRD §6.1 "The Gap" (the P0 product headline).

For each asset and clip-size rung, rank venues by realized execution cost and
show advertised vs realized vs the gap (bps), with the sample count and
confidence label on every cell. Honest seams per PRD §11 / spec §9:
  - sparse cells (n < --min-samples) are shown with their count, never hidden
    or smoothed into a fabricated number;
  - a venue only appears for assets it lists / we observed;
  - per-venue coverage (unmatched, exceeds-visible-depth) is reported so a
    thin-book venue isn't silently compared at a size its book can't show.

Usage:
  python -m analysis.league --db data/fills.db --hours 6
  python -m analysis.league --db data/fills.db --assets BTC ETH --venues lighter hyperliquid
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from statistics import median

from analysis.reconstruct import (
    METHOD, RUNGS, BookMatcher, group_clips, load_trades, measure, rung_for,
)

ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]
CONF_TAG = {"exact": "exact", "identity": "ident", "heuristic": "heur"}


def rung_label(r: int) -> str:
    return f"${r/1000:.0f}k" if r < 1_000_000 else f"${r/1_000_000:.0f}M"


def collect(conn, venue, coin, since_ns, window_ms):
    """Return (per_rung measurements, coverage dict). per_rung: rung -> list of
    measure() dicts. coverage: counts of clips, measured, skip reasons."""
    rows = load_trades(conn, venue, coin, since_ns)
    cov = {"prints": len(rows), "clips": 0, "measured": 0, "skips": {}}
    per_rung: dict[int, list] = {}
    if not rows:
        return per_rung, cov
    clips = group_clips(rows, venue, coin, window_ms)
    cov["clips"] = len(clips)
    matcher = BookMatcher(conn, venue, coin)
    for c in clips:
        r = measure(c, matcher)
        if "skip" in r:
            cov["skips"][r["skip"]] = cov["skips"].get(r["skip"], 0) + 1
            continue
        cov["measured"] += 1
        rung = rung_for(float(c.notional))
        if rung is not None:
            per_rung.setdefault(rung, []).append(r)
    return per_rung, cov


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--assets", nargs="+", default=ALL_ASSETS)
    ap.add_argument("--venues", nargs="+", default=ALL_VENUES)
    ap.add_argument("--window-ms", type=int, default=150)
    ap.add_argument("--min-samples", type=int, default=5)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)

    print(f"\nLEAGUE TABLE — advertised vs realized execution cost (bps)")
    print(f"window: last {args.hours:.0f}h   clip-window(HL/Pac): {args.window_ms}ms   "
          f"sparse threshold: n<{args.min_samples}")
    print("gap = realized − advertised (+ = filled worse than the book advertised)")

    coverage_all = {}
    for coin in args.assets:
        data = {}
        for v in args.venues:
            per_rung, cov = collect(conn, v, coin, since_ns, args.window_ms)
            data[v] = per_rung
            coverage_all[(coin, v)] = cov

        eligible = [v for v in args.venues if coverage_all[(coin, v)]["prints"] > 0]
        if not eligible:
            continue
        print("\n" + "=" * 78)
        print(f"{coin}   (venues observed: {', '.join(eligible)})")
        print("=" * 78)

        for rung in RUNGS:
            # one cell per eligible venue at this rung
            cells = []
            for v in eligible:
                ms = data[v].get(rung)
                if not ms:
                    continue
                real_med = median(x["realized_bps"] for x in ms)
                adv_med = median(x["advertised_bps"] for x in ms)
                # gap = difference of the displayed marginals, so the table is
                # arithmetically self-consistent and matches the quotable claim
                # ("advertises adv, fills real"). Paired per-clip gap is a
                # separate statistic used in the calibration tool, not here.
                cells.append({
                    "venue": v,
                    "conf": CONF_TAG[METHOD[v]],
                    "n": len(ms),
                    "real": real_med,
                    "adv": adv_med,
                    "gap": real_med - adv_med,
                    "age": median(x["book_age_ms"] for x in ms),
                })
            if not cells:
                continue
            # rank by realized cost (cheapest first), but sparse cells always
            # sort below well-sampled ones so a 3-sample outlier can't claim #1
            # (PRD §11: show the count, never let a quiet rung manufacture a rank)
            cells.sort(key=lambda c: (c["n"] < args.min_samples, c["real"]))
            print(f"\n  {rung_label(rung):>6} rung      "
                  f"{'venue':<12} {'conf':>6} {'n':>5} "
                  f"{'real':>7} {'adv':>7} {'gap':>7} {'age ms':>7}")
            for rank, c in enumerate(cells, 1):
                sparse = "  (sparse)" if c["n"] < args.min_samples else ""
                print(f"  {'':>6}      #{rank}  {c['venue']:<12} {c['conf']:>6} "
                      f"{c['n']:>5} {c['real']:>7.2f} {c['adv']:>7.2f} "
                      f"{c['gap']:>+7.2f} {c['age']:>7.0f}{sparse}")

    # methodology / coverage footer (book-depth comparability, PRD §11)
    print("\n" + "=" * 78)
    print("COVERAGE & CONFIDENCE  (per venue, across the assets above)")
    print("=" * 78)
    print(f"  {'venue':<12} {'method':>10} {'clips':>8} {'measured':>9} "
          f"{'unmatched':>10} {'depth-cut':>10}")
    for v in args.venues:
        clips = sum(c["clips"] for (cn, vv), c in coverage_all.items() if vv == v)
        meas = sum(c["measured"] for (cn, vv), c in coverage_all.items() if vv == v)
        unm = sum(c["skips"].get("no book within 1s", 0) + c["skips"].get("empty book", 0)
                  for (cn, vv), c in coverage_all.items() if vv == v)
        depth = sum(c["skips"].get("exceeds visible depth", 0)
                    for (cn, vv), c in coverage_all.items() if vv == v)
        if clips == 0:
            continue
        print(f"  {v:<12} {METHOD[v]:>10} {clips:>8} {meas:>9} "
              f"{unm:>10} {depth:>10}")
    print("\n  exact=Lighter order id · identity=HL taker addr+window · "
          "heuristic=Pacifica sweep")
    print("  depth-cut = clips larger than the venue's visible book "
          "(excluded, not extrapolated)")


if __name__ == "__main__":
    main()
