"""Behavior Under Stress — PRD §6.3.

Detect market-wide liquidation cascades from the OKX liquidation feed
(ext_liquidations; Binance is geo-blocked from our VPS), then measure each of
our venues' execution quality during
the cascade vs its calm baseline. The cascade is defined market-wide (your
absolute-$ threshold), so it doesn't depend on our own venues flagging
liquidations (HL doesn't) — we just measure how they behaved during it.

Cascade event = contiguous run of time-buckets where market-wide liquidation
notional >= --threshold ($), merged across gaps up to --merge-gap.

Per venue+asset during the event we report REALIZED execution cost (the metric
that's trustworthy on every venue, incl. Pacifica) vs the venue's baseline in
the hours before — the degradation is the story. Venues with an integrity gap
during the event are excluded (we don't report our outage as their stress).

  python -m analysis.stress --db data/fills.db --threshold 5e6 --bucket-min 1 --days 7
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from statistics import median

from analysis.reconstruct import (
    BOOK_RELIABLE, BookMatcher, group_clips, load_trades, measure,
)

ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]


def detect_cascades(conn, since_ns, bucket_ns, threshold_usd, merge_gap_ns):
    rows = conn.execute(
        f"SELECT (ts_ns/{bucket_ns})*{bucket_ns} b, SUM(notional_usd) tot "
        "FROM ext_liquidations WHERE ts_ns>=? "
        f"GROUP BY b HAVING tot >= {threshold_usd} ORDER BY b", (since_ns,)).fetchall()
    events = []
    for b, tot in rows:
        if events and b - events[-1]["end"] <= merge_gap_ns:
            events[-1]["end"] = b + bucket_ns
            events[-1]["liq"] += tot
            events[-1]["peak"] = max(events[-1]["peak"], tot)
        else:
            events.append({"start": b, "end": b + bucket_ns, "liq": tot, "peak": tot})
    return events


def realized(conn, venue, coin, t0, t1):
    """(median realized bps, n) over [t0, t1)."""
    rows = load_trades(conn, venue, coin, t0, t1)
    if not rows:
        return None, 0
    clips = group_clips(rows, venue, coin)
    matcher = BookMatcher(conn, venue, coin)
    vals = [r["realized_bps"] for c in clips
            if "skip" not in (r := measure(c, matcher))]
    return (median(vals), len(vals)) if vals else (None, 0)


def had_gap(conn, venue, t0, t1):
    # Only genuine data-loss events exclude a venue. Routine 'disconnect' events
    # auto-recover (HL reconnects often) and would over-exclude; 'gap'/'stale'
    # are explicit data problems (nonce gap, incomplete backfill, silent feed).
    return conn.execute(
        "SELECT 1 FROM integrity_log WHERE venue=? AND event IN ('gap','stale') "
        "AND ts_ns>=? AND ts_ns<? LIMIT 1", (venue, t0, t1)).fetchone() is not None


def fmt_t(ns):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ns / 1e9))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--venues", nargs="+", default=ALL_VENUES)
    ap.add_argument("--threshold", type=float, default=5e6,
                    help="$ market-wide liquidation per bucket to count as stress")
    ap.add_argument("--bucket-min", type=float, default=1.0)
    ap.add_argument("--merge-gap-min", type=float, default=10.0)
    ap.add_argument("--baseline-hours", type=float, default=24.0)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    since_ns = int((time.time() - args.days * 86400) * 1e9)
    bucket_ns = int(args.bucket_min * 60 * 1e9)
    merge_gap_ns = int(args.merge_gap_min * 60 * 1e9)

    liq_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(notional_usd),0) FROM ext_liquidations "
        "WHERE ts_ns>=?", (since_ns,)).fetchone()
    print(f"\nBEHAVIOR UNDER STRESS — last {args.days:g}d")
    print(f"liquidation signal: OKX market-wide, {liq_total[0]:,} events, "
          f"${liq_total[1]:,.0f} total (throttled ~1/s/symbol)")
    print(f"cascade = bucket(s) of {args.bucket_min:g}min with >=${args.threshold:,.0f} "
          f"liquidated, merged within {args.merge_gap_min:g}min")

    events = detect_cascades(conn, since_ns, bucket_ns, args.threshold, merge_gap_ns)
    if not events:
        print("\nNo cascade events above threshold. Largest liquidation buckets seen:")
        top = conn.execute(
            f"SELECT (ts_ns/{bucket_ns})*{bucket_ns} b, SUM(notional_usd) tot "
            "FROM ext_liquidations WHERE ts_ns>=? "
            f"GROUP BY b ORDER BY tot DESC LIMIT 5", (since_ns,)).fetchall()
        for b, tot in top:
            print(f"  {fmt_t(b)}  ${tot:,.0f}")
        print("\n(Cascade detection is live; this section fills in when a real one hits.)")
        return

    for ev in events:
        dur_min = (ev["end"] - ev["start"]) / 6e10
        print("\n" + "═" * 72)
        print(f" CASCADE  {fmt_t(ev['start'])} → {fmt_t(ev['end'])}  ({dur_min:.0f} min)")
        print(f" ${ev['liq']:,.0f} liquidated market-wide (peak ${ev['peak']:,.0f}/bucket)")
        print("═" * 72)
        base0, base1 = ev["start"] - int(args.baseline_hours * 3600 * 1e9), ev["start"]
        print(f"  {'asset':>5} {'venue':<12} {'stress':>8} {'baseline':>9} {'Δ bps':>8} {'n':>5}")
        for coin in args.assets:
            for v in args.venues:
                if had_gap(conn, v, ev["start"], ev["end"]):
                    print(f"  {coin:>5} {v:<12}  (excluded — integrity gap during event)")
                    continue
                s_bps, s_n = realized(conn, v, coin, ev["start"], ev["end"])
                b_bps, _ = realized(conn, v, coin, base0, base1)
                if s_bps is None:
                    continue
                delta = f"{s_bps - b_bps:>+8.2f}" if b_bps is not None else "     n/a"
                base_s = f"{b_bps:>9.2f}" if b_bps is not None else "      n/a"
                print(f"  {coin:>5} {v:<12} {s_bps:>8.2f} {base_s} {delta} {s_n:>5}")
    print("\n  stress/baseline = median realized cost (bps); Δ>0 = execution worse under stress")
    print("  realized is the cross-venue-comparable metric (Pacifica advertised is feed-limited)")


if __name__ == "__main__":
    main()
