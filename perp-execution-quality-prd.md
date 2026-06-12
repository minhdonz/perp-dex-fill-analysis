# PRD — Perp Execution Quality Report (working title: "RealizedFill")

**Author:** Minh
**Status:** Draft v0.1 — for review
**Last updated:** 12 Jun 2026

> Reviewer note: comment inline. Open questions are flagged `[OPEN]` throughout; decisions I've made for you are flagged `[DECISION]` so you can overrule them.

---

## 1. Context & why this exists

### 1.1 The portfolio thesis
This is not a business; it is **legible proof of judgment** built to travel on CT and land in the DMs of people hiring perp/trading-infra PMs. Every product decision is optimized for *credibility per unit of build effort*, not revenue, retention, or TAM. Where a "real startup" decision and a "credibility" decision diverge, credibility wins.

Concretely, success is: within ~3 editions, Minh is "the realized execution quality person" in perp DEX discourse, and that reputation generates inbound conversations with founders / Heads of Product who hire.

### 1.2 The market gap
A live orderbook depth scanner already exists ([@matrixthesun's tool](https://perps-depth-scanner.vercel.app), 8–10 venues, snapshot depth + static slippage sim + "trade advisor"). It owns the **snapshot** layer and does it well. We do not compete there.

A book snapshot is structurally blind to three things, and those three things *are* the product:

1. **Realized vs. quoted slippage.** A snapshot assumes the visible book is real and stable. It isn't — quoted depth evaporates under a live market order, MMs pull on size, and oracle-priced venues (Drift DAMM, GMX) have no book to scan at all. The valuable claim is: *"Venue X advertises $2M at 5bps but fills a $2M clip at 14bps,"* verified against actual fills.
2. **The time dimension.** A live dashboard is amnesiac. The credibility asset is the **track record** — depth persistence through volatility, fill quality during liquidation cascades, true cost over 30 days. History is the product.
3. **Funding-inclusive true cost.** Slippage is one leg. The fund's real question is total cost to enter + hold + exit. Snapshot tools answer "cheapest to enter now"; we answer "cheapest to actually run this position."

**Headline metric / the one-liner the report exists to deliver:** the gap between *advertised* depth and *realized* fills, per venue, tracked over time.

### 1.3 Positioning vs. the existing scanner
Adjacent, not on top. We **cite** the scanner as the snapshot layer and go up-stack to realized + historical + funding-inclusive. The framing in all public comms: *"Depth is what the book shows. Execution quality is what you actually get. This measures the second."*

---

## 2. Goals & non-goals

### 2.1 Goals
- **G1.** Produce a defensible, repeatable monthly measurement of realized execution quality across major perp venues, with a transparent methodology that survives a venue's pushback.
- **G2.** Make the *advertised-vs-realized gap* the memorable, quotable headline of each edition.
- **G3.** Accumulate a public historical dataset from day one (the moat that a snapshot tool can't replicate).
- **G4.** Convert each edition into a CT thread + written deep-dive that activates Minh's dormant Twitter/Substack.

### 2.2 Non-goals
- **NG1. No custody, no execution, no routing.** Read-only and research-shaped only. Anything touching user funds is a reputational liability for a job-hunting artifact. `[DECISION]`
- **NG2. Not a live dashboard race.** We do not try to out-refresh the scanner. Live readings are at most a supporting feature; the report is the product.
- **NG3. Not a venue leaderboard for retail.** Audience is funds / infra people / hiring managers, not degens chasing the "best" venue.
- **NG4. No paid tier, no gated data (for now).** Free maximizes distribution, which is the entire point in portfolio mode.

---

## 3. Audience & jobs-to-be-done

| Audience | What they want | Why they share it (= distribution) |
|---|---|---|
| Funds / desks trading size | True cost to execute & hold, per venue | It saves them real bps; they cite it in internal memos |
| Venue teams (named in report) | To defend or trumpet their numbers | They *must* respond — that response is the distribution |
| Infra founders / hiring managers | Evidence of analytical judgment | They DM the author |
| CT / researchers | A clean, quotable claim | They quote-tweet the gap stat |

The design wedge: **every named venue has an incentive to engage.** That's built-in reach with zero marketing spend.

---

## 4. Product scope

### 4.1 Three surfaces, ranked by priority

**P0 — The Report (the actual product).**
A monthly written + charted publication. Headline: advertised-vs-realized gap table. Supporting sections: true-cost ranking (slippage + funding + fees), behavior through the month's volatility events, methodology transparency. Lives on Substack/site, announced via CT thread.

**P1 — The historical dataset + a few standing charts.**
A lightweight public page showing the time series accumulating: realized cost by venue by clip size, over time. Not interactive-heavy — its value is *that it has history*, which compounds monthly.

**P2 — Open-source the data plumbing (optional, later).**
The read-only collectors / per-venue fill-attribution logic as a public repo. Signals AI-native/eng credibility and earns GitHub stars (legible to founders). Composes with a future read-only MCP server. Explicitly *after* the report has 1–2 editions of substance. `[DECISION: defer]`

### 4.2 Explicitly out of scope for v1
Auto-rebalancing, alerts, user accounts, any wallet connection, mobile app, real-time push.

---

## 5. The methodology (the moat — most important section)

This is where the product lives or dies. The difficulty here *is* the defensibility; if it were easy the scanner would already do it.

### 5.1 Core measurement
For a **ladder of clip sizes starting small** (`[DECISION]` $10k / $25k / $100k / $500k / $1M / $5M notional — small rungs let *every* venue appear on the table and expose exactly where thin venues start to break, instead of only measuring sizes that just the deepest venue can absorb) and a fixed set of **assets** (`[DECISION]` **BTC, ETH, SOL, HYPE, ZEC** — majors for cross-venue comparability, plus HYPE and ZEC as liquid-but-thinner names where venue depth genuinely diverges and the gap gets interesting), measure for each venue and present as a **league table** (venues ranked per rung):

- **Advertised cost:** what the visible book / quoted depth *implies* the fill would cost at that clip (this is the snapshot number — same thing the scanner computes).
- **Realized cost:** what a clip of that size *actually* cost, derived from onchain fills where available, or from venue fill/trade data otherwise.
- **The gap:** realized − advertised, in bps. This is the headline.

### 5.2 Per-venue fill attribution `[RESOLVED — API audit done]`
**Key unlock:** every target venue exposes a **public, market-wide trade feed with no auth and no wallet**. So we *observe the entire market's takers* and measure what they paid against the book snapshotted moments before — **no capital, no test clips**. This is what makes the observe-only decision (§5.4) viable rather than a compromise.

For v1, all four venues share one structure (orderbook), so a single league table applies cleanly:

**v1 venues — Orderbook (advertised-vs-realized gap applies directly):**

| Venue | Trade feed (realized) | Book feed (advertised) | Aggressor side | Clip reconstruction | Notes |
|---|---|---|---|---|---|
| **Hyperliquid** | `trades` ws (public) | `l2Book` ws | Inferable from print sequence | Sweep consecutive aggressor prints | **Flagship.** Deepest, most-watched → biggest claim. |
| **Lighter** | `trade/{mkt}` ws (public) | `order_book` ws + `market_stats` (funding) | **`is_maker_ask` — direct, no inference** | **`taker_position_size_before` gives the taker's full intended clip** | **Best data of the set.** That one field isolates genuinely large clips from the stream instead of guessing. |
| **Pacifica** | `trades` ws + REST `/api/v1/trades` backfill | public book channel | `d` field (e.g. `close_short`) | Sweep prints | Solana. Clean. |
| **Phoenix** | Fully onchain (Solana CLOB) | Onchain book | Onchain | Onchain | Small book → expect worse fills, as you predicted. Include precisely *because* it shows the thin-venue failure mode. |

**v1 = these four orderbook venues only.** Single-bucket product — advertised-vs-realized applies uniformly, no methodology seams to defend on debut.

**Deferred to a later edition (NOT v1):**
- **Variational** — RFQ/intent-based, no orderbook → needs the oracle/reference benchmark, different methodology. Cut from v1.
- **Ostium** — oracle-priced pool perps, price-after-impact, no book → same reason. Cut from v1.
- When added, both go in a separate "oracle/impact-priced" section benchmarked against reference price, never blended into the orderbook league table. (GMX, Drift DAMM would join here too.)

> Reviewer note: this is the section where your domain knowledge most overrides me — if any of these four venues changed their API recently, or if the Phoenix structural assumption is off, flag it.

### 5.3 Funding-inclusive true cost — anchored to *realized* holding length `[UPGRADED]`
Generic 1d/7d/30d hold assumptions are arbitrary. The sharper, more defensible move: **derive the actual average holding length of positions per venue from observed lifecycles, then price the funding drag over that realized duration.** This turns a generic "true cost" line into a venue-specific claim with teeth — e.g. *"the median HYPE position on Venue X lives ~N hours and pays Y bps of funding over its life; on Venue Z the same trade lives longer and the funding profile flips the entry-cost ranking."*

True cost = entry slippage + exit slippage + cumulative funding (over realized hold) + fees.

**Two layers, labeled by confidence:**
- **Headline (where data supports it):** funding cost over the *realized* median/distribution of hold length for that venue+asset. **Strongest on Lighter** — `taker_position_size_before` plus position state lets us track a position's size trajectory and infer open→close lifecycle. On HL/Pacifica, hold length is *approximated* from the public open/close print stream (we see opens and closes but attributing them per-account is inference), so it's best-effort and flagged.
- **Baseline fallback:** the same funding cost computed over fixed 1d / 7d / 30d horizons, shown alongside so there's always a comparable number even where realized-hold attribution is weak.

This makes "cheapest to enter" vs. "cheapest to actually hold" diverge in a *measured*, venue-specific way rather than a hypothetical one — and the divergence is itself a recurring story.

> Reviewer note: the realized-hold attribution is the part most dependent on what position state each venue exposes publicly. If HL/Pacifica expose more than I think (or Lighter less), this layering changes.

### 5.4 Methodology decisions `[RESOLVED]`
- **Clip rungs:** ladder from $10k (see §5.1). League-table presentation. ✓
- **Assets:** BTC, ETH, SOL, HYPE, ZEC. ✓
- **Venue list:** Hyperliquid (flagship), Lighter, Pacifica, Phoenix. Variational + Ostium deferred. ✓
- **Own-capital vs. observe-only:** **observe-only, no capital.** ✓
- **Hold durations:** realized average hold per venue as headline, 1d/7d/30d as fallback baseline (see §5.3). ✓
- **Time-of-day fairness:** **continuous logging, bucketed into 12 two-hour windows/day.** Because it's observe-only over public websockets, there is **no per-sample cost** — the collector runs always-on and captures every trade; the "12 windows" is how we *slice* the data for session fairness (Asia/EU/US), not a sampling budget. So we don't need to ration to 12 — we log everything and report by window. ✓

**Infra cost (the only real cost, and it's trivial):** one always-on VPS at ~$4–6/month (Vultr/DigitalOcean/Kamatera; RackNerd ~$15/yr) runs the collectors for all 4 venues × 5 assets. Compute is negligible for websocket ingestion. The only thing that *scales* is storage from logging every trade + book delta — still a few dollars of disk/month, manageable by aggregating older raw data into per-window summaries. No need to reduce sampling for cost.

**Listing rule:** an asset only appears for venues that list it. If ZEC (or any name) isn't on a given venue, that venue simply drops out of that asset's league table — we don't force coverage or penalize absence. The table notes which venues were eligible per asset so a sparse row isn't misread as poor performance.

**Still genuinely open:**
- `[OPEN]` Realized-hold attribution confidence per venue (§5.3) — depends on what position state HL/Pacifica expose publicly.

---

## 6. Edition structure (what ships monthly)

1. **The Gap** — headline table: advertised vs realized, per venue, per clip. One sentence of takeaway.
2. **True Cost to Hold** — funding-inclusive ranking; call out where it diverges from #1.
3. **Behavior Under Stress** — how depth/fills held up during the month's worst volatility window.
4. **Methodology & Confidence** — what's verified, what's best-effort, what changed since last edition.
5. **One opinionated call** — the thing Minh is willing to stake reputation on this month. This is the quotable line.

---

## 7. Build plan & sequencing

`[DECISION]` **Build quietly for one cycle before publishing.** The debut claim (likely "Venue X's advertised depth overstates realized fill quality by N bps at size") *is* the distribution, so it must be bulletproof. A weak first edition is worse than a later strong one.

- **Weeks 1–2:** lock methodology (resolve §5.4), build read-only collectors per venue, start logging. No public anything.
- **Weeks 2–3:** fill-attribution logic, confidence tagging, first internal gap table. Sanity-check against what you already know from Drift.
- **Week 3–4:** charts + write edition 1. Pressure-test the headline claim against the most likely venue rebuttal *before* publishing.
- **Launch:** thread on Minh's Twitter (primary channel) + deep-dive, citing the scanner for the snapshot layer.

---

## 8. Infrastructure & reliability requirements

These are must-haves, not nice-to-haves. This product's specific failure mode is *invisible data corruption* (a silent gap in captured fills), not visible downtime — and because the entire value proposition is credibility, a single corrupted edition costs more than a late one. The three requirements below are where an infra shortcut silently becomes a credibility problem.

**8.1 Gap detection & reconnect handling `[must-have v1]`**
The collectors are always-on stateful processes consuming public websockets. Venue feeds disconnect periodically and without warning (Hyperliquid's docs state this explicitly and instruct clients to handle reconnects and backfill missed data). Requirements:
- Every collector must detect disconnects and auto-reconnect, then backfill the missed window via REST where available (e.g. Pacifica's `/api/v1/trades`) or flag the gap where not.
- A continuous integrity check: any window with a detected gap is *marked*, and marked windows are excluded from published numbers rather than silently averaged in.
- Monitoring/alerting on collector liveness — we must *know* within minutes if a feed dropped, not discover it at publish time.

**8.2 Raw-retention-then-aggregate policy `[must-have v1]`**
Logging every trade + book delta across 4 venues × 5 assets is millions of rows/day; we can't keep raw forever, but aggregating too early forecloses future analysis. Policy:
- Keep **full-granularity raw data for a defined window** (proposed: 90 days `[OPEN]`) so a new question about a recent edition can still be answered from source.
- Roll older raw data into per-two-hour-window summaries (the same 12 daily buckets used for session fairness), then discard granular rows.
- The retention window is a *product* decision, not just cost management: it bounds what historical questions future editions can answer. Revisit if a recurring analysis needs deeper lookback.

**8.3 Cross-venue timestamp normalization `[must-have v1]`**
The core metric compares a realized fill against the book *as it was an instant before* — so trade and book-snapshot events must be orderable on a common clock. Venues stamp time differently (Lighter: microsecond `transaction_time`; Hyperliquid: millisecond block time; others vary). Requirements:
- Normalize all event timestamps to a single reference clock and resolution at ingestion, recording the original alongside.
- Define and document the matching rule (which book snapshot a given fill is measured against, and the tolerance window). This is a *methodology* commitment as much as an infra one — getting it wrong produces a number a venue can legitimately dispute.
- Be explicit in the methodology section about this rule, so the comparison is defensibly apples-to-apples.

**Deliberately *not* required:** low-latency or colocated infra. This is a measurement tool observing others' fills, not an execution system racing the market — so a ~$5/month VPS is correct and over-provisioning would be wasted spend. Knowing *why* the expensive infra isn't needed is itself the decision.

---

## 9. Success metrics

Because this is a credibility artifact, vanity metrics are downstream of the real one.
- **North star:** inbound from founders/hiring managers referencing the report. `[OPEN: target N over 3 editions?]`
- **Leading:** venue teams responding/engaging; quote-tweets of the gap stat; researchers citing the methodology.
- **Hygiene:** edition ships monthly without a methodology embarrassment (a number you have to retract = reputational cost > the edition's benefit).

---

## 10. Risks & honest caveats

| Risk | Severity | Mitigation |
|---|---|---|
| Realized-fill attribution is genuinely hard; some venues won't yield clean onchain data | High | Confidence buckets + transparent labeling; don't overclaim on lower-confidence venues |
| A venue publicly disputes a number and is *right* | High (reputational) | Build quietly first; pressure-test the headline against the obvious rebuttal pre-launch |
| Scanner author (or someone) adds a "realized" feature and overlaps | Medium | History is the moat — they start from zero on the time series; also stay "realized + funding + stress," not just realized |
| A silent collector gap corrupts an edition's numbers without anyone noticing | High (reputational) | §8.1 gap-detection + reconnect/backfill + liveness monitoring; marked windows excluded, never silently averaged |
| Cross-venue timestamp mismatch makes a comparison disputable | Medium-High | §8.3 normalization to a common clock + documented matching rule |
| Effort underestimated; stalls after edition 1 | Medium | Scope edition 1 narrow (fewer venues, verified bucket only) and expand coverage over editions |
| Looks like a tool, not a point of view | Medium | The "one opinionated call" section forces a stake-in-the-ground every edition |

---

## 11. Open decisions summary (for your walkthrough)

**Resolved this pass:**
- ✓ Clip rungs: ladder from $10k, league-table format
- ✓ Assets: BTC, ETH, SOL, HYPE, ZEC
- ✓ Observe-only, no capital (public trade feeds confirmed)
- ✓ v1 venues: Hyperliquid (flagship), Lighter, Pacifica, Phoenix — single orderbook bucket
- ✓ Variational + Ostium deferred (oracle/RFQ → different methodology, later edition)
- ✓ Funding cost anchored to *realized* hold length (headline) + 1d/7d/30d baseline
- ✓ Continuous logging, 12 two-hour session windows — no per-sample cost, infra ~$5/mo
- ✓ Build quietly before launch

**Still need your call:**
1. Confirm ZEC is listed across enough of the four venues to be comparable.
2. Realized-hold attribution confidence per venue (what position state HL/Pacifica expose).
3. ✓ Publish under Minh's Twitter account from edition 1 (named, not anonymous).
4. Name. "RealizedFill" is a placeholder.

**One attribution nuance to flag (observe-only):** because we watch the market's takers rather than placing our own clips, "realized cost at $1M" means *"when a ~$1M taker clip actually occurred, here's what it paid."* If a venue rarely sees $1M clips in an asset, that rung is sparse — we report sample counts per cell and don't manufacture a number from a quiet rung. Lighter's `taker_position_size_before` makes clip-size attribution exact; on HL/Pacifica we infer clip size by sweeping consecutive same-side prints, which is good but not perfect. This is the honest seam in the observe-only approach and we label it.
