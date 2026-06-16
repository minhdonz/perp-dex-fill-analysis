"""Edition assembler: turns the analyses into one markdown report (PRD §6, P0).

Sections: In plain English (auto-generated lay summary) · 1. The Gap ·
2. True Cost to Hold · 3. Behavior Under Stress · 4. Methodology & Confidence ·
5. One opinionated call (left for the author).

Reuses the analysis library so the numbers match the live tools exactly.

  python -m analysis.edition --db data/fills.db --hours 24 --volume 200e6 --hold-days 7
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from statistics import median

from analysis import cost, fees, funding, league, stress
from analysis.netcost import net_cost
from analysis.reconstruct import METHOD, NEG_REALIZED_LIMIT, RUNGS, touch_floor_bps

VENUES = ["hyperliquid", "lighter", "pacifica"]
CONF_LABEL = {"exact": "exact (order id)", "identity": "identity (taker+window)",
              "heuristic": "heuristic (sweep)"}


def rung_label(r):
    return f"${r//1000}k" if r < 1_000_000 else f"${r//1_000_000}M"


def usd(bps, notional):
    return bps / 1e4 * notional


def cheapest(cells):
    rk = [c for c in cells.values() if c and c["rankable"]]
    return min(rk, key=lambda c: c["real"]) if rk else None


def priciest(cells):
    rk = [c for c in cells.values() if c and c["rankable"]]
    return max(rk, key=lambda c: c["real"]) if rk else None


# ---- computation (shared by the table and the plain-English read) ----

def compute_gap(conn, assets, since_ns, window_ms, min_samples):
    """{asset: {rung: {venue: cell|None}}} for assets with any data."""
    gap = {}
    for coin in assets:
        floors, data, present = {}, {}, False
        for v in VENUES:
            per_rung, cov = league.collect(conn, v, coin, since_ns, window_ms)
            data[v] = per_rung
            floors[v] = touch_floor_bps(conn, v, coin, since_ns)
            present = present or cov["prints"] > 0
        if not present:
            continue
        gap[coin] = {}
        for rung in RUNGS:
            cells = {v: (league.build_cell(v, data[v][rung], floors[v], min_samples)
                         if data[v].get(rung) else None) for v in VENUES}
            if any(cells.values()):
                gap[coin][rung] = cells
    return gap


def compute_cost(conn, assets, since_ns, window_ms, rung, volume, hold_days, side):
    hold_h = hold_days * 24
    rows = []
    for coin in assets:
        for v in VENUES:
            ms = cost.collect(conn, v, coin, since_ns, window_ms).get(rung)
            if not ms:
                continue
            slip = median(s for s, _ in ms)
            if slip < NEG_REALIZED_LIMIT:  # thin/stale cell, don't price it
                continue
            fee, _ = cost.venue_fee_bps(conn, v, [k for _, k in ms], volume)
            fb, _ = funding.funding_cost_bps(conn, v, coin, hold_h, side)
            fb = fb or 0.0
            _, rt = net_cost(slip, fee, fb)
            rows.append({"asset": coin, "venue": v, "slip": slip, "fee": fee,
                         "funding": fb, "rt": rt})
    return rows


# ---- rendering ----

def section_plain_english(gap, cost_rows, rung, hold_days):
    rl = rung_label(rung)
    L = ["## In plain English\n",
         f"_\"bps\" = basis points = 0.01%. On a {rl} trade, 1 bp ≈ ${rung/1e4:,.0f}. "
         "\"The gap\" is how much worse a trade actually filled than the order book "
         "promised; lower realized cost is better._\n"]

    # 1. cheapest to trade the focal size on a major
    for coin in ("BTC", "ETH", "SOL"):
        cells = gap.get(coin, {}).get(rung)
        if not cells:
            continue
        ch, pr = cheapest(cells), priciest(cells)
        if not ch:
            break
        s = (f"- **Trading {rl} of {coin}** was cheapest on **{ch['venue']}** at about "
             f"{ch['real']:.2f} bps (~${usd(ch['real'], rung):,.0f} on a {rl} clip)")
        if pr and pr["venue"] != ch["venue"] and pr["real"] > ch["real"] + 0.02:
            s += (f", versus {pr['real']:.2f} bps (~${usd(pr['real'], rung):,.0f}) "
                  f"on {pr['venue']}")
            if ch["real"] > 0 and pr["real"] / ch["real"] >= 1.5:
                s += f", roughly {pr['real']/ch['real']:.0f}× more"
        L.append(s + ".")
        break

    # 2. widest divergence across assets at the focal rung
    best = None
    for coin, rungs in gap.items():
        cells = rungs.get(rung)
        if not cells:
            continue
        ch, pr = cheapest(cells), priciest(cells)
        if ch and pr and ch["venue"] != pr["venue"] and ch["real"] > 0:
            mult = pr["real"] / ch["real"]
            if best is None or mult > best[0]:
                best = (mult, coin, ch, pr)
    if best and best[0] >= 2:
        mult, coin, ch, pr = best
        L.append(f"- The widest spread between venues was on **{coin}**: {ch['venue']} "
                 f"filled {rl} at ~{ch['real']:.2f} bps while {pr['venue']} charged "
                 f"~{pr['real']:.2f} bps, **about {mult:.0f}× more expensive** for the "
                 "same trade.")

    # 3. cheapest to enter vs to hold (net cost) on BTC
    btc = [r for r in cost_rows if r["asset"] == "BTC"]
    if btc:
        rt = min(btc, key=lambda r: r["rt"])
        slip_best = min(btc, key=lambda r: r["slip"])
        s = (f"- **Cheapest to actually *hold*** a {rl} BTC position for {hold_days:g} days "
             f"(spread + fees + funding) was **{rt['venue']}** at ~{rt['rt']:+.0f} bps "
             f"round-trip (~${usd(abs(rt['rt']), rung):,.0f})")
        if slip_best["venue"] != rt["venue"]:
            s += (f". Note {slip_best['venue']} had the tightest *spread*, but fees and "
                  "funding change who's cheapest once you hold")
        L.append(s + ". Fees and funding dwarf the spread for any real hold.")

    # 4. negative funding = paid to hold
    paid = sorted({r["asset"] for r in cost_rows if r["funding"] < -1})
    if paid:
        L.append(f"- On **{', '.join(paid)}**, funding currently *pays* people who are long, "
                 "so holding the position can earn money over the period rather than cost it.")
    L.append("")
    return "\n".join(L)


def section_gap(gap):
    out = ["## 1. The Gap: advertised vs realized execution cost\n",
           "_Each cell: a venue's **realized** fill cost (bps), with the **gap** in "
           "parentheses. Gap = realized minus advertised (what walking the book implied); "
           "positive means it filled worse than the book promised. Cheapest realized in "
           "**bold**. `fl` = Pacifica book feed-limited (realized shown, not gap)._\n"]
    for coin, rungs in gap.items():
        out.append(f"\n**{coin}**\n")
        out.append("| rung | " + " | ".join(VENUES) + " |")
        out.append("|" + "---|" * (len(VENUES) + 1))
        for rung in RUNGS:
            cells = rungs.get(rung)
            if not cells:
                continue
            rk = [c for c in cells.values() if c and c["rankable"]]
            best = min(rk, key=lambda c: c["real"])["venue"] if rk else None
            row = [rung_label(rung)]
            for v in VENUES:
                c = cells[v]
                if not c:
                    row.append("–")
                    continue
                if c["bad_realized"]:
                    row.append("n/a")
                    continue
                g = "fl" if c["feed_limited"] else f"{c['gap']:+.2f}"
                txt = f"{c['real']:.2f} ({g}){'*' if c['sparse'] else ''}"
                row.append(f"**{txt}**" if v == best else txt)
            out.append("| " + " | ".join(row) + " |")
    out.append("\n_`*` = sparse (few samples); a venue absent for an asset isn't listed._\n")
    return "\n".join(out)


def section_cost(cost_rows, rung, volume, hold_days, side):
    out = [f"\n## 2. True Cost to Hold: net cost at {rung_label(rung)}, "
           f"${volume/1e6:.0f}M/14d fund, {side} {hold_days:g}d hold\n",
           "_Net = slippage + taker fee (both legs) + funding over the hold (bps)._\n",
           "| asset | venue | slip/leg | fee/leg | funding | round-trip |",
           "|---|---|---|---|---|---|"]
    for r in cost_rows:
        out.append(f"| {r['asset']} | {r['venue']} | {r['slip']:.2f} | {r['fee']:.2f} | "
                   f"{r['funding']:+.2f} | {r['rt']:+.2f} |")
    return "\n".join(out)


def section_stress(conn, assets, since_ns, threshold, bucket_min, merge_gap_min, baseline_h):
    bucket_ns = int(bucket_min * 60 * 1e9)
    events = stress.detect_cascades(conn, since_ns, bucket_ns, threshold,
                                    int(merge_gap_min * 60 * 1e9))
    out = ["\n## 3. Behavior Under Stress\n",
           f"_Market-wide liquidation cascades (OKX feed, ≥${threshold:,.0f}/"
           f"{bucket_min:g}min), execution vs prior-{baseline_h:g}h baseline._\n"]
    if not events:
        out.append(f"No cascade ≥${threshold:,.0f} in this window. (Detector is live; "
                   "the section fills in when one occurs.)")
        return "\n".join(out)
    for ev in events:
        out.append(f"\n**Cascade {stress.fmt_t(ev['start'])} to {stress.fmt_t(ev['end'])}**: "
                   f"${ev['liq']:,.0f} liquidated.\n")
        out.append("| asset | venue | stress | baseline | Δ bps |")
        out.append("|---|---|---|---|---|")
        b0 = ev["start"] - int(baseline_h * 3600 * 1e9)
        for coin in assets:
            for v in VENUES:
                if stress.had_gap(conn, v, ev["start"], ev["end"]):
                    continue
                s, _ = stress.realized(conn, v, coin, ev["start"], ev["end"])
                b, _ = stress.realized(conn, v, coin, b0, ev["start"])
                if s is None:
                    continue
                d = f"{s-b:+.2f}" if b is not None else "n/a"
                base_s = f"{b:.2f}" if b is not None else "n/a"
                out.append(f"| {coin} | {v} | {s:.2f} | {base_s} | {d} |")
    return "\n".join(out)


def section_methodology(conn):
    out = ["\n## 4. Methodology & Confidence\n",
           "**Coverage (clean 2h windows, excluded if a feed gap was detected):**\n",
           "| venue | method | clean windows |", "|---|---|---|"]
    for v in VENUES:
        total, gapped = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(has_gap),0) FROM window_summaries WHERE venue=?",
            (v,)).fetchone()
        pct = f"{100*(total-gapped)/total:.1f}% of {total}" if total else "n/a"
        out.append(f"| {v} | {CONF_LABEL[METHOD[v]]} | {pct} |")
    out.append("\n**Honest seams:**")
    out.append("- **Pacifica advertised/gap is feed-limited**: its sub-10ms book refreshes "
               "faster than its ~4/s snapshot feed, so the book-implied cost under-measures "
               "real fillable liquidity. We report Pacifica's *realized* cost, not its gap.")
    out.append("- **Funding is a trailing-average forward estimate** (mean-reverting; we also "
               "track sign-flip rate as a stability measure), not a guarantee.")
    out.append("- **Cascade signal is OKX-only** (Binance's stream is geo-restricted from our "
               "host); OKX is a top venue but a thinner proxy than the full market.")
    out.append("- **Sparse rungs** (few clips of a size in-window) are flagged, never "
               "fabricated; large-size rows can be thin.")
    out.append("- **Hyperliquid book is top-20 levels** on its WS feed; clips deeper than "
               "visible depth are excluded, not extrapolated.")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL", "HYPE", "ZEC"])
    ap.add_argument("--window-ms", type=int, default=150)
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--rung", type=int, default=100_000)
    ap.add_argument("--volume", type=float, default=200e6)
    ap.add_argument("--hold-days", type=float, default=7.0)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--stress-threshold", type=float, default=5e6)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    fees.ensure_schema(conn)
    funding.ensure_schema(conn)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)
    today = time.strftime("%Y-%m-%d", time.gmtime())

    gap = compute_gap(conn, args.assets, since_ns, args.window_ms, args.min_samples)
    cost_rows = compute_cost(conn, args.assets, since_ns, args.window_ms, args.rung,
                             args.volume, args.hold_days, args.side)

    parts = [
        f"# RealizedFill · Edition {today}\n",
        f"_Advertised-vs-realized execution quality across perp DEXs. "
        f"Window: last {args.hours:g}h · Venues: {', '.join(VENUES)} · "
        f"Assets: {', '.join(args.assets)}._\n",
        "> Read-only measurement of public trade/book feeds; no capital, no orders.\n",
        section_plain_english(gap, cost_rows, args.rung, args.hold_days),
        section_gap(gap),
        section_cost(cost_rows, args.rung, args.volume, args.hold_days, args.side),
        section_stress(conn, args.assets, since_ns, args.stress_threshold, 1.0, 10.0, 24.0),
        section_methodology(conn),
        "\n## 5. One opinionated call\n\n_[Author's stake-in-the-ground for this edition.]_\n",
    ]
    report = "\n".join(parts)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
        print(f"wrote {args.out} ({len(report):,} chars)")
    else:
        print(report)


if __name__ == "__main__":
    main()
