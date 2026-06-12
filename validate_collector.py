#!/usr/bin/env python3
"""
validate_collector.py — integrity checks + one-clip gap spike for the
Perp Execution Quality collector SQLite DB.

Usage:
    python3 validate_collector.py /path/to/collector.db
    python3 validate_collector.py /path/to/collector.db --window-min 5 --coin BTC

What it does (read-only; never writes to your DB):
    0. Introspect schema — find the trades table and books table, map columns
       to the logical model, and print what it found so you can confirm.
    1. Aggressor-side coverage per venue (the field most likely silently null).
    2. Timestamp normalization sanity across venues (catches unit errors).
    3. Book-snapshot presence + matchability within the 1s tolerance.
    4. Spike: reconstruct one large HL clip and compute realized-vs-advertised
       gap in bps, with book_age_ms attached.

It assumes ts_ns is integer UTC nanoseconds and bids/asks/raw are JSON text.
If your column names differ it will try to auto-map; override with the flags
printed in the schema section if the guess is wrong.
"""

import argparse
import json
import sqlite3
import sys
import time
import zlib
from collections import defaultdict

TOLERANCE_NS = 1_000_000_000  # 1 second, the §3 matching tolerance
NS_PER_MS = 1_000_000

# ----- column name candidates (lowercased) for fuzzy mapping -----
CANDIDATES = {
    "venue":            ["venue", "exchange", "ex"],
    "coin":             ["coin", "symbol", "asset", "market"],
    "ts_ns":            ["ts_ns", "timestamp_ns", "ts", "time_ns"],
    "price":            ["price", "px", "p"],
    "size_base":        ["size_base", "size", "sz", "amount", "a", "qty"],
    "notional_usd":     ["notional_usd", "notional", "usd_amount", "usd"],
    "aggressor_side":   ["aggressor_side", "side", "taker_side", "direction", "d"],
    "taker_id":         ["taker_id", "taker", "taker_account", "user"],
    "taker_size_before":["taker_size_before", "taker_position_size_before", "size_before"],
    "trade_id":         ["trade_id", "tid", "id"],
    "is_liquidation":   ["is_liquidation", "liquidation", "li", "is_liq"],
    "bids":             ["bids", "bid", "bid_levels"],
    "asks":             ["asks", "ask", "ask_levels"],
}


def c(s):  # tiny color helper
    return s


def ok(msg):    print(f"  [PASS] {msg}")
def warn(msg):  print(f"  [WARN] {msg}")
def fail(msg):  print(f"  [FAIL] {msg}")
def info(msg):  print(f"  {msg}")


def get_tables(con):
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return [r[0] for r in rows]


def get_cols(con, table):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]  # column names


def fuzzy_map(cols):
    """Map logical field -> actual column name, by candidate list."""
    lower = {col.lower(): col for col in cols}
    mapping = {}
    for logical, cands in CANDIDATES.items():
        for cand in cands:
            if cand in lower:
                mapping[logical] = lower[cand]
                break
    return mapping


def pick_trades_table(con, tables):
    """Heuristic: the trades table has price+size-ish columns, not bids/asks."""
    best, best_score = None, -1
    for t in tables:
        cols = [x.lower() for x in get_cols(con, t)]
        score = 0
        if any(x in cols for x in ["price", "px", "p"]): score += 1
        if any(x in cols for x in ["size", "sz", "size_base", "a", "amount"]): score += 1
        if any(x in cols for x in ["side", "aggressor_side", "d", "direction"]): score += 1
        if any(x in cols for x in ["bids", "asks"]): score -= 3  # that's the book table
        if "trade" in t.lower(): score += 2
        if score > best_score:
            best, best_score = t, score
    return best


def pick_books_table(con, tables):
    best, best_score = None, -1
    for t in tables:
        cols = [x.lower() for x in get_cols(con, t)]
        score = 0
        if "bids" in cols or "bid" in cols: score += 2
        if "asks" in cols or "ask" in cols: score += 2
        if "book" in t.lower() or "snapshot" in t.lower(): score += 2
        if score > best_score:
            best, best_score = t, score
    return best if best_score > 0 else None


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# ----------------------------------------------------------------------
# Check 0 — schema introspection
# ----------------------------------------------------------------------
def check_schema(con):
    section("0. SCHEMA — what the validator found")
    tables = get_tables(con)
    info(f"Tables in DB: {tables}")

    tt = pick_trades_table(con, tables)
    bt = pick_books_table(con, tables)
    if not tt:
        fail("Could not identify a trades table. Pass --trades-table explicitly.")
        sys.exit(1)
    info(f"Trades table  -> {tt}")
    info(f"Books table   -> {bt if bt else '(none found — Check 3 will fail)'}")

    tmap = fuzzy_map(get_cols(con, tt))
    bmap = fuzzy_map(get_cols(con, bt)) if bt else {}
    info(f"Trade column map: {tmap}")
    if bt:
        info(f"Book column map:  {bmap}")

    for need in ["venue", "coin", "ts_ns", "price", "size_base", "aggressor_side"]:
        if need not in tmap:
            warn(f"Trade column for '{need}' not auto-found — some checks may skip.")
    return tt, bt, tmap, bmap


