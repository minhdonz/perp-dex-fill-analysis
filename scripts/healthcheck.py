"""Liveness healthcheck — run from cron every 5 minutes (PRD §8.1: "know
within minutes if a feed dropped, not at publish time").

Checks data freshness per venue straight from the SQLite file:
- book_snapshots are the primary signal (every venue pushes books at
  sub-second cadence, so book silence means the feed/collector is down);
- trades are checked with a looser threshold (quiet markets are legitimate).

If a heartbeat URL is given (healthchecks.io style), pings $URL when healthy
and $URL/fail with a reason when not — so you get an email/push alert both on
failure and on the cron itself dying.

Stdlib only; exits 1 when unhealthy (also usable from shell).

  */5 * * * * .venv/bin/python scripts/healthcheck.py --db data/fills.db --ping https://hc-ping.com/<uuid>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.request

BOOK_SILENCE_LIMIT_S = 5 * 60
TRADE_SILENCE_LIMIT_S = 60 * 60


def check(db: str) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    now_ns = time.time_ns()
    problems = []
    venues = [r[0] for r in conn.execute("SELECT DISTINCT venue FROM trades")]
    if not venues:
        return ["no venues/trades in db at all"]
    for venue in venues:
        for table, limit_s, label in (
            ("book_snapshots", BOOK_SILENCE_LIMIT_S, "book"),
            ("trades", TRADE_SILENCE_LIMIT_S, "trades"),
        ):
            row = conn.execute(
                f"SELECT MAX(ts_ns) FROM {table} WHERE venue=?", (venue,)
            ).fetchone()
            last = row[0] or 0
            silence_s = (now_ns - last) / 1e9
            if silence_s > limit_s:
                problems.append(f"{venue}/{label} silent {silence_s/60:.0f}m")
    return problems


def ping(url: str, ok: bool, body: str) -> None:
    target = url if ok else url.rstrip("/") + "/fail"
    req = urllib.request.Request(target, data=body.encode() or b"ok", method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"heartbeat ping failed: {e}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/fills.db")
    p.add_argument("--ping", help="healthchecks.io-style heartbeat URL")
    args = p.parse_args()

    try:
        problems = check(args.db)
    except sqlite3.Error as e:
        problems = [f"cannot read db: {e}"]

    if args.ping:
        ping(args.ping, ok=not problems, body="; ".join(problems))
    if problems:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} UNHEALTHY: " + "; ".join(problems))
        sys.exit(1)


if __name__ == "__main__":
    main()
