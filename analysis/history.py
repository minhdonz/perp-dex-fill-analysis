"""Time-series net cost — how slippage + fee + funding evolve over time (PRD P1,
"history is the product"). Reads the precomputed gap_windows + funding_windows,
so it's fast and survives book-thinning. Two slices:

  (a) DAILY TREND        — net cost by day
  (b) SESSION WINDOWS    — the 12 daily 2h buckets (Asia/EU/US time-of-day
                           fairness, PRD §5.4), aggregated across days

Per period × venue: clip-weighted realized slippage + gap (from gap_windows),
taker fee (fee model at --volume tier), funding (from funding_windows over the
--hold-days hold), and net round-trip. Gap-marked windows are excluded.

  python -m analysis.history --db data/fills.db --assets BTC --rung 100000 --days 14
  python -m analysis.history --db ... --volume 200e6 --hold-days 7 --side short
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from collections import defaultdict

from analysis import fees
from analysis.netcost import net_cost

WINDOW_NS = 2 * 3600 * 1_000_000_000
DAY_NS = 24 * 3600 * 1_000_000_000
ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]
CONF = {"hyperliquid": "ident", "lighter": "exact", "pacifica": "heur"}


def session_label(bucket: int) -> str:
    h0 = bucket * 2
    region = "Asia" if h0 < 8 else "EU" if h0 < 16 else "US"
    return f"{h0:02d}-{h0+2:02d}h {region}"


def load_gap(conn, venue, coin, rung, since_ns):
    """window_start_ns -> (n_clips, realized, advertised, gap), gap-clean only."""
    rows = conn.execute(
        "SELECT window_start_ns, n_clips, med_realized_bps, med_advertised_bps, med_gap_bps "
        "FROM gap_windows WHERE venue=? AND coin=? AND rung=? AND window_start_ns>=? "
        "AND has_gap=0", (venue, coin, rung, since_ns)).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def load_funding(conn, venue, coin, since_ns):
    """window_start_ns -> (mean_hourly, n_hours)."""
    rows = conn.execute(
        "SELECT window_start_ns, mean_hourly, n_hours FROM funding_windows "
        "WHERE venue=? AND coin=? AND window_start_ns>=?", (venue, coin, since_ns)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def aggregate(gap, funding, key_fn):
    """Group windows by key_fn(window_start_ns); clip-weighted slippage/gap,
    hour-weighted funding. Returns key -> dict."""
    g = defaultdict(lambda: {"n": 0, "rw": 0.0, "aw": 0.0, "gw": 0.0})
    for w, (n, real, adv, gap_bps) in gap.items():
        d = g[key_fn(w)]
        d["n"] += n
        d["rw"] += real * n
        d["aw"] += adv * n
        d["gw"] += gap_bps * n
    f = defaultdict(lambda: {"h": 0, "fw": 0.0})
    for w, (mh, nh) in funding.items():
        d = f[key_fn(w)]
        d["h"] += nh
        d["fw"] += mh * nh
    out = {}
    for k in set(g) | set(f):
        gg, ff = g.get(k), f.get(k)
        out[k] = {
            "n": gg["n"] if gg else 0,
            "realized": gg["rw"] / gg["n"] if gg and gg["n"] else None,
            "advertised": gg["aw"] / gg["n"] if gg and gg["n"] else None,
            "gap": gg["gw"] / gg["n"] if gg and gg["n"] else None,
            "mean_hourly": ff["fw"] / ff["h"] if ff and ff["h"] else None,
        }
    return out


def print_slice(title, conn, venues, coin, rung, since_ns, key_fn, label_fn,
                volume, hold_hours, side, min_clips):
    print(f"\n  {title}")
    print(f"  {'period':>14}  {'venue':<12} {'n':>5} {'slip':>6} {'gap':>6} "
          f"{'fee':>6} {'fundAPR':>8} {'net RT':>8}")
    side_sign = 1.0 if side == "long" else -1.0
    per_venue = {}
    for v in venues:
        per_venue[v] = aggregate(load_gap(conn, v, coin, rung, since_ns),
                                 load_funding(conn, v, coin, since_ns), key_fn)
    keys = sorted({k for v in venues for k in per_venue[v]})
    for k in keys:
        for v in venues:
            cell = per_venue[v].get(k)
            if not cell or cell["realized"] is None:
                continue
            fee, _ = fees.taker_bps(conn, v, assumed_vol_14d=volume)
            fee = fee if fee is not None else 0.0
            mh = cell["mean_hourly"]
            apr = mh * 24 * 365 * 100 if mh is not None else 0.0
            fund_bps = side_sign * mh * hold_hours * 1e4 if mh is not None else 0.0
            _, rt = net_cost(cell["realized"], fee, fund_bps)
            sp = "*" if cell["n"] < min_clips else " "
            print(f"  {label_fn(k):>14}  {v:<12} {cell['n']:>4}{sp} "
                  f"{cell['realized']:>6.2f} {cell['gap']:>+6.2f} {fee:>6.2f} "
                  f"{apr:>+7.1f}% {rt:>+8.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--assets", nargs="+", default=["BTC"])
    ap.add_argument("--venues", nargs="+", default=ALL_VENUES)
    ap.add_argument("--rung", type=int, default=100_000)
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--volume", type=float, default=None)
    ap.add_argument("--hold-days", type=float, default=1.0)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--min-clips", type=int, default=5)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    since_ns = int((time.time() - args.days * 86400) * 1e9)
    hold_hours = args.hold_days * 24

    print("\nNET COST OVER TIME — slippage + fee + funding (bps), gap-clean windows only")
    fee_basis = (f"fund volume ${args.volume:,.0f}/14d" if args.volume
                 else "base fee tier (pass --volume for your tier)")
    print(f"rung ${args.rung:,} · {fee_basis} · funding {args.side} {args.hold_days:g}d · "
          f"net RT = 2×(slip+fee)+funding")

    day_label = lambda w: time.strftime("%Y-%m-%d", time.gmtime(w / 1e9))
    sess_key = lambda w: (w % DAY_NS) // WINDOW_NS
    for coin in args.assets:
        print("\n" + "═" * 76 + f"\n {coin}   (rung ${args.rung:,})\n" + "═" * 76)
        print_slice("(a) DAILY TREND", conn, args.venues, coin, args.rung, since_ns,
                    day_label, lambda d: d, args.volume, hold_hours, args.side, args.min_clips)
        print_slice("(b) SESSION WINDOWS (Asia/EU/US, aggregated across days)",
                    conn, args.venues, coin, args.rung, since_ns,
                    sess_key, session_label, args.volume, hold_hours, args.side, args.min_clips)
    print("\n  slip=realized slippage/leg · gap=realized−advertised · fee=taker (tier) · "
          "* = sparse (n<min)\n  funding from funding_windows (trailing per-window); "
          "gap-marked windows excluded")


if __name__ == "__main__":
    main()