# ----------------------------------------------------------------------
# Check 1 — aggressor-side coverage per venue
# ----------------------------------------------------------------------
def check_aggressor(con, tt, tmap, since_ns):
    section("1. AGGRESSOR-SIDE COVERAGE (null = unusable for the gap metric)")
    if "aggressor_side" not in tmap or "venue" not in tmap:
        warn("Missing aggressor_side or venue column; skipping.")
        return
    v, a, ts = tmap["venue"], tmap["aggressor_side"], tmap["ts_ns"]
    rows = con.execute(
        f"SELECT {v}, "
        f"  SUM(CASE WHEN {a} IS NULL OR {a}='' THEN 1 ELSE 0 END) AS nulls, "
        f"  COUNT(*) AS total "
        f"FROM {tt} WHERE {ts} >= ? GROUP BY {v}",
        (since_ns,),
    ).fetchall()
    if not rows:
        warn("No trades in the window. Widen --window-min or check the collector.")
        return
    for venue, nulls, total in rows:
        pct = 100.0 * nulls / total if total else 0
        line = f"{venue:<14} {total:>7} trades, {pct:5.1f}% null aggressor"
        if pct == 0:
            ok(line)
        elif pct < 5:
            warn(line + "  (minor — investigate the null cases)")
        else:
            fail(line + "  (parsing likely broken for this venue)")


# ----------------------------------------------------------------------
# Check 2 — timestamp normalization sanity across venues
# ----------------------------------------------------------------------
def check_timestamps(con, tt, tmap, since_ns):
    section("2. TIMESTAMP NORMALIZATION (catches ms/us/ns unit errors)")
    if "venue" not in tmap or "ts_ns" not in tmap:
        warn("Missing venue or ts_ns column; skipping.")
        return
    v, ts = tmap["venue"], tmap["ts_ns"]
    rows = con.execute(
        f"SELECT {v}, MIN({ts}), MAX({ts}), COUNT(*) FROM {tt} "
        f"WHERE {ts} >= ? GROUP BY {v}",
        (since_ns,),
    ).fetchall()
    now_ns = time.time() * 1e9
    maxes = []
    for venue, mn, mx, cnt in rows:
        # sanity: a normalized ns timestamp for "now" is ~1.7e18
        order = len(str(int(mx))) if mx else 0
        flag = ""
        if order != 19:
            flag = f"  <-- digit count {order}, expected 19 for UTC ns. UNIT BUG?"
        drift_s = (now_ns - mx) / 1e9 if mx else None
        info(f"{venue:<14} max_ts={int(mx)} ({order} digits) "
             f"newest≈{drift_s:5.1f}s ago{flag}")
        maxes.append(mx)
    if len(maxes) >= 2:
        spread_s = (max(maxes) - min(maxes)) / 1e9
        if spread_s < 120:
            ok(f"Cross-venue newest-trade spread = {spread_s:.1f}s (consistent clocks)")
        else:
            fail(f"Cross-venue spread = {spread_s:.1f}s — one venue's clock is off. "
                 f"Likely a unit-conversion bug (us vs ms vs ns).")


