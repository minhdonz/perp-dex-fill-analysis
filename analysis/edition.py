"""Edition assembler — turns the analyses into one markdown report (PRD §6, P0).

Sections: 1. The Gap · 2. True Cost to Hold · 3. Behavior Under Stress ·
4. Methodology & Confidence (coverage, confidence tags, the honest seams) ·
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
from analysis.reconstruct import BOOK_RELIABLE, METHOD, RUNGS, touch_floor_bps

VENUES = ["hyperliquid", "lighter", "pacifica"]
CONF_LABEL = {"exact": "exact (order id)", "identity": "identity (taker+window)",
              "heuristic": "heuristic (sweep)"}


def rung_label(r):
    return f"${r//1000}k" if r < 1_000_000 else f"${r//1_000_000}M"


def section_gap(conn, assets, since_ns, window_ms, min_samples):
    out = ["## 1. The Gap — advertised vs realized execution cost\n",
           "_Realized cost per clip-size rung (bps), cheapest venue in **bold**. "
           "`fl` = Pacifica's book is feed-limited (refreshes faster than snapshots), "
           "so its gap isn't comparable; its realized cost is._\n"]
    for coin in assets:
        floors, data = {}, {}
        present = False
        for v in VENUES:
            per_rung, cov = league.collect(conn, v, coin, since_ns, window_ms)
            data[v] = per_rung
            floors[v] = touch_floor_bps(conn, v, coin, since_ns)
            present = present or cov["prints"] > 0
        if not present:
            continue
        out.append(f"\n**{coin}**\n")
        out.append("| rung | " + " | ".join(VENUES) + " |")
        out.append("|" + "---|" * (len(VENUES) + 1))
        for rung in RUNGS:
            cells = {}
            for v in VENUES:
                ms = data[v].get(rung)
                cells[v] = league.build_cell(v, ms, floors[v], min_samples) if ms else None
            if not any(cells.values()):
                continue
            rankable = [c for c in cells.values() if c and c["rankable"]]
            best = min(rankable, key=lambda c: c["real"])["venue"] if rankable else None
            row = [rung_label(rung)]
            for v in VENUES:
                c = cells[v]
                if not c:
                    row.append("–")
                    continue
                gap = "fl" if c["feed_limited"] else f"{c['gap']:+.2f}"
                txt = f"{c['real']:.2f} ({gap}){'*' if c['sparse'] else ''}"
                row.append(f"**{txt}**" if v == best else txt)
            out.append("| " + " | ".join(row) + " |")
    out.append("\n_`*` = sparse (few samples); a venue absent for an asset isn't listed._\n")
    return "\n".join(out)


def section_cost(conn, assets, since_ns, window_ms, rung, volume, hold_days, side):
    hold_h = hold_days * 24
    out = [f"\n## 2. True Cost to Hold — net cost at {rung_label(rung)}, "
           f"${volume/1e6:.0f}M/14d fund, {side} {hold_days:g}d hold\n",
           "_Net = slippage + taker fee (both legs) + funding over the hold (bps)._\n",
           "| asset | venue | slip/leg | fee/leg | funding | round-trip |",
           "|---|---|---|---|---|---|"]
    for coin in assets:
        for v in VENUES:
            per_rung = cost.collect(conn, v, coin, since_ns, window_ms)
            ms = per_rung.get(rung)
            if not ms:
                continue
            slip = median(s for s, _ in ms)
            fee, _ = cost.venue_fee_bps(conn, v, [k for _, k in ms], volume)
            fb, _ = funding.funding_cost_bps(conn, v, coin, hold_h, side)
            fb = fb or 0.0
            _, rt = net_cost(slip, fee, fb)
            out.append(f"| {coin} | {v} | {slip:.2f} | {fee:.2f} | {fb:+.2f} | {rt:+.2f} |")
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
        out.append(f"\n**Cascade {stress.fmt_t(ev['start'])} → {stress.fmt_t(ev['end'])}** — "
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


def section_methodology(conn, assets, since_ns, window_ms):
    out = ["\n## 4. Methodology & Confidence\n"]
    # coverage from window_summaries
    out.append("**Coverage (clean 2h windows, excluded if a feed gap was detected):**\n")
    out.append("| venue | method | clean windows |")
    out.append("|---|---|---|")
    for v in VENUES:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(has_gap),0) FROM window_summaries WHERE venue=?",
            (v,)).fetchone()
        total, gapped = row[0], row[1]
        pct = f"{100*(total-gapped)/total:.1f}% of {total}" if total else "—"
        out.append(f"| {v} | {CONF_LABEL[METHOD[v]]} | {pct} |")
    out.append("\n**Clip-size attribution confidence:** Lighter `exact` (per-order id), "
               "Hyperliquid `identity` (taker address + time window), Pacifica `heuristic` "
               "(print sweep, no taker id).\n")
    out.append("**Honest seams:**")
    out.append("- **Pacifica advertised/gap is feed-limited** — its sub-10ms book refreshes "
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
    ap.add_argument("--out", default=None, help="write to this path (default: stdout)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    fees.ensure_schema(conn)
    funding.ensure_schema(conn)
    since_ns = int((time.time() - args.hours * 3600) * 1e9)
    today = time.strftime("%Y-%m-%d", time.gmtime())

    parts = [
        f"# RealizedFill — Edition {today}\n",
        f"_Advertised-vs-realized execution quality across perp DEXs. "
        f"Window: last {args.hours:g}h · Venues: {', '.join(VENUES)} · "
        f"Assets: {', '.join(args.assets)}._\n",
        "> Read-only measurement of public trade/book feeds; no capital, no orders.\n",
        section_gap(conn, args.assets, since_ns, args.window_ms, args.min_samples),
        section_cost(conn, args.assets, since_ns, args.window_ms, args.rung,
                     args.volume, args.hold_days, args.side),
        section_stress(conn, args.assets, since_ns, args.stress_threshold, 1.0, 10.0, 24.0),
        section_methodology(conn, args.assets, since_ns, args.window_ms),
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
