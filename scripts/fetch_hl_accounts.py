"""Poll public Hyperliquid per-address state for the top makers (phase 2).

Seeds the maker set from analysis.makers.rank (top makers by maker notional,
gated by maker/taker ratio so directional takers are excluded), then for each
address polls the public Info endpoints and stores snapshots:

  clearinghouseState -> account_state (OI, positions, unrealized PnL, acct value)
  userFunding        -> account_state.funding_net (net usdc over the lookback)
  portfolio          -> account_pnl (daily PnL deltas)
  userFees           -> account_fees (taker bps + maker bps; negative = rebate)

All public, no auth. Mirrors scripts/fetch_hl_fees.py.

  python scripts/fetch_hl_accounts.py data/fills.db --top 25 --days 5 --min-ratio 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from analysis import makers
from analysis.fees import ensure_schema as ensure_fees_schema
from collector.models import zjson
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


def store_state(conn, addr, start_ms, days, now_ns):
    chs = info({"type": "clearinghouseState", "user": addr})
    if not isinstance(chs, dict) or "assetPositions" not in chs:
        return False
    aps = chs["assetPositions"]
    oi = sum(abs(float(p["position"]["positionValue"])) for p in aps)
    upnl = sum(float(p["position"]["unrealizedPnl"]) for p in aps)
    ms = chs.get("marginSummary", {})
    positions = [{"coin": p["position"]["coin"],
                  "szi": p["position"]["szi"],
                  "positionValue": p["position"]["positionValue"],
                  "unrealizedPnl": p["position"]["unrealizedPnl"]} for p in aps]

    uf = info({"type": "userFunding", "user": addr, "startTime": start_ms})
    funding_net = None
    if isinstance(uf, list):
        funding_net = 0.0
        for e in uf:
            try:
                funding_net += float(e["delta"]["usdc"])
            except Exception:
                pass

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_state (venue, address, ts_ns, account_value, "
            "open_interest, n_positions, unrealized_pnl, total_ntl_pos, funding_net, "
            "funding_days, positions) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (makers.VENUE, addr, now_ns, float(ms.get("accountValue", 0)), oi, len(aps),
             upnl, float(ms.get("totalNtlPos", 0)), funding_net, days, zjson(positions)))
    return True


def store_pnl(conn, addr):
    pf = info({"type": "portfolio", "user": addr})
    if not isinstance(pf, list):
        return 0
    windows = {w[0]: w[1] for w in pf if isinstance(w, list) and len(w) == 2}
    # 'month' carries a daily-cadence cumulative pnl series; diff to per-day deltas
    src = windows.get("month") or windows.get("allTime") or {}
    pnl_hist = src.get("pnlHistory") or []
    av_hist = dict(src.get("accountValueHistory") or [])
    rows, prev = [], None
    for ts_ms, cum in pnl_hist:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
        cum = float(cum)
        delta = None if prev is None else cum - prev
        prev = cum
        if delta is not None:
            av = av_hist.get(ts_ms)
            rows.append((makers.VENUE, addr, day, delta,
                         float(av) if av is not None else None))
    if rows:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO account_pnl (venue, address, day, pnl, account_value) "
                "VALUES (?,?,?,?,?)", rows)
    return len(rows)


def store_fees(conn, addr, now_ns):
    fees = info({"type": "userFees", "user": addr})
    if not isinstance(fees, dict) or "userCrossRate" not in fees:
        return
    taker = float(fees["userCrossRate"]) * 1e4
    maker = float(fees.get("userAddRate", 0)) * 1e4   # negative = rebate
    rows = fees.get("dailyUserVlm") or []
    vol14 = sum(float(r.get("userCross", 0)) + float(r.get("userAdd", 0)) for r in rows[-14:])
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_fees (venue, account, taker_bps, maker_bps, "
            "vol_14d_usd, fetched_ns) VALUES ('hyperliquid',?,?,?,?,?)",
            (addr, taker, maker, vol14, now_ns))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--days", type=float, default=5.0)
    ap.add_argument("--min-ratio", type=float, default=3.0)
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    conn = connect(args.db)
    makers.ensure_schema(conn)
    ensure_fees_schema(conn)

    ranked = makers.rank(conn, args.days, args.min_ratio, args.top)
    now_ns = time.time_ns()
    start_ms = int((time.time() - args.days * 86400) * 1000)
    with conn:
        for i, r in enumerate(ranked, 1):
            conn.execute(
                "INSERT INTO maker_accounts (venue, address, first_seen_ns, last_rank, "
                "maker_ntl, taker_ntl) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(venue, address) DO UPDATE SET last_rank=excluded.last_rank, "
                "maker_ntl=excluded.maker_ntl, taker_ntl=excluded.taker_ntl",
                (makers.VENUE, r["address"], now_ns, i, r["maker_ntl"], r["taker_ntl"]))

    ok = 0
    for r in ranked:
        addr = r["address"]
        got = store_state(conn, addr, start_ms, args.days, now_ns)
        npnl = store_pnl(conn, addr)
        store_fees(conn, addr, now_ns)
        ok += int(got)
        a = f"{addr[:6]}…{addr[-4:]}"
        print(f"  {a}  state={'ok' if got else 'MISS'}  pnl_days={npnl}")
        time.sleep(args.sleep)
    print(f"\nfetched {ok}/{len(ranked)} accounts")


if __name__ == "__main__":
    main()