# ----------------------------------------------------------------------
# Check 3 — book snapshots present & matchable within tolerance
# ----------------------------------------------------------------------
def check_books(con, tt, bt, tmap, bmap, since_ns):
    section("3. BOOK SNAPSHOTS — present and matchable within 1s tolerance")
    if not bt:
        fail("No books table found. Every trade is unmatched → no gap metric. "
             "Check the book-feed subscription wired up.")
        return
    bv, bc, bts = bmap.get("venue"), bmap.get("coin"), bmap.get("ts_ns")
    if not (bv and bts):
        warn("Book table missing venue/ts_ns mapping; partial check only.")
    # per-venue book counts + cadence
    counts = con.execute(
        f"SELECT {bv}, COUNT(*) FROM {bt} WHERE {bts} >= ? GROUP BY {bv}",
        (since_ns,),
    ).fetchall()
    if not counts:
        fail("Zero book snapshots in window — book feed not writing.")
        return
    for venue, cnt in counts:
        ok(f"{venue:<14} {cnt:>6} book snapshots stored")

    # matchability: sample trades, find nearest prior book within tolerance
    tv, tc, tts = tmap["venue"], tmap["coin"], tmap["ts_ns"]
    sample = con.execute(
        f"SELECT {tv}, {tc}, {tts} FROM {tt} WHERE {tts} >= ? "
        f"ORDER BY RANDOM() LIMIT 200",
        (since_ns,),
    ).fetchall()
    matched, ages = 0, []
    for venue, coin, t_ns in sample:
        row = con.execute(
            f"SELECT MAX({bts}) FROM {bt} "
            f"WHERE {bv}=? AND {bc}=? AND {bts} <= ? AND {bts} >= ?",
            (venue, coin, t_ns, t_ns - TOLERANCE_NS),
        ).fetchone()
        if row and row[0]:
            matched += 1
            ages.append((t_ns - row[0]) / NS_PER_MS)
    if sample:
        rate = 100.0 * matched / len(sample)
        line = f"{matched}/{len(sample)} sampled trades matched a book ≤1s prior ({rate:.0f}%)"
        if rate > 90:
            ok(line)
        elif rate > 50:
            warn(line + "  (some venues may have sparse book updates)")
        else:
            fail(line + "  (matching is mostly failing — gap metric won't work)")
        if ages:
            ages.sort()
            med = ages[len(ages)//2]
            info(f"book_age_ms: median={med:.0f}  min={ages[0]:.0f}  max={ages[-1]:.0f}")


# ----------------------------------------------------------------------
# Check 4 — the spike: one HL clip's realized-vs-advertised gap
# ----------------------------------------------------------------------
def json_levels(val):
    """Parse a bids/asks cell into [[price,size],...].

    Cells may be plain JSON text OR zlib-compressed JSON — the collector
    compresses its JSON columns (raw/bids/asks) to cut disk usage, so handle
    both transparently here.
    """
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return val
    if isinstance(val, (bytes, bytearray)):
        try:
            val = zlib.decompress(val)
        except zlib.error:
            pass  # not compressed — let json.loads try the raw bytes
    try:
        parsed = json.loads(val)
    except Exception:
        return []
    out = []
    for lvl in parsed:
        if isinstance(lvl, dict):
            px = lvl.get("px") or lvl.get("price")
            sz = lvl.get("sz") or lvl.get("size")
            if px is not None and sz is not None:
                out.append([float(px), float(sz)])
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            out.append([float(lvl[0]), float(lvl[1])])
    return out


def vwap_fill(levels, target_base):
    """Walk book levels, fill target_base size, return (vwap, filled, exhausted)."""
    remaining = target_base
    cost = 0.0
    for px, sz in levels:
        take = min(remaining, sz)
        cost += take * px
        remaining -= take
        if remaining <= 1e-12:
            break
    filled = target_base - max(remaining, 0)
    if filled <= 0:
        return None, 0.0, True
    return cost / filled, filled, remaining > 1e-9


def check_spike(con, tt, bt, tmap, bmap, since_ns, coin):
    section(f"4. SPIKE — one Hyperliquid {coin} clip: realized vs advertised (bps)")
    if not bt:
        warn("No books table — can't compute the gap. Fix Check 3 first.")
        return
    needed = ["venue", "coin", "ts_ns", "price", "size_base", "aggressor_side"]
    if any(n not in tmap for n in needed):
        warn(f"Trade table missing one of {needed}; skipping spike.")
        return
    tv, tc, tts = tmap["venue"], tmap["coin"], tmap["ts_ns"]
    tp, tsz, tside = tmap["price"], tmap["size_base"], tmap["aggressor_side"]
    bv, bc, bts = bmap["venue"], bmap["coin"], bmap["ts_ns"]
    bbids, basks = bmap.get("bids"), bmap.get("asks")
    if not (bbids and basks):
        warn("Book table has no bids/asks columns mapped; skipping spike.")
        return

    # find the single largest HL trade in the window (proxy for a notable clip).
    # CAST to REAL: price/size/notional are stored as exact decimal strings, so
    # without the cast SQLite both errors on numeric formatting AND orders them
    # lexically (where '9' sorts after '1000000', picking the wrong "largest").
    notional_expr = (
        f"CAST({tmap['notional_usd']} AS REAL)" if "notional_usd" in tmap
        else f"(CAST({tp} AS REAL)*CAST({tsz} AS REAL))"
    )
    row = con.execute(
        f"SELECT {tts}, {tp}, {tsz}, {tside}, {notional_expr} AS notional "
        f"FROM {tt} WHERE LOWER({tv})='hyperliquid' AND {tc}=? AND {tts} >= ? "
        f"ORDER BY notional DESC LIMIT 1",
        (coin, since_ns),
    ).fetchone()
    if not row:
        warn(f"No Hyperliquid {coin} trades in window. Widen --window-min.")
        return
    t_ns, price, size, side, notional = row
    info(f"Largest HL {coin} print: notional≈${notional:,.0f}  "
         f"size={size}  price={price}  side={side}")
    info("(NOTE: this is a single print, not yet a grouped clip — see caveat below.)")

    # nearest prior book within tolerance
    brow = con.execute(
        f"SELECT {bts}, {bbids}, {basks} FROM {bt} "
        f"WHERE {bv} LIKE '%hyperliquid%' AND {bc}=? AND {bts} <= ? AND {bts} >= ? "
        f"ORDER BY {bts} DESC LIMIT 1",
        (coin, t_ns, t_ns - TOLERANCE_NS),
    ).fetchone()
    if not brow:
        fail("No book snapshot within 1s before this trade — can't compute gap.")
        return
    b_ns, bids_raw, asks_raw = brow
    book_age_ms = (t_ns - b_ns) / NS_PER_MS

    side_l = str(side).lower()
    is_buy = side_l in ("buy", "b", "bid", "long", "open_long", "close_short")
    levels = json_levels(asks_raw) if is_buy else json_levels(bids_raw)
    if not levels:
        warn("Book side empty/unparseable; check how bids/asks are stored.")
        return
    levels.sort(key=lambda x: x[0], reverse=not is_buy)  # asks asc, bids desc

    best = levels[0][0]
    vwap, filled, exhausted = vwap_fill(levels, float(size))
    if vwap is None:
        warn("Could not fill against book.")
        return
    # advertised cost = slippage implied by walking the visible book
    advertised_bps = abs(vwap - best) / best * 1e4
    # realized cost = actual fill price vs best quote at snapshot
    realized_bps = abs(float(price) - best) / best * 1e4
    gap_bps = realized_bps - advertised_bps

    info("")
    info(f"  best quote (snapshot)   : {best}")
    info(f"  advertised VWAP @ size  : {vwap:.2f}   ({advertised_bps:.2f} bps from best)")
    info(f"  realized print price    : {price}   ({realized_bps:.2f} bps from best)")
    info(f"  ADVERTISED-vs-REALIZED  : {gap_bps:+.2f} bps")
    info(f"  book_age_ms             : {book_age_ms:.0f}")
    if exhausted:
        warn("Book exhausted before filling full size — advertised number is a floor.")
    if book_age_ms > 600:
        warn(f"book_age_ms={book_age_ms:.0f} > 600 — stale-ish even for HL; flag in analysis.")
    print("""
  CAVEAT: this spike uses the single largest *print* as a stand-in. The real
  §7 gate requires grouping consecutive same-taker, same-direction prints
  (via users[] taker id) into one CLIP, then filling the clip's total size
  against the book. This gives you the end-to-end number; clip grouping is
  the next thing to verify once these checks are green.
""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="path to collector SQLite DB")
    ap.add_argument("--window-min", type=int, default=10,
                    help="look-back window in minutes (default 10)")
    ap.add_argument("--coin", default="BTC", help="coin for the spike (default BTC)")
    ap.add_argument("--trades-table", default=None)
    ap.add_argument("--books-table", default=None)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = None
    since_ns = int((time.time() - args.window_min * 60) * 1e9)

    print(f"\nValidating: {args.db}")
    print(f"Window: last {args.window_min} min   Spike coin: {args.coin}")

    tt, bt, tmap, bmap = check_schema(con)
    if args.trades_table:
        tt = args.trades_table; tmap = fuzzy_map(get_cols(con, tt))
    if args.books_table:
        bt = args.books_table; bmap = fuzzy_map(get_cols(con, bt))

    check_aggressor(con, tt, tmap, since_ns)
    check_timestamps(con, tt, tmap, since_ns)
    check_books(con, tt, bt, tmap, bmap, since_ns)
    check_spike(con, tt, bt, tmap, bmap, since_ns, args.coin)

    section("DONE")
    print("  Green on checks 1–3 → data is trustworthy to keep accumulating.")
    print("  Check 4 prints one gap number → sanity-check it's plausible,")
    print("  then move to clip grouping for the real §7 gate.\n")
    con.close()


if __name__ == "__main__":
    main()
