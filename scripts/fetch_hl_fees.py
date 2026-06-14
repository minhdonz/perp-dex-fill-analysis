"""Fetch real Hyperliquid per-account taker rates from the public `userFees`
endpoint and cache them in the account_fees table.

userFees returns the account's actual current rates (userCrossRate = taker,
userAddRate = maker) plus dailyUserVlm — no auth, no assumption. We fetch the
takers we actually observe (by descending observed volume so the accounts that
move the table are covered first), rate-limited, skipping rows fetched within
--max-age-days.

  python scripts/fetch_hl_fees.py data/fills.db --top 300
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from analysis.fees import ensure_schema
from collector.storage import connect

INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_user_fees(address: str, retries: int = 3):
    body = json.dumps({"type": "userFees", "user": address}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                INFO_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def vol_14d(d: dict) -> float:
    rows = d.get("dailyUserVlm") or []
    recent = rows[-14:]
    return sum(float(r.get("userCross", 0)) + float(r.get("userAdd", 0)) for r in recent)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--top", type=int, default=300, help="top N takers by observed volume")
    ap.add_argument("--min-vol", type=float, default=100_000, help="skip takers below $ observed vol")
    ap.add_argument("--max-age-days", type=float, default=3.0, help="re-fetch if cache older")
    ap.add_argument("--sleep", type=float, default=0.12, help="seconds between requests")
    args = ap.parse_args()

    conn = connect(args.db)
    ensure_schema(conn)
    fresh_ns = time.time_ns() - int(args.max_age_days * 86400 * 1e9)

    takers = conn.execute(
        "SELECT taker_id, SUM(CAST(notional_usd AS REAL)) vol FROM trades "
        "WHERE venue='hyperliquid' AND taker_id IS NOT NULL AND taker_id!='' "
        "GROUP BY taker_id HAVING vol >= ? ORDER BY vol DESC LIMIT ?",
        (args.min_vol, args.top),
    ).fetchall()
    print(f"candidate HL takers: {len(takers)}")

    fetched = skipped = failed = 0
    for addr, obs_vol in takers:
        cached = conn.execute(
            "SELECT fetched_ns FROM account_fees WHERE venue='hyperliquid' AND account=?",
            (addr,)).fetchone()
        if cached and cached[0] >= fresh_ns:
            skipped += 1
            continue
        try:
            d = fetch_user_fees(addr)
        except Exception as e:
            failed += 1
            print(f"  fail {addr[:10]}: {type(e).__name__}")
            continue
        taker = float(d.get("userCrossRate", 0)) * 1e4
        maker = float(d.get("userAddRate", 0)) * 1e4
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_fees "
                "(venue, account, taker_bps, maker_bps, vol_14d_usd, fetched_ns) "
                "VALUES ('hyperliquid', ?, ?, ?, ?, ?)",
                (addr, taker, maker, vol_14d(d), time.time_ns()))
        fetched += 1
        if fetched <= 5 or fetched % 50 == 0:
            print(f"  {addr[:12]} taker={taker:.2f}bps maker={maker:.2f}bps "
                  f"14d_vol=${vol_14d(d):,.0f}  (obs ${obs_vol:,.0f})")
        time.sleep(args.sleep)

    print(f"\nfetched {fetched}, skipped(fresh) {skipped}, failed {failed}")
    dist = conn.execute(
        "SELECT ROUND(taker_bps,1) t, COUNT(*) FROM account_fees "
        "WHERE venue='hyperliquid' GROUP BY t ORDER BY t").fetchall()
    print("taker-bps distribution across cached accounts:", dict(dist))


if __name__ == "__main__":
    main()
