# Collector Spec / Build Brief — Perp Execution Quality Report (v1)

**Companion to:** `perp-execution-quality-prd.md`
**Audience:** Minh, building in Claude Code. This doc is written so it can be handed *directly* to Claude Code as a build brief — API shapes are inline so it isn't guessing.
**Status:** Draft v0.1
**Scope:** v1 collectors only — Hyperliquid, Lighter, Pacifica. Assets: BTC, ETH, SOL, HYPE, ZEC (skip any asset a venue doesn't list). **Phoenix deferred** — it's an onchain Solana read (different ingestion path from the three websocket venues) and disproportionate build cost for a thin venue; add post-v1 to demonstrate the thin-venue failure mode.

---

## 0. What this component is

An always-on ingestion service that, per venue, subscribes to two public feeds — **trades** (realized) and **order book** (advertised) — normalizes both onto a common clock, and writes them to local storage. It places no orders, holds no keys, needs no wallet. Everything here is public read-only data.

Downstream analysis (computing the advertised-vs-realized gap, league tables, funding-inclusive cost) is **out of scope for this spec** — that reads from the stored data later. Build the collector first; the data has to exist before anything can analyze it.

**Build order (do not parallelize):**
1. Hyperliquid collector for BTC only → prove one clip's realized-vs-advertised number end to end. This is the spike. If clip reconstruction doesn't work here, stop and rethink before building the rest.
2. Generalize HL to all 5 assets.
3. Lighter, then Pacifica.
4. Storage rollup + gap detection (PRD §8).

---

## 1. Common data model (all venues normalize to this)

Two record types. Every collector maps its venue-specific payload into these.

### 1.1 `TradeRecord`
```
{
  venue:            string        // "hyperliquid" | "lighter" | "pacifica"
  coin:             string        // normalized symbol, e.g. "BTC"
  ts_ns:            int64         // event time, normalized to UTC nanoseconds (see §3)
  ts_venue_raw:     string        // original timestamp as received, unmodified (audit trail)
  price:            decimal
  size_base:        decimal       // size in base asset
  notional_usd:     decimal       // price * size_base
  aggressor_side:   "buy"|"sell"|null  // taker direction; null if unknowable
  taker_id:         string|null   // taker account identifier if exposed (for clip grouping)
  taker_size_before: decimal|null // taker's position size before this fill, if exposed
  trade_id:         string        // venue-unique trade id
  is_liquidation:   bool
  raw:              json          // full original message, for reprocessing
}
```

### 1.2 `BookSnapshot`
```
{
  venue:            string
  coin:             string
  ts_ns:            int64         // snapshot time, normalized (see §3)
  ts_venue_raw:     string
  bids:             [[price, size], ...]   // sorted best-first, top N levels (N=50 target)
  asks:             [[price, size], ...]
  raw:              json
}
```

**Why `raw` on both:** if a metric or matching rule changes later, you reprocess from raw rather than re-collecting (impossible for past data). This is cheap insurance and the reason the retention policy (§5) keeps raw for a window.

---

## 2. Per-venue ingestion

### 2.1 Hyperliquid `[FLAGSHIP — build first]`
- **WS endpoint:** `wss://api.hyperliquid.xyz/ws`
- **Subscribe (per coin):**
  - Trades: `{"method":"subscribe","subscription":{"type":"trades","coin":"BTC"}}`
  - Book: `{"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC"}}`
- **Trade payload** (`WsTrade[]`): `{ coin, side, px, sz, time, tid, hash, users:[buyer,seller] }`
  - `side` → `aggressor_side`. (HL `side` is the aggressor/taker side.)
  - `time` is **milliseconds** → `ts_ns = time * 1_000_000`.
  - **Clip grouping unlock:** `users[0]`/`users[1]` give buyer & seller addresses. The taker is the aggressor side's address. Group consecutive trades with the **same taker address + same direction within a short time window** into one clip. This is *better than a blind print-sweep* — you have the actual taker identity, so you're not guessing whether two prints were one sweep. → set `taker_id`.
  - `taker_size_before` → null (not on public trade feed; would need `userEvents` which is per-address/auth — skip).
- **Book payload** (`WsBook`): `{ coin, levels:[bids,asks], time }` where each level is `{px, sz, n}`.
  - **Critical constraint:** the book is a *snapshot* feed (full top-of-book state each push), pushed *on a block that is at least 0.5s since the last push* — i.e. **at most** every ~0.5s, a floor on the interval, not a fixed tick (real cadence depends on block timing and can be longer). Source: HL WS docs, `WsBook` definition. So your advertised-side resolution is no finer than ~0.5s. This sets the matching tolerance in §3.
- **Reconnect:** HL drops connections periodically without notice; on reconnect the snapshot ack is tagged `isSnapshot:true`. Handle reconnect + dedupe snapshot vs. already-seen data.

### 2.2 Lighter `[BEST DATA]`
- **WS endpoint:** `wss://mainnet.zklighter.elliot.ai/stream` (append `?readonly=true` if region-restricted)
- **Subscribe (per market index):**
  - Trades: `{"type":"subscribe","channel":"trade/{MARKET_INDEX}"}`
  - Book: `{"type":"subscribe","channel":"order_book/{MARKET_INDEX}"}`
  - Funding/stats: `{"type":"subscribe","channel":"market_stats/{MARKET_INDEX}"}`
- **Trade payload** (`Trade`): rich. Key fields:
  - `is_maker_ask` → derive `aggressor_side` **directly, no inference** (if maker is ask, taker is buy, etc.).
  - `size`, `price`, `usd_amount` → `size_base`, `price`, `notional_usd` (usd_amount is given, no need to compute).
  - **`taker_position_size_before`** → `taker_size_before`. This is the unlock: it tells you the taker's position size entering the trade, so you can reconstruct the *full intended clip* and bucket by true clip size rather than per-print. Lighter is the only v1 venue with this.
  - `timestamp` (ms) and `transaction_time` (**microseconds**) — use `transaction_time` for finest ordering: `ts_ns = transaction_time * 1_000`.
  - `ask_account_id`/`bid_account_id` → `taker_id` (the aggressor side's account).
- **Map market index → symbol** once at startup via the REST `orderBooks`/`orderBookDetails` endpoint; cache it.
- **Reconnect:** book channel sends full snapshot on subscribe then deltas; verify continuity via `begin_nonce` matching previous `nonce`. On gap, resubscribe for a fresh snapshot.

### 2.3 Pacifica
- **WS endpoint:** `wss://ws.pacifica.fi/ws`
- **Subscribe:** trades channel + book channel (per symbol). Also REST `GET /api/v1/trades?symbol=BTC` for **backfill after a disconnect**.
- **Trade payload:** `{ h, s, a, p, d, tc, t, li }`
  - `s` → coin, `a` → `size_base`, `p` → `price`, `t` (ms) → `ts_ns`.
  - `d` is direction (e.g. `close_short`, `open_long`) → derive `aggressor_side` from the open/close + long/short.
  - No taker id / size-before on public feed → `taker_id=null`, `taker_size_before=null`; clip grouping falls back to the **sweep heuristic** (§4).
- **Reconnect:** on gap, use REST `/api/v1/trades` to backfill the missed window before resuming live.

### 2.4 Phoenix `[DEFERRED — not in v1]`
Cut from v1. Rationale: it's an onchain Solana read (RPC/SDK event stream), a fundamentally different ingestion path from the three websocket venues, so it carries disproportionate build cost for a thin venue. Add post-v1, where its value is demonstrating the thin-venue failure mode (wide realized gap at size). When added, it normalizes into the same `TradeRecord`/`BookSnapshot` model; the only new work is the ingestion adapter and onchain block/slot-time → UTC normalization.


---

## 3. Timestamp normalization & matching rule `[the methodology-critical part]`

All four venues stamp time differently — normalize at ingestion:
- Hyperliquid: ms → `*1e6`
- Lighter: microseconds (`transaction_time`) → `*1e3`
- Pacifica: ms → `*1e6`

Store normalized `ts_ns` **and** original `ts_venue_raw` (never overwrite the source).

**Matching rule (advertised vs realized):** for each trade, the "advertised" book it's measured against = **the most recent `BookSnapshot` for that venue+coin with `ts_ns` ≤ trade `ts_ns`**, within a tolerance window.
- Tolerance is bounded by the *slowest* book feed: HL pushes book at ~0.5s cadence, so a tolerance tighter than ~0.5s would orphan HL trades. Propose **tolerance = 1s** for v1, recorded per-match so a venue with finer book updates isn't blurred to HL's resolution unnecessarily — store the actual `book_age_ms` (trade ts − matched snapshot ts) on each computed gap so analysis can filter on freshness.
- If no snapshot within tolerance → that trade is **unmatched**, excluded from the gap metric, and counted (so we report coverage, not silently drop).

> This rule is a methodology commitment, not just plumbing. Write `book_age_ms` into the output so a venue can't claim you compared its fill to a stale book without you being able to show exactly how stale.

---

## 4. Clip reconstruction

Goal: turn the stream of individual prints into **clips** (a single taker's market order that may have eaten multiple levels/prints), then measure realized cost of the *clip* vs. the book before it.

- **Lighter (exact):** use `taker_size_before` + `taker_id` — you know the taker and their position trajectory, so group their consecutive same-direction fills into one clip directly. Highest confidence.
- **Hyperliquid (near-exact):** use `taker_id` from `users[]` — group consecutive prints by same taker address + same direction within a short window (propose 50–200ms). Better than blind sweep because identity is known; confidence high.
- **Pacifica (heuristic):** no taker id → **sweep heuristic**: consecutive prints, same direction, same ~timestamp cluster, treated as one clip. Lower confidence; tag as such.

**Tag every clip with a `confidence` field** (`exact` | `identity` | `heuristic`) so the analysis layer and the published report can be honest about which venues' clip sizes are inferred. This is the §8/§9 "admit the seams" principle made concrete.

Clip-size buckets (the ladder, PRD §5.1): assign each reconstructed clip to the nearest rung — $10k / $25k / $100k / $500k / $1M / $5M notional. A clip only populates a rung if a clip of ~that size actually occurred; sparse rungs report sample count, not a fabricated number.

---

## 5. Storage & retention (PRD §8.2)

- Write `TradeRecord` and `BookSnapshot` to a local time-series-friendly store (SQLite/Parquet on disk is fine for v1 on a $5 VPS; don't over-engineer).
- Partition by `venue/coin/date` for easy pruning.
- **Retention:** full-granularity raw for **90 days** (decided), then roll into per-two-hour-window summaries (the 12 daily session buckets) and drop granular rows.
- Reprocessing must be possible from `raw` within the retention window.

---

## 6. Reliability (PRD §8.1) — build these alongside, not after

- **Per-feed liveness:** track last-message timestamp per venue+coin+feed; alert if silent beyond a threshold (e.g. trades feed silent >N min during active hours).
- **Reconnect + backfill:** auto-reconnect on drop; backfill via REST where the venue offers it (Pacifica yes; others flag the gap).
- **Gap marking:** any session-window with a detected gap is flagged in storage and **excluded from published numbers**, never silently averaged.
- **Integrity log:** a simple append-only log of disconnects/reconnects/gaps per venue, so at publish time you can state coverage honestly ("Venue X: 99.2% of windows clean this edition").

---

## 7. The spike's done-definition (gate before building the other two venues)

You've de-risked the thesis when, for **Hyperliquid BTC**, you can:
1. Capture trades + book continuously for a few hours.
2. Reconstruct at least a handful of clips at different sizes.
3. Compute, for one real clip: realized fill cost vs. the book immediately prior, in bps, with `book_age_ms` attached.
4. Eyeball it against intuition (a large clip should show a wider gap than a small one).

If step 2 or 3 is shaky — e.g. taker-address grouping doesn't cleanly isolate clips — that's the signal to revisit the methodology *before* sinking time into the other two collectors. Cheap failure here is the whole point of spiking.

---

## 8. Open build-time questions
- `[OPEN]` Confirm ZEC listing per venue; drop where absent.
- `[DECIDED]` Retention window = 90 days full-granularity, then roll to per-2h-window summaries.
- `[OPEN]` Clip-grouping time windows (HL 50–200ms, Pacifica cluster tolerance) — calibrate empirically during the spike.
- `[OPEN]` Storage choice (SQLite vs Parquet vs DuckDB) — pick during the spike based on what query shape the analysis layer wants.
