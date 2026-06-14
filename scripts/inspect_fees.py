"""Probe whether each venue's stored trade raw carries a per-fill fee, and in
what units. Read-only."""
import sqlite3
import sys
from decimal import Decimal

sys.path.insert(0, ".")
from collector.models import unjson

db = sys.argv[1] if len(sys.argv) > 1 else "data/fills.db"
c = sqlite3.connect(db)

FEE_HINTS = ("fee", "commission", "rebate")

for venue in ("hyperliquid", "lighter", "pacifica"):
    print(f"\n=== {venue} ===")
    rows = c.execute(
        "SELECT price, size_base, notional_usd, raw FROM trades "
        "WHERE venue=? AND coin='BTC' AND is_liquidation=0 ORDER BY ts_ns DESC LIMIT 3",
        (venue,)).fetchall()
    if not rows:
        print("  (no trades)")
        continue
    price, size, notional, raw = rows[0]
    r = unjson(raw)
    keys = sorted(r.keys())
    print("  raw keys:", keys)
    fee_keys = [k for k in r if any(h in k.lower() for h in FEE_HINTS)]
    print("  fee-like keys:", fee_keys or "NONE")
    for k in fee_keys:
        print(f"    {k} = {r[k]!r}")
    # if a fee field exists, show implied bps vs notional for a few trades
    for k in fee_keys:
        print(f"  implied bps from '{k}' (vs notional_usd):")
        for p, s, n, rw in rows:
            rr = unjson(rw)
            val = rr.get(k)
            if val is None:
                continue
            print(f"    notional=${float(n):,.2f}  {k}={val}  "
                  f"raw/notional*1e4={float(Decimal(str(val))/Decimal(n)*10000):.4f} "
                  f"(if {k} already USD)")
