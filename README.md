# perp-dex-fill-analysis — collector

Always-on, read-only ingestion of **trades** (realized) and **order book**
(advertised) feeds from Hyperliquid, Lighter, and Pacifica, normalized onto a
common clock and written to SQLite. No orders, no keys, no wallet — public
data only. See `collector-spec.md` (build brief) and
`perp-execution-quality-prd.md` (product rationale).

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# everything (3 venues x 5 assets) -> data/fills.db
.venv/bin/python -m collector

# subset
.venv/bin/python -m collector --venues hyperliquid --coins BTC --db data/spike.db
```

Stop with Ctrl-C (flushes cleanly). Designed to sit under systemd/supervisor
on a small VPS; the process reconnects forever on feed drops.

### Spike analysis (clip reconstruction + the gap)

```bash
.venv/bin/python -m analysis.spike --db data/spike.db --venue hyperliquid --coin BTC
```

Reconstructs clips from prints, matches each against the most recent book
snapshot ≤ clip start (tolerance 1s, actual `book_age_ms` reported per clip),
and prints advertised vs realized cost in bps per clip-size rung.

### Rollup / retention (run daily from cron)

```bash
.venv/bin/python -m analysis.rollup --db data/fills.db        # summarize + prune >90d
.venv/bin/python -m analysis.rollup --db data/fills.db --no-prune
```

Summarizes completed 2h UTC windows into `window_summaries`, marking any
window overlapping a disconnect/gap/stale integrity event as `has_gap=1`
(excluded from published numbers, never silently averaged). Granular rows
older than 90 days are pruned only after their windows are summarized.

## Layout

```
collector/
  models.py            TradeRecord / BookSnapshot / MarketStats (spec §1)
  storage.py           SQLite (WAL), batched writes, integrity log, schema
  base.py              reconnect loop, per-feed liveness, stale flagging
  venues/hyperliquid.py  trades + l2Book (spec §2.1)
  venues/lighter.py      trades + book deltas w/ nonce continuity + market_stats (§2.2)
  venues/pacifica.py     trades + book + REST backfill after disconnect (§2.3)
analysis/
  spike.py             clip reconstruction + advertised-vs-realized gap (§4, §7)
  rollup.py            2h-window summaries, gap marking, 90d retention (§5, §6)
scripts/probe_ws.py    one-off payload probe used during the spike
```

## Decisions made during the build (spec §8 open items)

- **ZEC listing:** all five assets (BTC, ETH, SOL, HYPE, ZEC) are listed and
  active on all three venues (verified 2026-06-12). Nothing dropped.
- **Storage:** SQLite, WAL mode. Single small-VPS writer, append-heavy,
  time-range reads; DuckDB can attach SQLite later if analysis wants columnar.
- **Clip windows:** defaults — HL/Lighter 200ms taker-identity grouping,
  Pacifica 200ms sweep heuristic. `--clip-window-ms` flag exists for the
  empirical calibration the spec calls for.
- **Lighter book emission:** the book channel is snapshot+delta; we maintain
  state locally and emit a top-50 snapshot row at most every 250ms per market
  (finer than HL's 0.5s floor; storing every delta would multiply rows for no
  matching precision). Nonce gaps trigger an integrity `gap` event + fresh
  resubscribe.
- **Pacifica REST backfill:** REST trade rows have no trade id, so backfilled
  rows get synthetic ids and the WS/backfill boundary is partitioned strictly
  by timestamp. If REST history doesn't reach back to the disconnect point,
  the hole is logged as a `gap` (window gets excluded at publish time).

## Honest seams (carried into the methodology section later)

- **HL liquidations:** the public trades feed doesn't flag liquidations
  (zero-hash also covers TWAP/internal fills), so `is_liquidation` is always
  false for HL. Lighter and Pacifica flag them natively.
- **HL book depth:** the `l2Book` WS feed exposes top 20 levels per side, not
  the spec's N=50 target. Clips deeper than visible depth are counted as
  "exceeds visible depth", never extrapolated.
- **Lighter book rows** are reconstructed from deltas; `raw` on those rows
  stores the trigger message type + offset/nonce cursor rather than a full
  venue-sent snapshot.
- **Clip confidence** is tagged per venue: Lighter `exact`
  (`taker_position_size_before`), Hyperliquid `identity` (taker address),
  Pacifica `heuristic` (sweep grouping, no taker id).

## Spike result (spec §7 gate — passed 2026-06-12)

~12 minutes of HL BTC: 2,064 prints → 930 clips (260 multi-print), 914
matched to a book within 1s, 16 unmatched (counted, not dropped). Largest
clip $1.26M / 93 prints: advertised 1.18bps, realized 2.11bps, gap +0.94bps
at `book_age` 270ms — larger clips show wider gaps, small clips sit at ~0, as
intuition requires. Lighter (`exact`) and Pacifica (`heuristic`) paths
verified on live captures of all five assets.
