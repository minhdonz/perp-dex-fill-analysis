"""One-time backfill: populate trades.taker_order_id for Lighter rows written
before the column existed, extracting the taker-side order id from raw.

Idempotent and resumable — only touches rows where taker_order_id IS NULL.
Run after deploying the schema migration:
    python scripts/backfill_taker_order_id.py data/fills.db
"""
import sqlite3
import sys

sys.path.insert(0, ".")
from collector.models import unjson
from collector.storage import connect

BATCH = 5000


def taker_order_from_raw(raw_blob) -> str | None:
    try:
        r = unjson(raw_blob)
    except Exception:
        return None
    is_maker_ask = r.get("is_maker_ask")
    if is_maker_ask is None:
        return None
    oid = (r.get("bid_id_str") or r.get("bid_id")) if is_maker_ask \
        else (r.get("ask_id_str") or r.get("ask_id"))
    return str(oid) if oid is not None else None


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/fills.db"
    conn = connect(db)  # runs migration if needed
    total = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE venue='lighter' AND taker_order_id IS NULL"
    ).fetchone()[0]
    print(f"lighter rows needing backfill: {total:,}")
    done = 0
    while True:
        rows = conn.execute(
            "SELECT trade_id, raw FROM trades "
            "WHERE venue='lighter' AND taker_order_id IS NULL LIMIT ?", (BATCH,)
        ).fetchall()
        if not rows:
            break
        updates = [(taker_order_from_raw(raw), tid) for tid, raw in rows]
        # rows with unparseable/missing id get '' so the WHERE NULL filter
        # doesn't loop forever on them
        updates = [(oid if oid is not None else "", tid) for oid, tid in updates]
        with conn:
            conn.executemany(
                "UPDATE trades SET taker_order_id=? WHERE venue='lighter' AND trade_id=?",
                updates,
            )
        done += len(rows)
        print(f"  {done:,}/{total:,}", end="\r", flush=True)
    print(f"\nbackfilled {done:,} rows")
    got = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE venue='lighter' AND taker_order_id!=''"
    ).fetchone()[0]
    print(f"lighter rows with a real order id now: {got:,}")


if __name__ == "__main__":
    main()
