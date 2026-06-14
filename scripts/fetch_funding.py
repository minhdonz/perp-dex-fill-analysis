"""Fetch trailing funding-rate history per venue+asset, normalize to a per-hour
fraction, store the mean in funding_rates. Cron'd like the fee fetcher.

Unit normalization (verified 2026-06-14 — see analysis/funding.py):
  HL       fundingHistory.fundingRate        -> fraction/hr (direct)
  Pacifica funding_rate/history.funding_rate  -> fraction/hr (direct)
  Lighter  fundings(resolution=1h).rate       -> PERCENT/hr  -> ÷100

  python scripts/fetch_funding.py data/fills.db --days 14
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from analysis.funding import HOURS_PER_YEAR, ensure_schema, stability
from collector.storage import connect

HL_INFO = "https://api.hyperliquid.xyz/info"
PACIFICA_HIST = "https://api.pacifica.fi/api/v1/funding_rate/history"
LIGHTER_ORDERBOOKS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
LIGHTER_FUNDINGS = "https://mainnet.zklighter.elliot.ai/api/v1/fundings"
ASSETS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]


def _get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def hl_hourly(coin, start_ms):
    d = _post(HL_INFO, {"type": "fundingHistory", "coin": coin, "startTime": start_ms})
    d.sort(key=lambda x: x["time"])  # chronological for flip-rate
    return [float(x["fundingRate"]) for x in d]  # fraction/hr


def pacifica_hourly(coin, limit):
    url = f"{PACIFICA_HIST}?{urllib.parse.urlencode({'symbol': coin, 'limit': limit})}"
    d = _get(url).get("data") or []
    d.sort(key=lambda x: int(x["created_at"]))  # endpoint returns newest-first
    return [float(x["funding_rate"]) for x in d]  # fraction/hr


def lighter_market_ids():
    d = _get(LIGHTER_ORDERBOOKS)
    return {ob["symbol"]: ob["market_id"] for ob in d.get("order_books", [])}


def lighter_hourly(market_id, start_s, end_s, count_back):
    q = urllib.parse.urlencode({
        "market_id": market_id, "resolution": "1h",
        "start_timestamp": start_s, "end_timestamp": end_s, "count_back": count_back})
    d = _get(f"{LIGHTER_FUNDINGS}?{q}")
    fundings = sorted(d.get("fundings", []), key=lambda f: f["timestamp"])
    # Lighter 'rate' is PERCENT/hr -> ÷100; 'direction' sets sign
    out = []
    for f in fundings:
        rate = float(f["rate"]) / 100.0
        if f.get("direction") == "short":
            rate = -rate
        out.append(rate)
    return out


def store(conn, venue, coin, rates, days):
    if not rates:
        print(f"  {venue}/{coin}: no samples")
        return
    mean_hourly, pct_pos, flip_rate, std = stability(rates)
    apr = mean_hourly * HOURS_PER_YEAR
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO funding_rates "
            "(venue, coin, mean_hourly, apr, pct_positive, flip_rate, std_hourly, "
            " n_samples, window_days, fetched_ns) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (venue, coin, mean_hourly, apr, pct_pos, flip_rate, std,
             len(rates), days, time.time_ns()))
    print(f"  {venue:<11} {coin:<5} APR={apr*100:+6.2f}%  flip={flip_rate*100:3.0f}%  "
          f"pos={pct_pos*100:3.0f}%  n={len(rates)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--venues", nargs="+", default=["hyperliquid", "pacifica", "lighter"])
    args = ap.parse_args()

    conn = connect(args.db)
    ensure_schema(conn)
    now_s = int(time.time())
    start_ms = int((now_s - args.days * 86400) * 1000)
    count_back = int(args.days * 24)

    lighter_ids = lighter_market_ids() if "lighter" in args.venues else {}

    for coin in ASSETS:
        for venue in args.venues:
            try:
                if venue == "hyperliquid":
                    store(conn, venue, coin, hl_hourly(coin, start_ms), args.days)
                elif venue == "pacifica":
                    store(conn, venue, coin, pacifica_hourly(coin, count_back), args.days)
                elif venue == "lighter":
                    mid = lighter_ids.get(coin)
                    if mid is None:
                        continue
                    store(conn, venue, coin,
                          lighter_hourly(mid, now_s - int(args.days * 86400), now_s, count_back),
                          args.days)
            except Exception as e:
                print(f"  {venue}/{coin}: FAIL {type(e).__name__}: {e}")
            time.sleep(0.1)


if __name__ == "__main__":
    main()
