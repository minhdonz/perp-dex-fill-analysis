"""Probe Lighter clip-delimiting fields on stored data. Checks whether the
taker-side order id (bid_id for buys / ask_id for sells, from raw) is constant
across the fills of one market order — i.e. an exact, window-free clip key.
Not part of the service."""
import sqlite3
import sys
from decimal import Decimal

sys.path.insert(0, ".")
from collector.models import unjson

db = sys.argv[1] if len(sys.argv) > 1 else "data/fills.db"
coin = sys.argv[2] if len(sys.argv) > 2 else "BTC"
c = sqlite3.connect(db)

taker = c.execute(
    "SELECT taker_id FROM trades "
    "WHERE venue='lighter' AND coin=? AND taker_id IS NOT NULL "
    "AND ts_ns > (SELECT MAX(ts_ns)-1800000000000 FROM trades WHERE venue='lighter' AND coin=?) "
    "GROUP BY taker_id ORDER BY COUNT(*) DESC LIMIT 1", (coin, coin)).fetchone()[0]
print(f"busiest {coin} taker: {taker}\n")

rows = c.execute(
    "SELECT ts_ns, aggressor_side, size_base, taker_size_before, raw "
    "FROM trades WHERE venue='lighter' AND coin=? AND taker_id=? "
    "ORDER BY ts_ns LIMIT 45", (coin, taker)).fetchall()

print(f"{'ts_ns':>22} {'side':>4} {'size':>10} {'taker_order_id':>20} {'gap_us':>8}")
prev_oid = None
prev_ts = None
for ts, side, sz, sb, raw in rows:
    r = unjson(raw)
    oid = r.get("bid_id_str") or r.get("bid_id") if side == "buy" else (r.get("ask_id_str") or r.get("ask_id"))
    gap_us = (ts - prev_ts) / 1000 if prev_ts else 0
    mark = "" if oid != prev_oid else "  <-same order"
    print(f"{ts:>22} {side:>4} {str(sz):>10} {str(oid):>20} {gap_us:>8.0f}{mark}")
    prev_oid, prev_ts = oid, ts
