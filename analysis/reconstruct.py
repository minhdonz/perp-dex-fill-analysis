"""Canonical clip reconstruction + advertised-vs-realized measurement, shared
by the calibration tool (clips.py) and the league table (league.py).

Per-venue method and the confidence label it earns (PRD §5.2, spec §4):
  - lighter     -> "exact"     : group by taker order id (bid_id/ask_id). One
                                  market order = one order id across all its
                                  fills; window-free, verified live.
  - hyperliquid -> "identity"  : group by taker address + direction within a
                                  short window (no public order id).
  - pacifica    -> "heuristic" : sweep consecutive same-direction prints (no
                                  taker id at all).

Advertised cost = walk the matched book snapshot (most recent with ts_ns <=
clip start, within 1s) for the clip's full size; realized = the clip's actual
fill VWAP; both expressed in bps vs the snapshot mid, sign so + = worse.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal

from collector.models import unjson

TOLERANCE_NS = 1_000_000_000  # §3 matching tolerance: 1s

METHOD = {"lighter": "exact", "hyperliquid": "identity", "pacifica": "heuristic"}
DEFAULT_WINDOW_MS = {"hyperliquid": 150, "pacifica": 150}  # ignored for exact

# Is the venue's public book snapshot a faithful basis for the *advertised* leg?
# Pacifica's off-chain sub-10ms engine refreshes the touch far faster than its
# ~4/s book feed (verified 2026-06-14: large clips fill 2-4x the displayed touch
# even against <150ms-fresh snapshots, and it has no hidden/iceberg order types).
# So its advertised-vs-realized GAP is structurally under-measured from snapshots
# and is flagged feed-limited; its REALIZED cost is still trustworthy.
BOOK_RELIABLE = {"hyperliquid": True, "lighter": True, "pacifica": False}

# Realized cost below this (bps) is non-physical — a taker can't fill better than
# the mid that existed before it, beyond small book-age drift. A clearly-negative
# realized means the matched book's mid was stale/wrong (thin, illiquid cell), so
# the number is suppressed everywhere rather than shown as a finding or ranked.
NEG_REALIZED_LIMIT = -0.10

RUNGS = [10_000, 25_000, 100_000, 500_000, 1_000_000, 5_000_000]


def rung_for(notional: float) -> int | None:
    """Nearest ladder rung in log space; None outside the ladder's range so a
    sub-$10k or >$5M clip isn't forced onto an end rung (PRD §5.1)."""
    if notional <= 0:
        return None
    low = RUNGS[0] / math.sqrt(RUNGS[1] / RUNGS[0])
    high = RUNGS[-1] * math.sqrt(RUNGS[-1] / RUNGS[-2])
    if notional < low or notional > high:
        return None
    edges = [math.sqrt(a * b) for a, b in zip(RUNGS, RUNGS[1:])]
    for r, e in zip(RUNGS, edges):
        if notional < e:
            return r
    return RUNGS[-1]


@dataclass
class Clip:
    venue: str
    coin: str
    side: str            # "buy" | "sell"
    key: str             # order id / taker addr / "sweep" — for audit
    method: str          # exact | identity | heuristic
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


def load_trades(conn, venue: str, coin: str, since_ns: int, until_ns: int | None = None):
    if until_ns is None:
        return conn.execute(
            "SELECT ts_ns, price, size_base, aggressor_side, taker_id, trade_id, taker_order_id "
            "FROM trades WHERE venue=? AND coin=? AND is_liquidation=0 AND ts_ns>=? "
            "ORDER BY ts_ns",
            (venue, coin, since_ns),
        ).fetchall()
    return conn.execute(
        "SELECT ts_ns, price, size_base, aggressor_side, taker_id, trade_id, taker_order_id "
        "FROM trades WHERE venue=? AND coin=? AND is_liquidation=0 AND ts_ns>=? AND ts_ns<? "
        "ORDER BY ts_ns",
        (venue, coin, since_ns, until_ns),
    ).fetchall()


def group_clips(rows, venue: str, coin: str, window_ms: int | None = None) -> list[Clip]:
    method = METHOD.get(venue, "heuristic")
    if method == "exact":
        return _by_order_id(rows, venue, coin)
    w = window_ms if window_ms is not None else DEFAULT_WINDOW_MS.get(venue, 150)
    if method == "identity":
        return _by_identity(rows, venue, coin, w)
    return _by_sweep(rows, venue, coin, w)


