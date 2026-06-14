"""Fee model — Phase 1 of the net-cost build. All schedules below are from
official docs (Hyperliquid public userFees + docs; Lighter docs; Pacifica
docs), confirmed 2026-06-14. Rates are fractions of notional; bps = rate*1e4.

Three different fee structures:
  - Hyperliquid : 14d-volume tier ladder, AND real per-account rate via the
                  public `userFees` endpoint (cached) — the only venue we can
                  read an actual account's effective rate from.
  - Pacifica    : 14d-volume tier ladder (same shape as HL) but NO public
                  per-account read (no taker id on the feed), so we can price a
                  fund's stated volume but not observe real accounts.
  - Lighter     : NOT volume-based. Standard accounts pay 0; opt-in Premium
                  accounts pay a flat fee reduced by LIT staking. Account type
                  isn't visible on the feed, so we show standard (0) with the
                  premium schedule noted. (Standard trades at higher latency —
                  the real cost of its zero fee.)
"""
from __future__ import annotations

# ---- Volume-tier ladders: (14d rolling volume cutoff $, taker frac, maker frac) ----
HL_TIERS = [
    (0,             0.00045, 0.00015),
    (5_000_000,     0.00040, 0.00012),
    (25_000_000,    0.00035, 0.00008),
    (100_000_000,   0.00030, 0.00004),
    (500_000_000,   0.00028, 0.0),
    (2_000_000_000, 0.00026, 0.0),
    (7_000_000_000, 0.00024, 0.0),
]
PACIFICA_TIERS = [
    (0,            0.00040, 0.00015),
    (5_000_000,    0.00038, 0.00012),
    (10_000_000,   0.00036, 0.00009),
    (25_000_000,   0.00034, 0.00006),
    (50_000_000,   0.00032, 0.00003),
    (100_000_000,  0.00030, 0.0),
    (250_000_000,  0.00029, 0.0),
    (500_000_000,  0.00028, 0.0),
]
VOLUME_TIERS = {"hyperliquid": HL_TIERS, "pacifica": PACIFICA_TIERS}

# ---- Lighter: account-type based (not volume). Premium base taker, reduced by LIT staking. ----
LIGHTER_STANDARD_TAKER_BPS = 0.0
LIGHTER_PREMIUM_BASE_TAKER_BPS = 2.80   # 0.0280%; -> 1.96 bps at 500k LIT staked
LIGHTER_PREMIUM_MIN_TAKER_BPS = 1.96


def tier_for_volume(tiers, vol_14d_usd: float):
    chosen = tiers[0]
    for t in tiers:
        if vol_14d_usd >= t[0]:
            chosen = t
    return chosen


def taker_bps_for_volume(venue: str, vol_14d_usd: float) -> float:
    """Ladder taker rate (bps) for a stated 14d volume — the fund's own number,
    no assumption. HL and Pacifica only."""
    return tier_for_volume(VOLUME_TIERS[venue], vol_14d_usd)[1] * 1e4


def tier_label(venue: str, vol_14d_usd: float) -> str:
    cutoff = tier_for_volume(VOLUME_TIERS[venue], vol_14d_usd)[0]
    if cutoff == 0:
        return "base"
    return f">${cutoff/1e6:.0f}M" if cutoff < 1e9 else f">${cutoff/1e9:.0f}B"


ACCOUNT_FEES_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_fees (
    venue TEXT NOT NULL,
    account TEXT NOT NULL,
    taker_bps REAL,
    maker_bps REAL,
    vol_14d_usd REAL,
    fetched_ns INTEGER NOT NULL,
    PRIMARY KEY (venue, account)
);
"""


def ensure_schema(conn) -> None:
    conn.execute(ACCOUNT_FEES_SCHEMA)


def real_hl_taker_bps(conn, account: str):
    row = conn.execute(
        "SELECT taker_bps FROM account_fees WHERE venue='hyperliquid' AND account=?",
        (account,),
    ).fetchone()
    return row[0] if row else None


def taker_bps(conn, venue: str, account: str | None = None,
              assumed_vol_14d: float | None = None):
    """Return (bps, basis).

    HL: real per-account if cached; else ladder at --volume; else base.
    Pacifica: ladder at --volume; else base tier (no per-account visibility).
    Lighter: standard 0 (premium = flat 2.8bps, account-type not observable).
    """
    if venue == "hyperliquid":
        if account:
            b = real_hl_taker_bps(conn, account)
            if b is not None:
                return b, "real"
        if assumed_vol_14d is not None:
            return taker_bps_for_volume(venue, assumed_vol_14d), f"tier {tier_label(venue, assumed_vol_14d)}"
        return HL_TIERS[0][1] * 1e4, "base"
    if venue == "pacifica":
        if assumed_vol_14d is not None:
            return taker_bps_for_volume(venue, assumed_vol_14d), f"tier {tier_label(venue, assumed_vol_14d)}"
        return PACIFICA_TIERS[0][1] * 1e4, "base (no acct visibility)"
    if venue == "lighter":
        return LIGHTER_STANDARD_TAKER_BPS, "standard (premium 2.8bps)"
    return None, "unknown"
