"""Net cost to execute + hold — slippage + fee + funding, entry vs round-trip.

The "cost to operate at scale" view (PRD §5.3). For each asset / clip-size
rung / venue:

  slip/leg  = median realized slippage at that size (league-table number)
  fee/leg   = taker fee: HL real per-account or --volume tier ladder;
              Pacifica --volume tier; Lighter standard 0 (premium noted).
  fund      = funding over the hold (the fund SETS --hold-days and --side),
              signed: + = you pay, − = you're paid. Trailing-avg rate.
  ENTRY     = slip/leg + fee/leg                         (cost to get in)
  ROUND-TRIP= 2*(slip/leg + fee/leg) + fund              (in + hold + out)

Funding is a single hold cost (not per leg); fees are taker on both legs.

  python -m analysis.cost --db data/fills.db --hours 6 --hold-days 3
  python -m analysis.cost --db data/fills.db --volume 200e6 --hold-days 7 --side short
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from statistics import median

from analysis import fees, funding
from analysis.netcost import net_cost
from analysis.reconstruct import (
    METHOD, RUNGS, BookMatcher, group_clips, load_trades, measure, rung_for,
)

ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "SPCX"]
CONF_TAG = {"exact": "exact", "identity": "ident", "heuristic": "heur"}


def rung_label(r: int) -> str:
    return f"${r/1000:.0f}k" if r < 1_000_000 else f"${r/1_000_000:.0f}M"


def collect(conn, venue, coin, since_ns, window_ms):
    rows = load_trades(conn, venue, coin, since_ns)
    per_rung: dict[int, list] = {}
    if not rows:
        return per_rung
    clips = group_clips(rows, venue, coin, window_ms)
    matcher = BookMatcher(conn, venue, coin)
    for c in clips:
        r = measure(c, matcher)
        if "skip" in r:
            continue
        rung = rung_for(float(c.notional))
        if rung is not None:
            per_rung.setdefault(rung, []).append((r["realized_bps"], c.key))
    return per_rung


def venue_fee_bps(conn, venue, clip_keys, assumed_vol):
    if venue == "hyperliquid" and assumed_vol is None:
        reals = [b for k in clip_keys if (b := fees.real_hl_taker_bps(conn, k)) is not None]
        if reals:
            return median(reals), f"real n={len(reals)}"
        return fees.HL_TIERS[0][1] * 1e4, "base (uncached)"
    return fees.taker_bps(conn, venue, assumed_vol_14d=assumed_vol)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--assets", nargs="+", default=ALL_ASSETS)
    ap.add_argument("--venues", nargs="+", default=ALL_VENUES)
    ap.add_argument("--window-ms", type=int, default=150)
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--volume", type=float, default=None,
                    help="fund's 14d volume ($) -> price HL/Pacifica at that tier")
    ap.add_argument("--hold-days", type=float, default=1.0, help="hold length for funding")
    ap.add_argument("--side", choices=["long", "short"], default="long")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    fees.ensure_schema(conn)
    funding.ensure_schema(conn)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)
    hold_hours = args.hold_days * 24

    print("\nNET COST TO EXECUTE + HOLD — slippage + taker fee + funding (bps)")
    if args.volume is not None:
        print(f"HL/Pacifica fee at fund volume ${args.volume:,.0f}/14d -> "
              f"HL {fees.tier_label('hyperliquid', args.volume)} "
              f"({fees.taker_bps_for_volume('hyperliquid', args.volume):.2f}bps), "
              f"Pacifica {fees.tier_label('pacifica', args.volume)} "
              f"({fees.taker_bps_for_volume('pacifica', args.volume):.2f}bps)")
    else:
        print("HL fee = observed takers' REAL rates. Pass --volume to price your own tier.")
    print(f"funding: {args.side.upper()} held {args.hold_days:g}d (trailing-avg rate)   ·   "
          f"entry = slip+fee   ·   round-trip = 2×(slip+fee) + funding")

    for coin in args.assets:
        data = {v: collect(conn, v, coin, since_ns, args.window_ms) for v in args.venues}
        eligible = [v for v in args.venues if data[v]]
        if not eligible:
            continue
        # funding per venue for this coin (size-independent)
        fund = {}
        for v in eligible:
            fb, basis = funding.funding_cost_bps(conn, v, coin, hold_hours, args.side)
            fund[v] = (fb, basis)
        parts = []
        for v in eligible:
            if fund[v][0] is None:
                continue
            apr = fund[v][1].split(" ")[0]  # +8.0%APR
            st = funding.get_stability(conn, v, coin)
            flip = f" flip{st[1]*100:.0f}%" if st and st[1] is not None else ""
            parts.append(f"{v}:{apr}{flip}")
        fstr = "  ".join(parts)
        print("\n" + "═" * 90)
        print(f" {coin}    funding {args.side} {args.hold_days:g}d:  {fstr or '(no funding data — run fetch_funding.py)'}")
        print("  (flip% = share of hours funding changed sign; high = unstable/unpredictable carry)")
        print("═" * 90)
        print(f"  {'rung':>5}  {'venue':<12} {'conf':>5} {'n':>4} "
              f"{'slip/leg':>8} {'fee/leg':>8} {'fund':>7} {'ENTRY':>6} {'ROUND-TRIP':>11}  fee basis")
        for rung in RUNGS:
            cells = []
            for v in eligible:
                ms = data[v].get(rung)
                if not ms:
                    continue
                n = len(ms)
                slip = median(s for s, _ in ms)
                fee, basis = venue_fee_bps(conn, v, [k for _, k in ms], args.volume)
                fb = fund[v][0] or 0.0
                entry, rt = net_cost(slip, fee, fb)
                cells.append((v, n, slip, fee, fb, entry, rt, basis))
            if not cells:
                continue
            cells.sort(key=lambda c: c[6])  # by round-trip total
            label = rung_label(rung)
            for v, n, slip, fee, fb, entry, rt, basis in cells:
                sp = "*" if n < args.min_samples else " "
                print(f"  {label:>5}  {v:<12} {CONF_TAG[METHOD[v]]:>5} {n:>3}{sp} "
                      f"{slip:>8.2f} {fee:>8.2f} {fb:>+7.2f} {entry:>6.2f} {rt:>+11.2f}  {basis}")
                label = ""

    print("\n  fee basis: real=HL per-account (userFees) · tier=HL/Pacifica ladder at --volume")
    print("             base=lowest tier · standard=Lighter standard (0; premium=2.8bps)")
    print("  fund: + = you pay funding, − = you're paid · trailing-avg rate, forward estimate")
    print("  * = sparse   ·   round-trip = enter + hold + exit (funding once, fee both legs)")
    missing = [v for v in args.venues
               if funding.get_rate(conn, v, ALL_ASSETS[0]) is None]
    if missing:
        print(f"  NOTE: no funding data for {missing} — run scripts/fetch_funding.py")


if __name__ == "__main__":
    main()
