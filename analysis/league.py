"""Cross-venue league table — PRD §6.1 "The Gap" (the P0 product headline).

For each asset and clip-size rung: each venue's advertised vs realized cost
(bps), the gap, sample count, and confidence label. The leader (► cheapest
realized) is chosen only among cells that can be trusted and compared:

  - "sparse"  (n < --min-samples): shown for transparency, never ranked.
  - "beats book?" (gap < −0.15 bps): realized implausibly beat the advertised
    book — a stale/near-concurrent book-match artifact, not real price
    improvement. Flagged, not ranked. (Real fix: tighten the match to strictly
    prior + minimum age — pending.)
  - "at touch" (advertised ≈ the venue's half-spread floor): the clip fills at
    the best level, so the cost is the spread (tick-limited). Still rankable —
    a venue that absorbs size at the touch genuinely is cheapest — but if two
    venues tie at the touch the lead is marked "≈tie" so noise isn't a winner.

Coverage footer reports per-venue unmatched + depth-cut so a thin-book venue
isn't silently compared at a size its book can't show (PRD §11 / spec §9).

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
    BOOK_RELIABLE, METHOD, NEG_REALIZED_LIMIT, RUNGS, BookMatcher, group_clips,
    load_trades, measure, rung_for, touch_floor_bps,
)

ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]
CONF_TAG = {"exact": "exact", "identity": "ident", "heuristic": "heur"}
FLOOR_MULT = 1.5      # cost within this x the touch floor counts as "at the floor"
EGREGIOUS_NEG = 0.5   # gap below this (bps) is non-physical => artifact
TIE_EPS = 0.03        # leaders within this (bps) of each other => "≈tie"


def rung_label(r: int) -> str:
    return f"${r/1000:.0f}k" if r < 1_000_000 else f"${r/1_000_000:.0f}M"


def collect(conn, venue, coin, since_ns, window_ms):
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


def build_cell(venue, ms, floor, min_samples):
    real = median(x["realized_bps"] for x in ms)
    adv = median(x["advertised_bps"] for x in ms)
    n = len(ms)
    gap = real - adv
    sparse = n < min_samples
    touch_adv = floor is not None and adv <= floor * FLOOR_MULT  # book says it fits at L1
    touch_real = floor is not None and real <= floor * FLOOR_MULT
    # Where the book is a faithful basis, a negative gap is a matching artifact
    # ("beats book?"). Where the book is feed-limited (Pacifica), the gap is
    # under-measured by design — flag it, but realized is still trustworthy so
    # the cell stays rankable on realized.
    feed_limited = not BOOK_RELIABLE.get(venue, True)
    # Non-physical negative realized = stale/wrong matched mid on a thin cell.
    # Applies to every venue (incl. feed-limited Pacifica) — suppressed, not ranked.
    bad_realized = real < NEG_REALIZED_LIMIT
    suspect = (not feed_limited) and (
        (touch_real and not touch_adv) or gap < -EGREGIOUS_NEG or real < -0.10)
    return {
        "venue": venue, "conf": CONF_TAG[METHOD[venue]], "n": n,
        "real": real, "adv": adv, "gap": gap,
        "age": median(x["book_age_ms"] for x in ms),
        "at_touch": touch_adv, "feed_limited": feed_limited,
        "sparse": sparse, "suspect": suspect, "bad_realized": bad_realized,
        "rankable": not sparse and not suspect and not bad_realized,
    }


def display_order(cells):
    # rankable (by realized) first, then suspect, then sparse
    def k(c):
        bucket = 0 if c["rankable"] else (1 if c["suspect"] else 2)
        return (bucket, c["real"])
    return sorted(cells, key=k)


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

    print("\nLEAGUE TABLE — advertised vs realized execution cost (bps)")
    print(f"window: last {args.hours:.0f}h   clip-window(HL/Pac): {args.window_ms}ms   "
          f"sparse: n<{args.min_samples}")
    print("gap = realized − advertised  (+ = filled worse than the book advertised)")

    coverage_all = {}
    hdr = (f"  {'rung':>5}  {'':1} {'venue':<12} {'conf':>5} {'n':>5} "
           f"{'real':>6} {'adv':>6} {'gap':>6} {'age':>5}  note")

    for coin in args.assets:
        data, floors = {}, {}
        for v in args.venues:
            per_rung, cov = collect(conn, v, coin, since_ns, args.window_ms)
            data[v] = per_rung
            coverage_all[(coin, v)] = cov
            floors[v] = touch_floor_bps(conn, v, coin, since_ns)

        eligible = [v for v in args.venues if coverage_all[(coin, v)]["prints"] > 0]
        if not eligible:
            continue
        floor_str = "  ".join(
            f"{v} {floors[v]:.2f}" for v in eligible if floors[v] is not None)
        print("\n" + "═" * 78)
        print(f" {coin}    touch floor (bps): {floor_str}")
        print("═" * 78)
        print(hdr)

        for rung in RUNGS:
            cells = [build_cell(v, data[v][rung], floors[v], args.min_samples)
                     for v in eligible if data[v].get(rung)]
            if not cells:
                continue
            rankable = sorted((c for c in cells if c["rankable"]), key=lambda c: c["real"])
            leader = rankable[0] if rankable else None
            tie = (leader is not None and len(rankable) > 1
                   and rankable[1]["real"] - leader["real"] < TIE_EPS)

            label = rung_label(rung)
            for c in display_order(cells):
                is_leader = c is leader
                mark = "►" if is_leader else " "
                notes = []
                if is_leader:
                    notes.append("cheapest at size" + (" (≈tie)" if tie else ""))
                if c["at_touch"]:
                    notes.append("at touch")
                if c["suspect"]:
                    notes.append("beats book?")
                if c["feed_limited"] and not c["bad_realized"]:
                    notes.append("gap feed-limited")
                if c["bad_realized"]:
                    notes.append("thin/stale (realized non-physical)")
                if c["sparse"]:
                    notes.append("sparse")
                note = ("· " + ", ".join(notes)) if notes else ""
                if c["bad_realized"]:
                    real_s, adv_s, gap_s = "   n/a", "   n/a", "    n/a"
                else:
                    real_s = f"{c['real']:>6.2f}"
                    # feed-limited venues: realized is real, adv/gap aren't, so flag them.
                    # gap floored at 0 (a taker can't beat the pre-trade book; sub-zero is
                    # book-age noise).
                    adv_s = f"{c['adv']:>6.2f}" if not c["feed_limited"] else "  ~fl"
                    gap_s = f"{max(c['gap'], 0):>6.2f}" if not c["feed_limited"] else "   ~fl"
                print(f"  {label:>5}  {mark:1} {c['venue']:<12} {c['conf']:>5} "
                      f"{c['n']:>5} {real_s} {adv_s} "
                      f"{gap_s} {c['age']:>5.0f}  {note}")
                label = ""  # rung label only on the block's first row

    print("\n" + "═" * 78)
    print(" COVERAGE & CONFIDENCE  (per venue, across the assets above)")
    print("═" * 78)
    print(f"  {'venue':<12} {'method':>10} {'clips':>8} {'measured':>9} "
          f"{'unmatched':>10} {'depth-cut':>10}")
    for v in args.venues:
        c = [cov for (cn, vv), cov in coverage_all.items() if vv == v]
        clips = sum(x["clips"] for x in c)
        if clips == 0:
            continue
        meas = sum(x["measured"] for x in c)
        unm = sum(x["skips"].get("no book within 1s", 0)
                  + x["skips"].get("empty book", 0) for x in c)
        depth = sum(x["skips"].get("exceeds visible depth", 0) for x in c)
        print(f"  {v:<12} {METHOD[v]:>10} {clips:>8} {meas:>9} {unm:>10} {depth:>10}")

    print("\n  ► cheapest realized among trustworthy, comparable cells")
    print("  gap         = realized minus advertised, floored at 0 (a taker can't beat the")
    print("                pre-trade book; sub-zero is book-age noise)")
    print("  at touch    = clip fills at the best level; cost is the spread (tick-limited)")
    print("  beats book? = realized pinned at the touch floor while the book advertised")
    print("                depth (post-trade match), or gap < -0.5bps; artifact, not ranked")
    print("  sparse      = n below threshold; shown for transparency, never ranked")
    print("  thin/stale  = realized went non-physical (< -0.1bps, filled better than a stale")
    print("                mid) on a thin cell — suppressed (n/a), not ranked")
    print("  ~fl / gap feed-limited = Pacifica's book refreshes faster than its snapshot feed,")
    print("                so its advertised/gap is under-measured; realized is still reliable")
    print("  depth-cut   = clips larger than the venue's visible book (excluded, not extrapolated)")
    print("  conf = how each venue's many small fills are grouped back into one trader's order:")
    print("    exact = Lighter publishes the taker order id, so every fill is grouped precisely")
    print("    ident = HL publishes the taker but not order ids; fills from one taker in a short")
    print("            window are read as one clip")
    print("    heur  = Pacifica publishes neither, so a fast same-side run of trades is read as a sweep")


if __name__ == "__main__":
    main()
