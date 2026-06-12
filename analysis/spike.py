"""Spike analysis — clip reconstruction + advertised-vs-realized gap (spec §4, §7).

Turns the stored print stream into clips, matches each clip against the most
recent BookSnapshot at or before the clip's first print (tolerance 1s, spec
§3), and computes realized vs advertised cost in bps with book_age_ms attached.

Usage: python -m analysis.spike --db data/spike.db --venue hyperliquid --coin BTC
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

CONFIDENCE = {"lighter": "exact", "hyperliquid": "identity", "pacifica": "heuristic"}

# Clip-size rungs (PRD §5.1). A clip populates a rung only if its notional is
# within the geometric neighborhood of that rung (boundaries at geometric
# midpoints between adjacent rungs); tiny clips stay unbucketed rather than
# inflating the $10k rung.
RUNGS = [10_000, 25_000, 100_000, 500_000, 1_000_000, 5_000_000]


def rung_for(notional: float) -> int | None:
    lo = (RUNGS[0] * RUNGS[0] / RUNGS[1]) ** 0.5  # same half-width below the ladder
    hi = (RUNGS[-1] * RUNGS[-1] * RUNGS[1] / RUNGS[0]) ** 0.5
    if not (lo <= notional <= hi):
        return None
    best = min(RUNGS, key=lambda r: abs((notional / r) if notional > r else (r / notional)))
    return best


@dataclass
class Clip:
    venue: str
    coin: str
    side: str
    taker_id: str | None
    confidence: str
    prints: list = field(default_factory=list)  # (ts_ns, price, size)

    @property
    def start_ns(self) -> int:
        return self.prints[0][0]

    @property
    def size_base(self) -> Decimal:
        return sum((p[2] for p in self.prints), Decimal(0))

    @property
    def notional(self) -> Decimal:
        return sum((p[1] * p[2] for p in self.prints), Decimal(0))

    @property
    def realized_vwap(self) -> Decimal:
        return self.notional / self.size_base


def reconstruct_clips(rows, venue: str, coin: str, window_ms: int) -> list[Clip]:
    """rows: (ts_ns, price, size_base, aggressor_side, taker_id) sorted by ts.

    With taker identity (HL, Lighter): group by (taker_id, side), closing a
    clip when the taker's next same-direction print is > window_ms away.
    Interleaved prints from other takers don't break a clip.
    Without identity (Pacifica): sweep heuristic — consecutive same-side
    prints within window_ms form one clip; any opposite-side print breaks it.
    """
    window_ns = window_ms * 1_000_000
    confidence = CONFIDENCE.get(venue, "heuristic")
    clips: list[Clip] = []

    has_identity = any(r[4] for r in rows)
    if has_identity:
        open_clips: dict[tuple, Clip] = {}
        for ts, px, sz, side, taker in rows:
            if side is None:
                continue
            key = (taker, side)
            clip = open_clips.get(key)
            if clip is not None and ts - clip.prints[-1][0] > window_ns:
                clips.append(clip)
                clip = None
            if clip is None:
                clip = Clip(venue, coin, side, taker, confidence)
                open_clips[key] = clip
            clip.prints.append((ts, Decimal(px), Decimal(sz)))
        clips.extend(open_clips.values())
    else:
        current: Clip | None = None
        for ts, px, sz, side, _ in rows:
            if side is None:
                continue
            if current is not None and (
                side != current.side or ts - current.prints[-1][0] > window_ns
            ):
                clips.append(current)
                current = None
            if current is None:
                current = Clip(venue, coin, side, None, confidence)
            current.prints.append((ts, Decimal(px), Decimal(sz)))
        if current is not None:
            clips.append(current)

    clips.sort(key=lambda c: c.start_ns)
    return clips


def walk_book(levels: list[list[str]], size: Decimal) -> Decimal | None:
    """VWAP of filling `size` against [[price, size], ...]; None if book too thin."""
    remaining, cost = size, Decimal(0)
    for px, sz in levels:
        take = min(remaining, Decimal(sz))
        cost += take * Decimal(px)
        remaining -= take
        if remaining == 0:
            return cost / size
    return None


def cost_bps(vwap: Decimal, mid: Decimal, side: str) -> float:
    signed = (vwap - mid) if side == "buy" else (mid - vwap)
    return float(signed / mid * 10_000)


def match_and_measure(conn, clips, tolerance_ms: int):
    """For each clip find the book snapshot per spec §3 and compute the gap."""
    results, unmatched, too_thin = [], 0, 0
    cache: dict[str, tuple[list[int], list[int]]] = {}

    def snap_index(venue, coin):
        if venue + coin not in cache:
            rows = conn.execute(
                "SELECT ts_ns, id FROM book_snapshots WHERE venue=? AND coin=? ORDER BY ts_ns",
                (venue, coin),
            ).fetchall()
            cache[venue + coin] = ([r[0] for r in rows], [r[1] for r in rows])
        return cache[venue + coin]

    for clip in clips:
        ts_list, id_list = snap_index(clip.venue, clip.coin)
        i = bisect_right(ts_list, clip.start_ns) - 1
        if i < 0 or clip.start_ns - ts_list[i] > tolerance_ms * 1_000_000:
            unmatched += 1
            continue
        bids_j, asks_j = conn.execute(
            "SELECT bids, asks FROM book_snapshots WHERE id=?", (id_list[i],)
        ).fetchone()
        bids, asks = json.loads(bids_j), json.loads(asks_j)
        if not bids or not asks:
            unmatched += 1
            continue
        mid = (Decimal(bids[0][0]) + Decimal(asks[0][0])) / 2
        adv_vwap = walk_book(asks if clip.side == "buy" else bids, clip.size_base)
        book_age_ms = (clip.start_ns - ts_list[i]) / 1e6
        if adv_vwap is None:
            too_thin += 1
            continue
        results.append({
            "clip": clip,
            "notional": float(clip.notional),
            "advertised_bps": cost_bps(adv_vwap, mid, clip.side),
            "realized_bps": cost_bps(clip.realized_vwap, mid, clip.side),
            "book_age_ms": book_age_ms,
            "rung": rung_for(float(clip.notional)),
        })
    return results, unmatched, too_thin


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/fills.db")
    p.add_argument("--venue", default="hyperliquid")
    p.add_argument("--coin", default="BTC")
    p.add_argument("--clip-window-ms", type=int, default=200)  # spec §4, [OPEN] calibrate
    p.add_argument("--tolerance-ms", type=int, default=1000)   # spec §3
    p.add_argument("--top", type=int, default=8, help="largest clips to detail")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT ts_ns, price, size_base, aggressor_side, taker_id FROM trades "
        "WHERE venue=? AND coin=? AND is_liquidation=0 ORDER BY ts_ns",
        (args.venue, args.coin),
    ).fetchall()
    if not rows:
        print(f"no trades for {args.venue}/{args.coin} in {args.db}")
        return

    clips = reconstruct_clips(rows, args.venue, args.coin, args.clip_window_ms)
    results, unmatched, too_thin = match_and_measure(conn, clips, args.tolerance_ms)

    multi = sum(1 for c in clips if len(c.prints) > 1)
    print(f"{args.venue}/{args.coin}: {len(rows)} prints -> {len(clips)} clips "
          f"({multi} multi-print), confidence={CONFIDENCE.get(args.venue)}")
    print(f"matched {len(results)}, unmatched(no book within {args.tolerance_ms}ms): "
          f"{unmatched}, clip exceeds visible depth: {too_thin}\n")

    by_rung: dict[int | None, list[dict]] = {}
    for r in results:
        by_rung.setdefault(r["rung"], []).append(r)
    print(f"{'rung':>10} {'n':>5} {'med adv bps':>12} {'med real bps':>13} "
          f"{'med gap bps':>12} {'med age ms':>11}")
    for rung in RUNGS:
        rs = by_rung.get(rung)
        if not rs:
            print(f"{rung:>10,} {0:>5}     (no clips of ~this size observed)")
            continue
        adv = median(x["advertised_bps"] for x in rs)
        real = median(x["realized_bps"] for x in rs)
        gap = median(x["realized_bps"] - x["advertised_bps"] for x in rs)
        age = median(x["book_age_ms"] for x in rs)
        print(f"{rung:>10,} {len(rs):>5} {adv:>12.2f} {real:>13.2f} {gap:>12.2f} {age:>11.0f}")
    small = by_rung.get(None, [])
    if small:
        print(f"{'<ladder':>10} {len(small):>5}  (below $10k neighborhood, not bucketed)")

    print(f"\nlargest {args.top} clips (eyeball check, spec §7):")
    for r in sorted(results, key=lambda x: -x["notional"])[: args.top]:
        c = r["clip"]
        print(f"  ${r['notional']:>12,.0f} {c.side:<4} {len(c.prints):>3} prints  "
              f"adv {r['advertised_bps']:>7.2f}bps  real {r['realized_bps']:>7.2f}bps  "
              f"gap {r['realized_bps'] - r['advertised_bps']:>7.2f}bps  "
              f"book_age {r['book_age_ms']:>5.0f}ms  taker {str(c.taker_id)[:10]}")


if __name__ == "__main__":
    main()
