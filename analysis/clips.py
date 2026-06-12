"""Clip reconstruction + advertised-vs-realized gap on GROUPED clips — the
real spec §7 gate (supersedes spike.py's single-print Check-4 proxy).

Group consecutive HL prints sharing the same taker address (from users[],
stored as taker_id) and same direction within a time window into one clip,
fill the clip's *total* size against the book snapshot immediately prior
(the §3 matching rule), and compare advertised vs realized cost in bps.

Window calibration is empirical, not guessed: HL clips are matching-engine
sweeps — many prints a few ms apart, then a long quiet gap before that
taker's next distinct order. So the right window sits in the *valley* of the
distribution of gaps between consecutive same-(taker,side) prints. This tool
prints that distribution and a sweep across candidate windows so the choice
is settled by looking at real output.

Usage:
  python -m analysis.clips --db data/fills.db --coin BTC --hours 4
  python -m analysis.clips --db data/fills.db --coin BTC --window-ms 150
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

from analysis.spike import cost_bps, walk_book
from collector.models import unjson

TOLERANCE_NS = 1_000_000_000  # §3 matching tolerance: 1s
SWEEP_WINDOWS_MS = [50, 100, 150, 200, 350, 500, 1000]


@dataclass
class Clip:
    taker: str
    side: str  # "buy" | "sell"
    prints: list = field(default_factory=list)  # (ts_ns, Decimal px, Decimal sz, trade_id)

    @property
    def start_ns(self) -> int:
        return self.prints[0][0]

    @property
    def end_ns(self) -> int:
        return self.prints[-1][0]

    @property
    def size(self) -> Decimal:
        return sum((p[2] for p in self.prints), Decimal(0))

    @property
    def notional(self) -> Decimal:
        return sum((p[1] * p[2] for p in self.prints), Decimal(0))

    @property
    def vwap(self) -> Decimal:
        return self.notional / self.size

    @property
    def span_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6


def group_clips(rows, window_ms: int) -> list[Clip]:
    """rows: (ts_ns, price, size, side, taker, trade_id) sorted by ts_ns asc.

    Group by (taker, side). A clip closes when that taker's next same-side
    print arrives more than window_ms after the previous one. Prints from
    other takers interleaved in time do NOT break a clip — identity is what
    isolates the order, which is the whole advantage of HL's users[] over a
    blind print-sweep.
    """
    window_ns = window_ms * 1_000_000
    open_clips: dict[tuple, Clip] = {}
    done: list[Clip] = []
    for ts, px, sz, side, taker, tid in rows:
        if side is None or taker is None:
            continue
        key = (taker, side)
        clip = open_clips.get(key)
        if clip is not None and ts - clip.prints[-1][0] > window_ns:
            done.append(clip)
            clip = None
        if clip is None:
            clip = Clip(taker, side)
            open_clips[key] = clip
        clip.prints.append((ts, Decimal(str(px)), Decimal(str(sz)), tid))
    done.extend(open_clips.values())
    done.sort(key=lambda c: c.start_ns)
    return done


def intra_burst_gaps(rows) -> list[float]:
    """Gaps (ms) between consecutive same-(taker,side) prints, window-free.
    The shape of this is what justifies the window choice."""
    by_key: dict[tuple, list[int]] = {}
    for ts, _px, _sz, side, taker, _tid in rows:
        if side is None or taker is None:
            continue
        by_key.setdefault((taker, side), []).append(ts)
    gaps = []
    for ts_list in by_key.values():
        ts_list.sort()
        for a, b in zip(ts_list, ts_list[1:]):
            gaps.append((b - a) / 1e6)
    return gaps


class BookMatcher:
    def __init__(self, conn, venue, coin):
        rows = conn.execute(
            "SELECT ts_ns, id FROM book_snapshots WHERE venue=? AND coin=? ORDER BY ts_ns",
            (venue, coin),
        ).fetchall()
        self.ts = [r[0] for r in rows]
        self.ids = [r[1] for r in rows]
        self.conn = conn

    def match(self, start_ns: int):
        """Latest snapshot with ts_ns <= start_ns within tolerance. Returns
        (bids, asks, book_age_ms) or None."""
        i = bisect_right(self.ts, start_ns) - 1
        if i < 0 or start_ns - self.ts[i] > TOLERANCE_NS:
            return None
        bids_b, asks_b = self.conn.execute(
            "SELECT bids, asks FROM book_snapshots WHERE id=?", (self.ids[i],)
        ).fetchone()
        return unjson(bids_b), unjson(asks_b), (start_ns - self.ts[i]) / 1e6


def measure(clip: Clip, matcher: BookMatcher):
    """Returns dict with advertised/realized/gap bps + book_age_ms, or a
    reason string if it can't be measured."""
    m = matcher.match(clip.start_ns)
    if m is None:
        return {"skip": "no book within 1s"}
    bids, asks, age = m
    if not bids or not asks:
        return {"skip": "empty book"}
    mid = (Decimal(bids[0][0]) + Decimal(asks[0][0])) / 2
    book = asks if clip.side == "buy" else bids
    adv_vwap = walk_book(book, clip.size)
    if adv_vwap is None:
        return {"skip": "exceeds visible depth"}
    realized = cost_bps(clip.vwap, mid, clip.side)
    advertised = cost_bps(adv_vwap, mid, clip.side)
    return {
        "advertised_bps": advertised,
        "realized_bps": realized,
        "gap_bps": realized - advertised,
        "book_age_ms": age,
    }