def _by_order_id(rows, venue, coin) -> list[Clip]:
    clips: dict[str, Clip] = {}
    for ts, px, sz, side, _taker, tid, oid in rows:
        if not oid or side is None:  # '' from backfill = unparseable; skip
            continue
        c = clips.get(oid)
        if c is None:
            c = Clip(venue, coin, side, oid, "exact")
            clips[oid] = c
        c.prints.append((ts, Decimal(str(px)), Decimal(str(sz)), tid))
    out = list(clips.values())
    out.sort(key=lambda c: c.start_ns)
    return out


def _by_identity(rows, venue, coin, window_ms) -> list[Clip]:
    window_ns = window_ms * 1_000_000
    open_clips: dict[tuple, Clip] = {}
    done: list[Clip] = []
    for ts, px, sz, side, taker, tid, _oid in rows:
        if side is None or taker is None:
            continue
        key = (taker, side)
        c = open_clips.get(key)
        if c is not None and ts - c.prints[-1][0] > window_ns:
            done.append(c)
            c = None
        if c is None:
            c = Clip(venue, coin, side, taker, "identity")
            open_clips[key] = c
        c.prints.append((ts, Decimal(str(px)), Decimal(str(sz)), tid))
    done.extend(open_clips.values())
    done.sort(key=lambda c: c.start_ns)
    return done


def _by_sweep(rows, venue, coin, window_ms) -> list[Clip]:
    window_ns = window_ms * 1_000_000
    cur: Clip | None = None
    done: list[Clip] = []
    for ts, px, sz, side, _taker, tid, _oid in rows:
        if side is None:
            continue
        if cur is not None and (side != cur.side or ts - cur.prints[-1][0] > window_ns):
            done.append(cur)
            cur = None
        if cur is None:
            cur = Clip(venue, coin, side, "sweep", "heuristic")
        cur.prints.append((ts, Decimal(str(px)), Decimal(str(sz)), tid))
    if cur is not None:
        done.append(cur)
    return done


def walk_book(levels, size: Decimal):
    """VWAP to fill `size` against [[price, size], ...]; None if book too thin."""
    remaining, cost = size, Decimal(0)
    for px, sz in levels:
        take = min(remaining, Decimal(sz))
        cost += take * Decimal(px)
        remaining -= take
        if remaining <= 0:
            return cost / size
    return None


def cost_bps(vwap: Decimal, mid: Decimal, side: str) -> float:
    signed = (vwap - mid) if side == "buy" else (mid - vwap)
    return float(signed / mid * 10_000)


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
        i = bisect_right(self.ts, start_ns) - 1
        if i < 0 or start_ns - self.ts[i] > TOLERANCE_NS:
            return None
        bids_b, asks_b = self.conn.execute(
            "SELECT bids, asks FROM book_snapshots WHERE id=?", (self.ids[i],)
        ).fetchone()
        return unjson(bids_b), unjson(asks_b), (start_ns - self.ts[i]) / 1e6


def touch_floor_bps(conn, venue, coin, since_ns, sample=400):
    """Median half-spread (bps) from recent book snapshots = the cost of a clip
    that fills at the best level without consuming depth. Below this, the metric
    can't separate venues on execution quality — the number is just the spread
    (tick-limited). Used to flag 'at touch' cells in the league table."""
    rows = conn.execute(
        "SELECT bids, asks FROM book_snapshots WHERE venue=? AND coin=? AND ts_ns>=? "
        "ORDER BY ts_ns DESC LIMIT ?", (venue, coin, since_ns, sample)
    ).fetchall()
    hs = []
    for b, a in rows:
        bids, asks = unjson(b), unjson(a)
        if not bids or not asks:
            continue
        bb, ba = Decimal(bids[0][0]), Decimal(asks[0][0])
        if ba <= bb:
            continue
        hs.append(float((ba - bb) / 2 / ((bb + ba) / 2) * 10_000))
    if not hs:
        return None
    hs.sort()
    return hs[len(hs) // 2]


def measure(clip: Clip, matcher: BookMatcher):
    """advertised/realized/gap bps + book_age_ms, or {'skip': reason}."""
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
