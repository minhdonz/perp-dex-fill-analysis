"""Feasibility spike: Hyperliquid market-maker analytics.

Phase 1 of the maker-analytics plan. Two checks, no schema changes, read-only
against the DB + public HL REST:

  1a. Recover maker identity from stored HL trade `raw` (users:[buyer, seller];
      maker = counterparty to the taker we already extract) and rank the top
      makers by maker-side notional over the available window.
  1b. For the top few makers, hit the public HL Info endpoints and confirm they
      return what a maker league needs (OI, positions, PnL, funding, fees).

Writes a markdown findings file. Reuses collector.models.unjson and the Info
POST pattern from scripts/fetch_hl_fees.py.

  python scripts/spike_hl_makers.py --db data/fills.db --days 5 --out scripts/spike_findings.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict

sys.path.insert(0, ".")
from collector.models import unjson
from collector.storage import connect

INFO_URL = "https://api.hyperliquid.xyz/info"


def info(payload: dict, retries: int = 3):
    body = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                INFO_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == retries - 1:
                return {"__error__": str(e)}
            time.sleep(1.5 * (attempt + 1))


def short(a):
    return a if not a or len(a) < 12 else f"{a[:6]}…{a[-4:]}"


def rank_makers(conn, days, limit_rows):
    since_ns = int((time.time() - days * 86400) * 1e9)
    q = ("SELECT coin, notional_usd, raw FROM trades "
         "WHERE venue='hyperliquid' AND ts_ns >= ? ")
    if limit_rows:
        q += f"LIMIT {limit_rows}"
    cur = conn.execute(q, (since_ns,))

    maker_ntl = defaultdict(float)        # addr -> maker-side notional
    maker_coins = defaultdict(set)        # addr -> {coins}
    maker_cnt = defaultdict(int)          # addr -> maker trade count
    taker_ntl = defaultdict(float)        # addr -> taker-side notional
    rows = no_users = 0
    for coin, ntl_s, raw in cur:
        rows += 1
        t = unjson(raw)
        users = t.get("users") if isinstance(t, dict) else None
        if not users or len(users) < 2 or users[0] is None or users[1] is None:
            no_users += 1
            continue
        buy = t.get("side") == "B"           # taker bought
        taker = users[0] if buy else users[1]
        maker = users[1] if buy else users[0]
        ntl = float(ntl_s or 0)
        maker_ntl[maker] += ntl
        maker_coins[maker].add(coin)
        maker_cnt[maker] += 1
        taker_ntl[taker] += ntl
    top = sorted(maker_ntl.items(), key=lambda kv: kv[1], reverse=True)
    return top, maker_coins, maker_cnt, taker_ntl, rows, no_users


def probe_account(addr, start_ms):
    out = {}
    chs = info({"type": "clearinghouseState", "user": addr})
    if isinstance(chs, dict) and "assetPositions" in chs:
        aps = chs["assetPositions"]
        oi = sum(abs(float(p["position"]["positionValue"])) for p in aps)
        upnl = sum(float(p["position"]["unrealizedPnl"]) for p in aps)
        ms = chs.get("marginSummary", {})
        out["clearinghouseState"] = {
            "n_positions": len(aps), "open_interest_usd": round(oi),
            "unrealized_pnl_usd": round(upnl, 2),
            "account_value_usd": round(float(ms.get("accountValue", 0))),
            "total_ntl_pos_usd": round(float(ms.get("totalNtlPos", 0))),
            "sample_coins": [p["position"]["coin"] for p in aps[:6]],
        }
    else:
        out["clearinghouseState"] = {"__missing__": chs}

    pf = info({"type": "portfolio", "user": addr})
    if isinstance(pf, list):
        windows = {w[0]: w[1] for w in pf if isinstance(w, list) and len(w) == 2}
        day = windows.get("day", {})
        pnl = day.get("pnlHistory") or []
        out["portfolio"] = {
            "windows": list(windows.keys()),
            "day_pnlHistory_points": len(pnl),
            "day_pnl_last": pnl[-1] if pnl else None,
            "has_accountValueHistory": "accountValueHistory" in day,
        }
    else:
        out["portfolio"] = {"__missing__": pf}

    uf = info({"type": "userFunding", "user": addr, "startTime": start_ms})
    if isinstance(uf, list):
        tot = 0.0
        for e in uf:
            try:
                tot += float(e["delta"]["usdc"])
            except Exception:
                pass
        out["userFunding"] = {"events": len(uf), "net_usdc_window": round(tot, 2),
                              "sample": uf[0] if uf else None}
    else:
        out["userFunding"] = {"__missing__": uf}

    fees = info({"type": "userFees", "user": addr})
    if isinstance(fees, dict):
        out["userFees"] = {"userCrossRate": fees.get("userCrossRate"),
                           "userAddRate": fees.get("userAddRate"),
                           "has_dailyUserVlm": "dailyUserVlm" in fees}
    else:
        out["userFees"] = {"__missing__": fees}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--days", type=float, default=5.0)
    ap.add_argument("--limit-rows", type=int, default=0, help="cap rows scanned (0=all)")
    ap.add_argument("--probe", type=int, default=5, help="top N addresses to probe via REST")
    ap.add_argument("--out", default="scripts/spike_findings.md")
    args = ap.parse_args()

    conn = connect(args.db)
    t0 = time.time()
    top, coins, cnt, taker_ntl, rows, no_users = rank_makers(conn, args.days, args.limit_rows)
    scan_s = time.time() - t0

    # ---- market-level OI sanity (one call) ----
    mac = info({"type": "metaAndAssetCtxs"})
    market_oi = {}
    if isinstance(mac, list) and len(mac) == 2:
        universe = [u["name"] for u in mac[0]["universe"]]
        for name, ctx in zip(universe, mac[1]):
            if name in ("BTC", "ETH", "SOL", "HYPE", "ZEC"):
                market_oi[name] = {"openInterest": ctx.get("openInterest"),
                                   "markPx": ctx.get("markPx"),
                                   "dayNtlVlm": ctx.get("dayNtlVlm"),
                                   "funding": ctx.get("funding")}

    start_ms = int((time.time() - args.days * 86400) * 1000)
    probes = []
    for addr, _ in top[:args.probe]:
        probes.append((addr, probe_account(addr, start_ms)))
        time.sleep(0.15)

    # ---- write findings ----
    L = []
    L.append("# HL maker-analytics feasibility spike — findings\n")
    L.append(f"_window: last {args.days:.0f}d · HL trade rows scanned: {rows:,} "
             f"({no_users:,} without a usable users[] pair) · scan {scan_s:.1f}s_\n")

    L.append("\n## 1a. Top makers by maker-side notional (from stored `raw`)\n")
    L.append("| # | maker | maker vol ($) | mkts | maker trades | taker vol ($) | maker/taker |")
    L.append("|--:|---|--:|--:|--:|--:|--:|")
    for i, (addr, mv) in enumerate(top[:20], 1):
        tv = taker_ntl.get(addr, 0.0)
        ratio = (mv / tv) if tv else float("inf")
        ratio_s = "∞" if ratio == float("inf") else f"{ratio:.1f}x"
        L.append(f"| {i} | `{short(addr)}` | {mv:,.0f} | {len(coins[addr])} | "
                 f"{cnt[addr]:,} | {tv:,.0f} | {ratio_s} |")
    total_maker = sum(mv for _, mv in top)
    top20 = sum(mv for _, mv in top[:20])
    L.append(f"\n_total maker notional in window: ${total_maker:,.0f}; "
             f"top-20 share: {100*top20/total_maker:.1f}% of it ({len(top)} makers seen)_\n")

    L.append("\n## Market-level OI sanity (`metaAndAssetCtxs`)\n")
    L.append("```json")
    L.append(json.dumps(market_oi, indent=2))
    L.append("```")

    L.append("\n## 1b. Per-address endpoint probe (top makers)\n")
    for addr, p in probes:
        L.append(f"\n### `{short(addr)}`  (full: `{addr}`)")
        L.append("```json")
        L.append(json.dumps(p, indent=2))
        L.append("```")

    L.append("\n## Endpoint matrix\n")
    L.append("| endpoint | returns | gives us |")
    L.append("|---|---|---|")
    def ok(p, key, field):
        return all(field in pr[1].get(key, {}) for pr in probes) if probes else False
    L.append(f"| metaAndAssetCtxs | {'yes' if market_oi else 'NO'} | market OI, mark, day vol, funding |")
    L.append(f"| clearinghouseState | {'yes' if ok(probes,'clearinghouseState','open_interest_usd') else 'NO'} | per-maker OI, positions, unreal PnL, acct value |")
    L.append(f"| portfolio | {'yes' if ok(probes,'portfolio','day_pnlHistory_points') else 'NO'} | daily PnL + account-value history |")
    L.append(f"| userFunding | {'yes' if ok(probes,'userFunding','events') else 'NO'} | funding paid/received per account |")
    L.append(f"| userFees | {'yes' if ok(probes,'userFees','userAddRate') else 'NO'} | taker rate + maker rate (rebate) + 14d vol |")

    text = "\n".join(L) + "\n"
    with open(args.out, "w") as f:
        f.write(text)
    print(text)
    print(f"\n[written to {args.out}]")


if __name__ == "__main__":
    main()
