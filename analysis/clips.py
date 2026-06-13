"""Single-venue clip calibration + audit + advertised-vs-realized gap.

Venue-aware via analysis.reconstruct:
  - lighter (exact): clips = taker order id; reports order-partition stats
    (window-free, so no window to calibrate).
  - hyperliquid (identity) / pacifica (heuristic): reports the intra-burst gap
    distribution + a window sweep so the time window is chosen from data.

Usage:
  python -m analysis.clips --db data/fills.db --venue hyperliquid --coin BTC --hours 4
  python -m analysis.clips --db data/fills.db --venue lighter --coin BTC --hours 4
  python -m analysis.clips --db data/fills.db --venue pacifica --coin BTC --window-ms 150
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from statistics import median

from analysis.reconstruct import (
    METHOD, RUNGS, BookMatcher, group_clips, load_trades, measure, rung_for,
)

SWEEP_WINDOWS_MS = [50, 100, 150, 200, 350, 500, 1000]


def pctile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(p / 100 * len(sorted_vals)))]


def section(t):
    print("\n" + "=" * 76 + "\n" + t + "\n" + "=" * 76)


def intra_burst_gaps(rows):
    """Gaps (ms) between consecutive same-(taker,side) prints — identity/heuristic."""
    by_key = {}
    for ts, _px, _sz, side, taker, _tid, _oid in rows:
        if side is None or taker is None:
            continue
        by_key.setdefault((taker, side), []).append(ts)
    gaps = []
    for ts_list in by_key.values():
        ts_list.sort()
        gaps += [(b - a) / 1e6 for a, b in zip(ts_list, ts_list[1:])]
    return sorted(gaps)


def calibrate_window(rows):
    section("1. INTRA-BURST GAPS  (ms between consecutive same-taker+side prints)")
    gaps = intra_burst_gaps(rows)
    if not gaps:
        print("  (no taker identity in this venue's prints)")
        return
    buckets = [(0, 5), (5, 20), (20, 50), (50, 100), (100, 250),
               (250, 500), (500, 1000), (1000, float("inf"))]
    print(f"  {'bucket(ms)':>14} {'count':>8} {'cumulative%':>12}")
    cum = 0
    for lo, hi in buckets:
        n = sum(1 for g in gaps if lo <= g < hi)
        cum += n
        label = f"{lo}-{'inf' if hi == float('inf') else int(hi)}"
        print(f"  {label:>14} {n:>8} {100*cum/len(gaps):>11.1f}%")
    print(f"  percentiles: p50={pctile(gaps,50):.0f} p75={pctile(gaps,75):.0f} "
          f"p90={pctile(gaps,90):.0f} p95={pctile(gaps,95):.0f}ms")

    section("2. WINDOW SWEEP")
    print(f"  {'win(ms)':>8} {'clips':>7} {'multi%':>7} {'maxPrints':>10} "
          f"{'largest$':>13} {'clips>1s span':>13}")
    venue, coin = ROW_VENUE, ROW_COIN
    for w in SWEEP_WINDOWS_MS:
        clips = group_clips(rows, venue, coin, w)
        multi = sum(1 for c in clips if len(c.prints) > 1)
        over1s = sum(1 for c in clips if c.span_ms > 1000)
        largest = max((float(c.notional) for c in clips), default=0)
        maxp = max((len(c.prints) for c in clips), default=0)
        print(f"  {w:>8} {len(clips):>7} {100*multi/max(len(clips),1):>6.1f}% "
              f"{maxp:>10} {largest:>13,.0f} {over1s:>13}")
    print("  clips should drop then flatten; if largest$ / clips>1s keep climbing,\n"
          "  that's lumping distinct orders.")


def order_partition_stats(clips, rows):
    section("1-2. ORDER-ID PARTITION  (exact method — window-free)")
    n_rows = len(rows)
    n_with_id = sum(1 for r in rows if r[6] and r[3] is not None)
    fills = sorted(len(c.prints) for c in clips)
    spans = sorted(c.span_ms for c in clips)
    print(f"  prints: {n_rows}, with usable order id: {n_with_id} "
          f"({100*n_with_id/max(n_rows,1):.1f}%)")
    print(f"  clips (= distinct orders): {len(clips)}")
    if fills:
        print(f"  fills per order: median={pctile(fills,50)} p90={pctile(fills,90)} "
              f"max={fills[-1]}")
        print(f"  order span (first->last fill): median={pctile(spans,50):.1f}ms "
              f"p90={pctile(spans,90):.1f}ms max={spans[-1]:.0f}ms")
    print("  no time window involved: the order id is the clip boundary.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--venue", default="hyperliquid")
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--window-ms", type=int, default=None)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    global ROW_VENUE, ROW_COIN
    ROW_VENUE, ROW_COIN = args.venue, args.coin
    method = METHOD.get(args.venue, "heuristic")

    conn = sqlite3.connect(args.db)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)
    rows = load_trades(conn, args.venue, args.coin, since_ns)
    if not rows:
        print(f"no {args.venue}/{args.coin} trades in last {args.hours}h")
        return
    span_h = (rows[-1][0] - rows[0][0]) / 3.6e12
    print(f"{args.venue}/{args.coin} [{method}]: {len(rows)} prints over {span_h:.1f}h")

    clips = group_clips(rows, args.venue, args.coin, args.window_ms)
    if method == "exact":
        order_partition_stats(clips, rows)
    else:
        calibrate_window(rows)

    win = args.window_ms if args.window_ms is not None else 150
    wlabel = "n/a (order id)" if method == "exact" else f"{win}ms"
    section(f"3. GROUPED CLIPS @ window={wlabel}  (top {args.top} by notional)")
    matcher = BookMatcher(conn, args.venue, args.coin)
    measured, skips = [], {}
    for c in clips:
        r = measure(c, matcher)
        if "skip" in r:
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1
        else:
            measured.append((c, r))
    print(f"  {len(clips)} clips, {len(measured)} measured; skipped: {skips or 'none'}\n")
    print(f"  {'time(UTC)':>13} {'side':>4} {'prints':>6} {'size':>11} "
          f"{'notional$':>13} {'real bps':>9} {'adv bps':>9} {'gap bps':>9} "
          f"{'age ms':>7} {'key':>14}")
    top_clips = sorted(measured, key=lambda x: -float(x[0].notional))[: args.top]
    for c, r in top_clips:
        hhmm = time.strftime("%H:%M:%S", time.gmtime(c.start_ns / 1e9))
        print(f"  {hhmm:>13} {c.side:>4} {len(c.prints):>6} {float(c.size):>11.4f} "
              f"{float(c.notional):>13,.0f} {r['realized_bps']:>9.2f} "
              f"{r['advertised_bps']:>9.2f} {r['gap_bps']:>+9.2f} "
              f"{r['book_age_ms']:>7.0f} {str(c.key)[:14]:>14}")

    section("4. GROUPING AUDIT  (prints inside the 3 largest clips)")
    for c, r in top_clips[:3]:
        print(f"\n  clip[{c.method}]: {c.side} {len(c.prints)} prints  "
              f"${float(c.notional):,.0f}  span {c.span_ms:.0f}ms  key {str(c.key)[:16]}")
        gaps_in = [0.0] + [(c.prints[i][0] - c.prints[i-1][0]) / 1e6
                           for i in range(1, len(c.prints))]
        for (ts, px, sz, tid), g in list(zip(c.prints, gaps_in))[:10]:
            print(f"    +{g:>7.2f}ms  px={float(px):>10.1f}  sz={float(sz):>10.5f}")
        if len(c.prints) > 10:
            print(f"    ... {len(c.prints) - 10} more prints")
        print(f"    max internal gap: {max(gaps_in):.2f}ms")

    section("5. GAP BY CLIP-SIZE BUCKET  (the §7 intuition check)")
    by_rung = {}
    for c, r in measured:
        by_rung.setdefault(rung_for(float(c.notional)), []).append(r)
    print(f"  {'rung':>12} {'n':>5} {'med real':>9} {'med adv':>9} "
          f"{'med gap':>9} {'p90 gap':>9}")
    for rung in RUNGS:
        sub = by_rung.get(rung)
        if not sub:
            print(f"  {rung:>12,} {0:>5}")
            continue
        gaps_b = sorted(x["gap_bps"] for x in sub)
        print(f"  {rung:>12,} {len(sub):>5} "
              f"{median(x['realized_bps'] for x in sub):>9.2f} "
              f"{median(x['advertised_bps'] for x in sub):>9.2f} "
              f"{median(gaps_b):>+9.2f} {pctile(gaps_b,90):>+9.2f}")


if __name__ == "__main__":
    main()
