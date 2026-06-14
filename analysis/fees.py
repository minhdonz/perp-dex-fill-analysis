"""Fee model — Phase 1 of the net-cost build.

Hyperliquid is REAL: its public `userFees` endpoint returns each account's
actual taker rate and the full tier ladder, so we never assume a tier — we
look up the account, or read the ladder for a fund's stated volume.

Lighter and Pacifica expose no public fee API (verified 2026-06-14: account
endpoints carry no fee field; no fee/feeTiers endpoint), so they use published
schedules encoded below and flagged `confirmed=False` until checked against
docs. The cost view shows that flag rather than passing an unconfirmed number
off as real.

All rates are fractions of notional; bps = rate * 1e4.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- Hyperliquid: from public userFees.feeSchedule (fetched 2026-06-14) ----
# (14-day rolling volume cutoff in $, taker fraction, maker fraction)
HL_TIERS = [
    (0,             0.00045, 0.00015),   # base        4.5 / 1.5 bps
    (5_000_000,     0.00040, 0.00012),   # >$5M        4.0 / 1.2
    (25_000_000,    0.00035, 0.00008),   # >$25M       3.5 / 0.8
    (100_000_000,   0.00030, 0.00004),   # >$100M      3.0 / 0.4
    (500_000_000,   0.00028, 0.0),       # >$500M      2.8 / 0.0
    (2_000_000_000, 0.00026, 0.0),       # >$2B        2.6 / 0.0
    (7_000_000_000, 0.00024, 0.0),       # >$7B        2.4 / 0.0
]


def hl_tier_for_volume(vol_14d_usd: float):
    chosen = HL_TIERS[0]
    for t in HL_TIERS:
        if vol_14d_usd >= t[0]:
            chosen = t
    return chosen


def hl_taker_bps_for_volume(vol_14d_usd: float) -> float:
    """Ladder taker rate (bps) for a stated 14d volume — the 'what tier would
    my fund land in' lookup. No assumption: the fund supplies its own volume."""
    return hl_tier_for_volume(vol_14d_usd)[1] * 1e4


def hl_tier_label(vol_14d_usd: float) -> str:
    cutoff = hl_tier_for_volume(vol_14d_usd)[0]
    return "base" if cutoff == 0 else f">${cutoff/1e6:.0f}M" if cutoff < 1e9 else f">${cutoff/1e9:.0f}B"


# ---- Lighter / Pacifica: published schedule (no public fee API) ----
@dataclass(frozen=True)
class Schedule:
    taker_bps: float | None
    maker_bps: float | None
    confirmed: bool
    note: str


SCHEDULES = {
    # Lighter has run a zero/near-zero taker model; CONFIRM current docs before publishing.
    "lighter": Schedule(taker_bps=0.0, maker_bps=0.0, confirmed=False,
                        note="Lighter zero-fee model — confirm current docs"),
    # No basis to assume Pacifica's rate; left None so the view shows 'TBD'.
    "pacifica": Schedule(taker_bps=None, maker_bps=None, confirmed=False,
                         note="add Pacifica published taker rate from docs"),
}


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
    """Return (bps, basis). basis ∈ {real, tier, base, schedule, schedule?, unknown}.

    HL: real cached per-account rate if we have it; else the ladder rate for a
    stated volume; else base. Lighter/Pacifica: published schedule (None if
    unconfirmed/unknown)."""
    if venue == "hyperliquid":
        if account:
            b = real_hl_taker_bps(conn, account)
            if b is not None:
                return b, "real"
        if assumed_vol_14d is not None:
            return hl_taker_bps_for_volume(assumed_vol_14d), "tier"
        return HL_TIERS[0][1] * 1e4, "base"
    s = SCHEDULES.get(venue)
    if s is None or s.taker_bps is None:
        return None, "unknown"
    return s.taker_bps, ("schedule" if s.confirmed else "schedule?")