def pctile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(p / 100 * len(sorted_vals)))]


def section(t):
    print("\n" + "=" * 76 + "\n" + t + "\n" + "=" * 76)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--venue", default="hyperliquid")
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--window-ms", type=int, default=150)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)
    rows = conn.execute(
        "SELECT ts_ns, price, size_base, aggressor_side, taker_id, trade_id "
        "FROM trades WHERE venue=? AND coin=? AND is_liquidation=0 AND ts_ns>=? "
        "ORDER BY ts_ns",
        (args.venue, args.coin, since_ns),
    ).fetchall()
    if not rows:
        print(f"no {args.venue}/{args.coin} trades in last {args.hours}h")
        return
    takers = {r[4] for r in rows if r[4]}
    span_h = (rows[-1][0] - rows[0][0]) / 3.6e12
    print(f"{args.venue}/{args.coin}: {len(rows)} prints over {span_h:.1f}h, "
          f"{len(takers)} distinct takers")

    # 1. intra-burst gap distribution — justifies the window
    section("1. INTRA-BURST GAPS  (ms between consecutive same-taker+side prints)")
    gaps = sorted(intra_burst_gaps(rows))
    if gaps:
        buckets = [(0, 5), (5, 20), (20, 50), (50, 100), (100, 250),
                   (250, 500), (500, 1000), (1000, float("inf"))]
        print(f"  {'bucket(ms)':>14} {'count':>8} {'cumulative%':>12}")
        cum = 0
        for lo, hi in buckets:
            n = sum(1 for g in gaps if lo <= g < hi)
            cum += n
            label = f"{lo}-{'inf' if hi == float('inf') else int(hi)}"
            print(f"  {label:>14} {n:>8} {100*cum/len(gaps):>11.1f}%")
        print(f"  percentiles: p50={pctile(gaps,50):.0f}  p75={pctile(gaps,75):.0f}  "
              f"p90={pctile(gaps,90):.0f}  p95={pctile(gaps,95):.0f}ms")
        print("  -> a window in the valley after the dense low-gap mass groups bursts\n"
              "     without bridging into the taker's next distinct order.")

    # 2. window sweep — how grouping behaves as the window grows
    section("2. WINDOW SWEEP")
    print(f"  {'win(ms)':>8} {'clips':>7} {'multi%':>7} {'maxPrints':>10} "
          f"{'largest$':>13} {'p95 span(ms)':>13} {'clips>1s span':>13}")
    for w in SWEEP_WINDOWS_MS:
        clips = group_clips(rows, w)
        multi = [c for c in clips if len(c.prints) > 1]
        spans = sorted(c.span_ms for c in clips)
        over1s = sum(1 for c in clips if c.span_ms > 1000)
        largest = max((float(c.notional) for c in clips), default=0)
        maxp = max((len(c.prints) for c in clips), default=0)
        print(f"  {w:>8} {len(clips):>7} {100*len(multi)/max(len(clips),1):>6.1f}% "
              f"{maxp:>10} {largest:>13,.0f} {pctile(spans,95):>13.0f} {over1s:>13}")
    print("  reading it: clips should DROP then flatten as window grows; if largest$\n"
          "  and 'clips>1s span' keep climbing past the valley, that's lumping.")

    # 3. clip table at chosen window
    section(f"3. GROUPED CLIPS @ window={args.window_ms}ms  (top {args.top} by notional)")
    clips = group_clips(rows, args.window_ms)
    matcher = BookMatcher(conn, args.venue, args.coin)
    measured, skips = [], {}
    for c in clips:
        r = measure(c, matcher)
        if "skip" in r:
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1
        else:
            measured.append((c, r))
    print(f"  {len(clips)} clips, {len(measured)} measured; "
          f"skipped: {skips or 'none'}\n")
    print(f"  {'time(UTC)':>13} {'side':>4} {'prints':>6} {'size':>10} "
          f"{'notional$':>13} {'real bps':>9} {'adv bps':>9} {'gap bps':>9} "
          f"{'age ms':>7} {'taker':>12}")
    top_clips = sorted(measured, key=lambda x: -float(x[0].notional))[: args.top]
    for c, r in top_clips:
        hhmm = time.strftime("%H:%M:%S", time.gmtime(c.start_ns / 1e9))
        print(f"  {hhmm:>13} {c.side:>4} {len(c.prints):>6} {float(c.size):>10.4f} "
              f"{float(c.notional):>13,.0f} {r['realized_bps']:>9.2f} "
              f"{r['advertised_bps']:>9.2f} {r['gap_bps']:>+9.2f} "
              f"{r['book_age_ms']:>7.0f} {str(c.taker)[:12]:>12}")

    # 4. audit — the prints behind the largest clips (is grouping clean?)
    section("4. GROUPING AUDIT  (prints inside the 3 largest clips)")
    for c, r in top_clips[:3]:
        print(f"\n  clip: {c.side} {len(c.prints)} prints  ${float(c.notional):,.0f}  "
              f"span {c.span_ms:.0f}ms  taker {str(c.taker)[:12]}")
        gaps_in = [0.0] + [(c.prints[i][0] - c.prints[i-1][0]) / 1e6
                           for i in range(1, len(c.prints))]
        for (ts, px, sz, tid), g in list(zip(c.prints, gaps_in))[:12]:
            print(f"    +{g:>6.1f}ms  px={float(px):>10.1f}  sz={float(sz):>9.5f}")
        if len(c.prints) > 12:
            print(f"    ... {len(c.prints) - 12} more prints")
        maxgap = max(gaps_in) if gaps_in else 0
        flag = "  <-- internal gap near window; possible split point" if maxgap > args.window_ms * 0.8 else ""
        print(f"    max internal gap: {maxgap:.1f}ms{flag}")

    # 5. does the gap widen with size?
    section("5. GAP BY CLIP-SIZE BUCKET  (the §7 intuition check)")
    rungs = [(0, 25_000), (25_000, 100_000), (100_000, 500_000),
             (500_000, 1_000_000), (1_000_000, 5_000_000), (5_000_000, float("inf"))]
    print(f"  {'notional bucket':>22} {'n':>5} {'med real':>9} {'med adv':>9} "
          f"{'med gap':>9} {'p90 gap':>9}")
    for lo, hi in rungs:
        sub = [r for c, r in measured if lo <= float(c.notional) < hi]
        if not sub:
            print(f"  {f'${lo:,.0f}-{hi:,.0f}' if hi != float('inf') else f'>${lo:,.0f}':>22} "
                  f"{0:>5}")
            continue
        gaps_b = sorted(x["gap_bps"] for x in sub)
        label = f"${lo:,.0f}-{hi:,.0f}" if hi != float("inf") else f">${lo:,.0f}"
        print(f"  {label:>22} {len(sub):>5} "
              f"{median(x['realized_bps'] for x in sub):>9.2f} "
              f"{median(x['advertised_bps'] for x in sub):>9.2f} "
              f"{median(gaps_b):>+9.2f} {pctile(gaps_b,90):>+9.2f}")


if __name__ == "__main__":
    main()
