"""Hyperliquid market-maker league (PRD maker-analytics, phase 2).

HL is the only venue exposing maker economics publicly (it's an on-chain L1).
The maker is recoverable from each trade's stored `raw` (users:[buyer, seller],
counterparty to the taker we already extract), so maker volume comes free from
data we already hold; OI / PnL / funding / fees come from per-address public
REST polls (see scripts/fetch_hl_accounts.py).

This module owns:
  - the schema for the maker tables,
  - an incremental per-2h-window rollup of maker/taker notional by address+coin
    (maker_windows), mirroring analysis/gap_rollup.py, and
  - ranking + league assembly (joins maker volume to the polled account state).

  python -m analysis.makers --db data/fills.db            # incremental rollup + print top
  python -m analysis.makers --db data/fills.db --recompute
"""
from __future__ import annotations

import argparse
import time
from collections import defaultdict

from collector.models import unjson
from collector.storage import connect

WINDOW_NS = 2 * 3600 * 1_000_000_000
VENUE = "hyperliquid"
# only persist an (address, coin, window) maker row above this notional, so the
# long tail of ~20k incidental makers doesn't bloat the table.
MAKER_FLOOR = 250_000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS maker_windows (
    venue TEXT NOT NULL,
    maker TEXT NOT NULL,
    coin TEXT NOT NULL,
    window_start_ns INTEGER NOT NULL,
    maker_ntl REAL NOT NULL,
    maker_n INTEGER NOT NULL,
    taker_ntl REAL NOT NULL,
    taker_n INTEGER NOT NULL,
    PRIMARY KEY (venue, maker, coin, window_start_ns)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_mw_win ON maker_windows (venue, window_start_ns);

CREATE TABLE IF NOT EXISTS maker_accounts (
    venue TEXT NOT NULL,
    address TEXT NOT NULL,
    first_seen_ns INTEGER NOT NULL,
    last_rank INTEGER,
    maker_ntl REAL,
    taker_ntl REAL,
    PRIMARY KEY (venue, address)
);

CREATE TABLE IF NOT EXISTS account_state (
    venue TEXT NOT NULL,
    address TEXT NOT NULL,
    ts_ns INTEGER NOT NULL,
    account_value REAL,
    open_interest REAL,
    n_positions INTEGER,
    unrealized_pnl REAL,
    total_ntl_pos REAL,
    funding_net REAL,        -- net funding usdc over the lookback window
    funding_days REAL,
    positions TEXT,          -- zlib json: per-coin [{coin,szi,positionValue,unrealizedPnl}]
    PRIMARY KEY (venue, address, ts_ns)
);

CREATE TABLE IF NOT EXISTS account_pnl (
    venue TEXT NOT NULL,
    address TEXT NOT NULL,
    day TEXT NOT NULL,       -- YYYY-MM-DD UTC
    pnl REAL,                -- daily PnL delta (usdc)
    account_value REAL,
    PRIMARY KEY (venue, address, day)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


# ---------- maker-volume rollup (from stored raw) ----------

def windows_to_do(conn, last_complete_ns, recompute):
    rows = conn.execute(
        f"SELECT DISTINCT (ts_ns/{WINDOW_NS})*{WINDOW_NS} w FROM trades "
        f"WHERE venue=? AND ts_ns < ? ORDER BY w", (VENUE, last_complete_ns)).fetchall()
    wins = [r[0] for r in rows]
    if recompute:
        return wins
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT window_start_ns FROM maker_windows WHERE venue=?", (VENUE,))}
    return [w for w in wins if w not in done]


def rollup_window(conn, w) -> int:
    cur = conn.execute(
        "SELECT coin, notional_usd, raw FROM trades WHERE venue=? AND ts_ns>=? AND ts_ns<?",
        (VENUE, w, w + WINDOW_NS))
    agg = defaultdict(lambda: [0.0, 0, 0.0, 0])  # (addr,coin) -> [m_ntl,m_n,t_ntl,t_n]
    for coin, ntl_s, raw in cur:
        t = unjson(raw)
        users = t.get("users") if isinstance(t, dict) else None
        if not users or len(users) < 2 or users[0] is None or users[1] is None:
            continue
        buy = t.get("side") == "B"
        taker = users[0] if buy else users[1]
        maker = users[1] if buy else users[0]
        ntl = float(ntl_s or 0)
        m = agg[(maker, coin)]; m[0] += ntl; m[1] += 1
        k = agg[(taker, coin)]; k[2] += ntl; k[3] += 1
    out = [(VENUE, addr, coin, w, v[0], v[1], v[2], v[3])
           for (addr, coin), v in agg.items() if v[0] >= MAKER_FLOOR]
    if out:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO maker_windows "
                "(venue, maker, coin, window_start_ns, maker_ntl, maker_n, taker_ntl, taker_n) "
                "VALUES (?,?,?,?,?,?,?,?)", out)
    return len(out)


def run_rollup(conn, recompute=False, verbose=True) -> int:
    ensure_schema(conn)
    last_complete = (time.time_ns() // WINDOW_NS) * WINDOW_NS
    wins = windows_to_do(conn, last_complete, recompute)
    total = 0
    for i, w in enumerate(wins, 1):
        n = rollup_window(conn, w)
        total += n
        if verbose:
            print(f"  window {i}/{len(wins)} {w} -> {n} maker rows")
    return total


# ---------- ranking + league assembly ----------

def rank(conn, days=5.0, min_ratio=0.0, limit=50):
    """Top makers by maker notional over the lookback, with a maker/taker gate
    (min_ratio) to isolate genuine two-sided makers from directional takers."""
    since = int((time.time() - days * 86400) * 1e9)
    rows = conn.execute(
        "SELECT maker, SUM(maker_ntl), SUM(maker_n), SUM(taker_ntl), SUM(taker_n), "
        "COUNT(DISTINCT coin) FROM maker_windows WHERE venue=? AND window_start_ns>=? "
        "GROUP BY maker", (VENUE, since)).fetchall()
    out = []
    for addr, mntl, mn, tntl, tn, ncoins in rows:
        ratio = (mntl / tntl) if tntl else float("inf")
        if ratio < min_ratio:
            continue
        out.append({"address": addr, "maker_ntl": mntl, "maker_n": mn,
                    "taker_ntl": tntl, "taker_n": tn, "tape_coins": ncoins, "ratio": ratio})
    out.sort(key=lambda r: r["maker_ntl"], reverse=True)
    return out[:limit]


def _latest_state(conn, addr):
    r = conn.execute(
        "SELECT account_value, open_interest, n_positions, unrealized_pnl, total_ntl_pos, "
        "funding_net, funding_days, ts_ns FROM account_state WHERE venue=? AND address=? "
        "ORDER BY ts_ns DESC LIMIT 1", (VENUE, addr)).fetchone()
    if not r:
        return None
    keys = ["account_value", "open_interest", "n_positions", "unrealized_pnl",
            "total_ntl_pos", "funding_net", "funding_days", "ts_ns"]
    return dict(zip(keys, r))


def _recent_pnl(conn, addr, days=7):
    rows = conn.execute(
        "SELECT day, pnl FROM account_pnl WHERE venue=? AND address=? ORDER BY day DESC LIMIT ?",
        (VENUE, addr, days)).fetchall()
    return [{"day": d, "pnl": p} for d, p in reversed(rows)]


def _fees(conn, addr):
    r = conn.execute(
        "SELECT taker_bps, maker_bps, vol_14d_usd FROM account_fees "
        "WHERE venue='hyperliquid' AND account=?", (addr,)).fetchone()
    return {"taker_bps": r[0], "maker_bps": r[1], "vol_14d_usd": r[2]} if r else None


def league(conn, days=5.0, min_ratio=2.0, top=20):
    """Full per-maker league: tape volume + polled account state/PnL/funding/fees."""
    ranked = rank(conn, days, min_ratio, top)
    total_maker = conn.execute(
        "SELECT SUM(maker_ntl) FROM maker_windows WHERE venue=? AND window_start_ns>=?",
        (VENUE, int((time.time() - days * 86400) * 1e9))).fetchone()[0] or 1.0
    out = []
    for i, r in enumerate(ranked, 1):
        addr = r["address"]
        st = _latest_state(conn, addr)
        fees = _fees(conn, addr)
        rebate_earned = None
        if fees and fees["maker_bps"] is not None and fees["maker_bps"] < 0:
            rebate_earned = r["maker_ntl"] * (-fees["maker_bps"]) / 1e4
        out.append({
            "rank": i, "address": addr,
            "maker_ntl": r["maker_ntl"], "maker_share": r["maker_ntl"] / total_maker,
            "ratio": r["ratio"], "tape_coins": r["tape_coins"],
            "state": st, "pnl": _recent_pnl(conn, addr), "fees": fees,
            "rebate_earned_window": rebate_earned,
        })
    return {"days": days, "min_ratio": min_ratio, "total_maker_ntl": total_maker, "makers": out}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/fills.db")
    ap.add_argument("--days", type=float, default=5.0)
    ap.add_argument("--min-ratio", type=float, default=2.0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--recompute", action="store_true")
    args = ap.parse_args()
    conn = connect(args.db)
    cells = run_rollup(conn, args.recompute)
    print(f"maker rollup: {cells} maker-rows written\n")
    lg = league(conn, args.days, args.min_ratio, args.top)
    tot = lg["total_maker_ntl"]
    print(f"top {args.top} makers (last {args.days:.0f}d, maker/taker >= {args.min_ratio}):")
    print(f"  {'#':>2} {'address':<14} {'maker $':>14} {'share':>6} {'ratio':>7} "
          f"{'OI $':>12} {'mkr bps':>8}")
    for m in lg["makers"]:
        a = m["address"]
        ratio = "inf" if m["ratio"] == float("inf") else f"{m['ratio']:.1f}x"
        oi = f"{m['state']['open_interest']:,.0f}" if m["state"] else "-"
        mk = f"{m['fees']['maker_bps']:.2f}" if m["fees"] else "-"
        print(f"  {m['rank']:>2} {a[:6]}…{a[-4:]:<5} {m['maker_ntl']:>14,.0f} "
              f"{100*m['maker_share']:>5.1f}% {ratio:>7} {oi:>12} {mk:>8}")


if __name__ == "__main__":
    main()
