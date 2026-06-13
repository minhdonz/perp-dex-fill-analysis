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

## Deploy to a VPS (one command)

On a fresh Ubuntu 22.04/24.04 box (~$5/month, 1 vCPU / 1GB is plenty):

```bash
# optional but recommended — free alerting via healthchecks.io:
export HEALTHCHECK_URL=https://hc-ping.com/<your-uuid>
curl -fsSL https://raw.githubusercontent.com/minhdonz/perp-dex-fill-analysis/main/deploy/setup.sh | bash
```

This installs deps, clones the repo, starts the collector under systemd
(`KillSignal=SIGINT` so stops flush cleanly, `Restart=always`), and adds cron
entries for the nightly rollup and a 5-minute liveness healthcheck
(`scripts/healthcheck.py` — flags any venue whose book feed is >5 min silent
or trade feed >60 min silent, and pings/fails the heartbeat URL so you get an
alert within minutes, per PRD §8.1). Re-running the script updates the deploy.

Watch it: `journalctl -u collector -f`

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
  reconstruct.py       canonical clip reconstruction + gap measurement library
  league.py            cross-venue league table — PRD §6.1 "The Gap" (headline)
  clips.py             single-venue calibration + grouping audit + gap
  spike.py             original BTC spike (superseded by reconstruct/clips)
  rollup.py            2h-window summaries, gap marking, 90d retention (§5, §6)
scripts/probe_ws.py    one-off payload probe used during the spike
scripts/healthcheck.py cron liveness check + heartbeat ping (PRD §8.1)
scripts/backfill_taker_order_id.py  one-time Lighter order-id backfill
deploy/                systemd unit + one-command VPS bootstrap
```

## Analysis pipeline (the product)

```bash
# The headline: advertised vs realized gap, venues ranked per rung, all assets
.venv/bin/python -m analysis.league --db data/fills.db --hours 6

# Single-venue calibration + grouping audit (per-clip detail)
.venv/bin/python -m analysis.clips --db data/fills.db --venue lighter --coin BTC --hours 4
```

**Clip reconstruction is venue-aware, by what each feed exposes (PRD §5.2):**

| Venue | Method | Confidence | Clip key |
|---|---|---|---|
| Lighter | exact | `exact` | **taker order id** (`bid_id`/`ask_id`) — one market order = one id across all its fills; window-free. Verified live: 100% coverage, fills tens of µs apart, orders seconds apart. |
| Hyperliquid | identity | `identity` | taker address (`users[]`) + direction within a time window (no public order id; prints share one ms timestamp, so the 150ms window is insensitive) |
| Pacifica | heuristic | `heuristic` | sweep of consecutive same-direction prints (no taker id at all) |

`taker_position_size_before` (Lighter) chains per-fill as a running position —
useful for §5.3 hold-length/funding work, but it does **not** delimit orders
(it never resets); the order id does. Every league-table cell carries its
confidence label and sample count; sparse cells (n below `--min-samples`) are
shown but de-ranked so a quiet rung can't claim a misleading #1.

## Decisions made during the build (spec §8 open items)

- **ZEC listing:** all five assets (BTC, ETH, SOL, HYPE, ZEC) are listed and
  active on all three venues (verified 2026-06-12). Nothing dropped.
- **Storage:** SQLite, WAL mode. Single small-VPS writer, append-heavy,
  time-range reads; DuckDB can attach SQLite later if analysis wants columnar.
- **Clip windows:** defaults — HL/Lighter 200ms taker-identity grouping,
  Pacifica 200ms sweep heuristic. `--clip-window-ms` flag exists for the
  empirical calibration the spec calls for.
- **Lighter book emission:** the book channel is snapshot+delta; we maintain
  state locally and emit a top-50 snapshot row at most every 500ms per market.
  Nonce gaps trigger an integrity `gap` event + fresh resubscribe.
- **Storage rate (measured live):** uncapped, books alone ran ~10GB/day
  across 3 venues x 5 assets — far past the PRD's disk assumption. Fixes:
  all JSON columns are zlib-compressed (read back via
  `collector.models.unjson`), book storage is throttled to the methodology's
  0.5s resolution floor on every venue, and book retention is tiered
  (full 21 days -> 1/minute to 90 days -> summaries). Measured after fixes:
  ~2.5-3GB/day during active hours; steady-state disk at full retention
  ~75-100GB. Trades keep full 90-day raw per the spec decision.
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
