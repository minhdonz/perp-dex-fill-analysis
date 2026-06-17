"""Export aggregate JSON for the static website.

Runs the existing analyses and writes small, public-safe JSON (aggregates only —
no raw fills, no taker addresses, no DB). The static site consumes these; the
cost calculator recomputes client-side from calc.json. Run on the VPS by cron,
then push the output dir to the static host.

  python web/export.py --db data/fills.db --out web/public/data --hours 24
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from analysis import edition, fees, funding, history, league, makers, stress  # noqa: E402
from analysis.reconstruct import METHOD, RUNGS  # noqa: E402

VENUES = ["hyperliquid", "lighter", "pacifica"]
ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "SPCX"]
SEAMS = [
    "Pacifica's advertised/gap is feed-limited: its sub-10ms book refreshes faster than its "
    "snapshot feed, so we report Pacifica's realized cost, not its book-relative gap.",
    "Funding is a trailing-average forward estimate (mean-reverting), not a guarantee.",
    "Cascade signal is OKX market-wide (Binance is geo-restricted from our host). OKX is a "
    "top venue but a thinner proxy than the whole market.",
    "Sparse rungs (few clips of a size in-window) are flagged, never fabricated.",
    "Hyperliquid's book is top-20 levels on its public feed; clips deeper than visible depth "
    "are excluded, not extrapolated.",
]


def cell_json(c):
    if not c:
        return None
    return {"realized": round(c["real"], 3), "gap": round(c["gap"], 3),
            "sparse": c["sparse"], "feed_limited": c["feed_limited"],
            "bad_realized": c["bad_realized"], "n": c["n"]}


def build_headline(gap):
    for coin in ("BTC", "ETH", "SOL"):
        for rung in (1_000_000, 100_000, 10_000):
            cells = gap.get(coin, {}).get(rung)
            if not cells:
                continue
            ch, pr = edition.cheapest(cells), edition.priciest(cells)
            if ch and pr and ch["venue"] != pr["venue"] and pr["real"] > ch["real"] + 0.05:
                return (f"{ch['venue'].title()} fills {edition.rung_label(rung)} {coin} at "
                        f"{ch['real']:.2f} bps; {pr['venue'].title()} at {pr['real']:.2f} bps.")
    return "Advertised depth vs realized fills across perp DEXs, tracked over time."


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--out", default="web/public/data")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--hist-days", type=float, default=30.0)
    ap.add_argument("--window-ms", type=int, default=150)
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--stress-threshold", type=float, default=5e6)
    ap.add_argument("--maker-days", type=float, default=14.0)
    ap.add_argument("--maker-min-ratio", type=float, default=3.0)
    ap.add_argument("--maker-top", type=int, default=20)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    conn = sqlite3.connect(args.db)
    fees.ensure_schema(conn)
    funding.ensure_schema(conn)
    makers.ensure_schema(conn)
    since = int((time.time() - args.hours * 3600) * 1e9)
    hist_since = int((time.time() - args.hist_days * 86400) * 1e9)

    def dump(name, obj):
        with open(os.path.join(args.out, name), "w") as f:
            json.dump(obj, f, separators=(",", ":"))

    # ---- gap.json + calc.json ----
    gap = edition.compute_gap(conn, ASSETS, since, args.window_ms, args.min_samples)
    gap_out, slippage = {}, {}
    for a, rungs in gap.items():
        gap_out[a] = {str(r): {v: cell_json(cells.get(v)) for v in VENUES}
                      for r, cells in rungs.items()}
        # calculator slippage: null out thin/stale cells so they're never priced
        slippage[a] = {str(r): {v: (round(c["real"], 3) if (c := cells.get(v))
                                     and not c["bad_realized"] else None)
                                for v in VENUES} for r, cells in rungs.items()}
    dump("gap.json", gap_out)

    fapr = {v: {a: (round(row[1], 6) if (row := funding.get_rate(conn, v, a)) else None)
                for a in ASSETS} for v in VENUES}
    # funding lookback window + when the rates were last fetched (one cron run)
    fwin, fts = conn.execute(
        "SELECT MAX(window_days), MAX(fetched_ns) FROM funding_rates").fetchone()
    dump("calc.json", {
        "rungs": RUNGS,
        "slippage": slippage,
        "funding_apr": fapr,  # fraction/yr; net funding bps = apr*hold_days/365*1e4*side
        "funding_window_days": fwin,
        "funding_as_of": int(fts / 1e9) if fts else None,
        "fee_ladders": {
            "hyperliquid": fees.HL_TIERS,   # [ [vol_cutoff, taker_frac, maker_frac], ... ]
            "pacifica": fees.PACIFICA_TIERS,
            "lighter": {"standard_taker_bps": fees.LIGHTER_STANDARD_TAKER_BPS,
                        "premium_taker_bps": fees.LIGHTER_PREMIUM_BASE_TAKER_BPS},
        },
    })

    # ---- funding.json (carry: APR + stability, same source as calc.funding_apr) ----
    def fund_cell(v, a):
        rate = funding.get_rate(conn, v, a)
        if rate is None:
            return None
        _, apr, n = rate
        stab = funding.get_stability(conn, v, a) or (None, None, None, None)
        pct_pos, flip, std, _ = stab
        return {"apr": round(apr, 6), "pct_positive": pct_pos, "flip_rate": flip,
                "std_hourly": std, "n": n}
    fund = {a: {v: c for v in VENUES if (c := fund_cell(v, a))} for a in ASSETS}
    dump("funding.json", {"as_of": int(fts / 1e9) if fts else None,
                          "window_days": fwin, "assets": fund})

    # ---- history.json (gap_windows + funding_windows; daily + 12 session windows) ----
    daykey = lambda w: time.strftime("%Y-%m-%d", time.gmtime(w / 1e9))
    sesskey = lambda w: int((w % history.DAY_NS) // history.WINDOW_NS)
    hist = {}
    for a in ASSETS:
        hist[a] = {}
        for r in RUNGS:
            vd = {}
            for v in VENUES:
                g = history.load_gap(conn, v, a, r, hist_since)
                if not g:
                    continue
                f = history.load_funding(conn, v, a, hist_since)
                daily = history.aggregate(g, f, daykey)
                sess = history.aggregate(g, f, sesskey)
                vd[v] = {
                    "daily": [{"t": k, "realized": round(d["realized"], 3),
                               "gap": round(d["gap"], 3), "mh": d["mean_hourly"], "n": d["n"]}
                              for k, d in sorted(daily.items()) if d["realized"] is not None],
                    "session": [{"b": k, "realized": round(d["realized"], 3),
                                 "gap": round(d["gap"], 3), "mh": d["mean_hourly"], "n": d["n"]}
                                for k, d in sorted(sess.items()) if d["realized"] is not None],
                }
            if vd:
                hist[a][str(r)] = vd
    dump("history.json", hist)

    # ---- stress.json ----
    events = stress.detect_cascades(conn, since, int(60 * 1e9), args.stress_threshold,
                                    int(10 * 60 * 1e9))
    stress_out = []
    for ev in events:
        b0 = ev["start"] - int(24 * 3600 * 1e9)
        degr = []
        for a in ("BTC", "ETH", "SOL"):
            for v in VENUES:
                if stress.had_gap(conn, v, ev["start"], ev["end"]):
                    continue
                s, _ = stress.realized(conn, v, a, ev["start"], ev["end"])
                if s is None:
                    continue
                b, _ = stress.realized(conn, v, a, b0, ev["start"])
                degr.append({"asset": a, "venue": v, "stress": round(s, 3),
                             "baseline": round(b, 3) if b is not None else None})
        stress_out.append({"start": ev["start"], "end": ev["end"],
                           "liq": round(ev["liq"]), "degr": degr})
    dump("stress.json", {"threshold": args.stress_threshold, "events": stress_out})

    # ---- coverage.json ----
    cov = []
    for v in VENUES:
        total, gapped = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(has_gap),0) FROM window_summaries WHERE venue=?",
            (v,)).fetchone()
        cov.append({"venue": v, "method": METHOD[v], "windows": total,
                    "clean_pct": round(100 * (total - gapped) / total, 1) if total else None})
    dump("coverage.json", {"venues": cov, "seams": SEAMS})

    # ---- makers.json (HL market-maker league; addresses are public on HL) ----
    ml = makers.league(conn, args.maker_days, args.maker_min_ratio, args.maker_top)
    as_of = conn.execute(
        "SELECT MAX(ts_ns) FROM account_state WHERE venue='hyperliquid'").fetchone()[0]
    n_seen = conn.execute(
        "SELECT COUNT(DISTINCT maker) FROM maker_windows WHERE venue='hyperliquid' "
        "AND window_start_ns>=?", (int((time.time() - args.maker_days * 86400) * 1e9),)
    ).fetchone()[0]
    # Display volume = all-market 14d maker volume from userFees (the tape only sees our
    # streamed coins); fall back to the tape figure if the account wasn't polled yet.
    def disp_vol(m):
        s = m["state"] or {}
        return (s.get("maker_vol_14d"), True) if s.get("maker_vol_14d") else (m["maker_ntl"], False)
    enriched = [(m, *disp_vol(m)) for m in ml["makers"]]
    enriched.sort(key=lambda t: t[1], reverse=True)           # order by displayed (true) volume
    tot_disp = sum(v for _, v, _ in enriched) or 1.0
    def maker_json(rank, m, vol, all_markets):
        s, f = m["state"] or {}, m["fees"] or {}
        mbps = (f or {}).get("maker_bps")
        rebate = vol * (-mbps) / 1e4 if mbps is not None and mbps < 0 else None
        return {
            "rank": rank, "address": m["address"],
            "short": f"{m['address'][:6]}…{m['address'][-4:]}",
            "maker_ntl": round(vol), "maker_share": round(vol / tot_disp, 4),
            "all_markets": all_markets, "tape_ntl": round(m["maker_ntl"]),
            "ratio": None if m["ratio"] == float("inf") else round(m["ratio"], 1),
            "markets": (s or {}).get("n_positions"),         # true breadth (positions held)
            "tape_coins": m["tape_coins"],
            "oi": round(s["open_interest"]) if s else None,
            "account_value": round(s["account_value"]) if s else None,
            "funding_net": round(s["funding_net"]) if s and s.get("funding_net") is not None else None,
            "funding_days": s.get("funding_days") if s else None,
            "last_day_pnl": round(m["pnl"][-1]["pnl"]) if m["pnl"] else None,
            "pnl": [{"day": p["day"], "pnl": round(p["pnl"])} for p in m["pnl"]],
            "taker_bps": (f or {}).get("taker_bps"),
            "maker_bps": mbps,
            "rebate_window": round(rebate) if rebate else None,
        }
    dump("makers.json", {
        "days": ml["days"], "min_ratio": ml["min_ratio"],
        "total_maker_ntl": round(tot_disp),
        "makers_seen": n_seen, "as_of": int(as_of / 1e9) if as_of else None,
        "makers": [maker_json(i, m, v, am) for i, (m, v, am) in enumerate(enriched, 1)],
    })

    # ---- meta.json ----
    dump("meta.json", {
        "generated_at": int(time.time()), "hours": args.hours,
        "venues": VENUES, "assets": ASSETS, "headline": build_headline(gap),
        "has_makers": bool(ml["makers"]),
    })

    print(f"exported {len(os.listdir(args.out))} json files to {args.out}")


if __name__ == "__main__":
    main()
