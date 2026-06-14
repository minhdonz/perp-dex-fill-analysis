"""Net cost to execute — slippage + fee, entry vs round-trip side by side.

Phase 1 of the "cost to operate at scale" view (funding is Phase 2). For each
asset / clip-size rung / venue:

  slip/leg  = median realized slippage at that size (the league-table number)
  fee/leg   = taker fee:
                Hyperliquid — REAL per-account (median of observed takers'
                  actual rates), or, with --volume, the ladder rate for a
                  fund's stated 14-day volume (no assumption — the fund's own
                  number). Cheapest available, not assumed.
                Lighter/Pacifica — published schedule (flagged ‡ if unconfirmed,
                  '—' if we have no schedule yet).
  ENTRY     = slip/leg + fee/leg                  (cost to get in)
  ROUND-TRIP= 2*(slip/leg + fee/leg)              (in + out, taker both legs)

Funding is NOT included yet (Phase 2); the point this view already makes is
that the fee — not slippage — is the cost that matters, and on HL it is set by
the fund's volume tier.

  python -m analysis.cost --db data/fills.db --hours 6
  python -m analysis.cost --db data/fills.db --volume 200e6   # price HL at the >$100M tier
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from statistics import median

from analysis import fees
from analysis.reconstruct import (
    METHOD, RUNGS, BookMatcher, group_clips, load_trades, measure, rung_for,
)

ALL_VENUES = ["hyperliquid", "lighter", "pacifica"]
ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]
CONF_TAG = {"exact": "exact", "identity": "ident", "heuristic": "heur"}


def rung_label(r: int) -> str:
    return f"${r/1000:.0f}k" if r < 1_000_000 else f"${r/1_000_000:.0f}M"


def collect(conn, venue, coin, since_ns, window_ms):
    """rung -> list of (slip_bps, taker_key)."""
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
    """(fee_bps, basis) for a rung. HL with no stated volume uses observed real
    per-account rates (needs clip keys); everything else delegates to the fee
    model (HL/Pacifica ladder at --volume, Lighter standard)."""
    if venue == "hyperliquid" and assumed_vol is None:
        reals = [b for k in clip_keys
                 if (b := fees.real_hl_taker_bps(conn, k)) is not None]
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
                    help="fund's 14d volume ($) -> price HL at that tier (no assumption)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    fees.ensure_schema(conn)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)

    print("\nNET COST TO EXECUTE — slippage + taker fee (bps).  Funding = Phase 2, not included.")
    if args.volume is not None:
        print(f"HL/Pacifica priced at the fund's stated volume ${args.volume:,.0f}/14d -> "
              f"HL {fees.tier_label('hyperliquid', args.volume)} "
              f"({fees.taker_bps_for_volume('hyperliquid', args.volume):.2f}bps), "
              f"Pacifica {fees.tier_label('pacifica', args.volume)} "
              f"({fees.taker_bps_for_volume('pacifica', args.volume):.2f}bps)")
    else:
        print("HL priced at observed takers' REAL rates (median per rung). "
              "Pass --volume to price your own tier (HL + Pacifica).")
    print("entry = slip/leg + fee/leg   ·   round-trip = 2×(slip+fee), taker both legs")

    for coin in args.assets:
        data = {v: collect(conn, v, coin, since_ns, args.window_ms) for v in args.venues}
        eligible = [v for v in args.venues if data[v]]
        if not eligible:
            continue
        print("\n" + "═" * 82)
        print(f" {coin}")
        print("═" * 82)
        print(f"  {'rung':>5}  {'venue':<12} {'conf':>5} {'n':>4} "
              f"{'slip/leg':>8} {'fee/leg':>9} {'ENTRY':>7} {'ROUND-TRIP':>11}  fee basis")
        for rung in RUNGS:
            line_cells = []
            for v in eligible:
                ms = data[v].get(rung)
                if not ms:
                    continue
                n = len(ms)
                slip = median(s for s, _ in ms)
                keys = [k for _, k in ms]
                fee, basis = venue_fee_bps(conn, v, keys, args.volume)
                line_cells.append((v, n, slip, fee, basis))
            if not line_cells:
                continue
            # order by round-trip net (cheapest first); unknown-fee rows last
            def total(c):
                v, n, slip, fee, basis = c
                return (fee is None, (slip + (fee or 0)))
            line_cells.sort(key=total)
            label = rung_label(rung)
            for v, n, slip, fee, basis in line_cells:
                conf = CONF_TAG[METHOD[v]]
                sparse = "*" if n < args.min_samples else " "
                if fee is None:
                    feec, entry, rt = "   —", "   —", "        —"
                    basis = basis + " (no schedule)"
                else:
                    unconf = "‡" if "?" in basis else " "
                    feec = f"{fee:>7.2f}{unconf}"
                    entry = f"{slip+fee:>7.2f}"
                    rt = f"{2*(slip+fee):>9.2f}"
                print(f"  {label:>5}  {v:<12} {conf:>5} {n:>3}{sparse} "
                      f"{slip:>8.2f} {feec:>9} {entry:>7} {rt:>11}  {basis}")
                label = ""

    print("\n  fee basis (all schedules from official docs, 2026-06-14):")
    print("    real     = HL actual per-account rate (public userFees)")
    print("    tier >$X = HL/Pacifica 14d-volume ladder priced at --volume (the fund's own number)")
    print("    base     = lowest tier (Pacifica has no per-account read; HL uncached)")
    print("    standard = Lighter standard account (0 fee, higher latency)")
    print("  * = sparse (n below threshold)   ·   funding not included (Phase 2)")
    print("  note [lighter]: standard=0 shown; opt-in Premium pays flat 2.8bps taker")
    print("                  (1.96 at max LIT stake), in exchange for lower latency")
    print("  note [pacifica]: no taker id on feed -> can't read real accounts; --volume")
    print("                   prices the fund's tier, else base tier (4.0bps) is shown")


if __name__ == "__main__":
    main()
