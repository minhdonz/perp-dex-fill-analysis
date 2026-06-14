"""Funding model — Phase 2 of the net-cost build.

All three venues settle funding HOURLY and publish a per-hour rate, but in
DIFFERENT UNITS (verified 2026-06-14 against each venue's funding history and
the cross-exchange funding-rates feed, where Lighter BTC ≈ HL BTC once units
are aligned):

  - Hyperliquid : fraction of notional per hour   (use directly)
  - Pacifica    : fraction of notional per hour   (use directly)
  - Lighter     : PERCENT per hour                (÷100 for the fraction)

So a stored Lighter value of 0.0012 = 0.0012%/hr = 1.2e-5/hr, NOT 0.12%/hr.
Getting this wrong is a 100× error — see scripts/fetch_funding.py for where
each venue's rate is normalized to a fraction/hour before storage.

Funding cost over a held position = mean hourly rate × hours held. Positive
rate ⇒ longs pay shorts. Hold length is a fund parameter (not inferred). The
representative rate is a trailing average — funding is mean-reverting and
regime-dependent, so this is a forward estimate, not a guarantee.
"""
from __future__ import annotations

HOURS_PER_YEAR = 24 * 365

FUNDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_rates (
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    mean_hourly REAL NOT NULL,   -- fraction of notional per hour (normalized)
    apr REAL NOT NULL,           -- mean_hourly * 24 * 365
    n_samples INTEGER NOT NULL,
    window_days REAL NOT NULL,
    fetched_ns INTEGER NOT NULL,
    PRIMARY KEY (venue, coin)
);
"""


def ensure_schema(conn) -> None:
    conn.execute(FUNDING_SCHEMA)


def get_rate(conn, venue: str, coin: str):
    """(mean_hourly_fraction, apr, n_samples) or None."""
    return conn.execute(
        "SELECT mean_hourly, apr, n_samples FROM funding_rates WHERE venue=? AND coin=?",
        (venue, coin),
    ).fetchone()


def funding_cost_bps(conn, venue: str, coin: str, hold_hours: float, side: str = "long"):
    """Funding cost (bps) over the hold for the chosen side, plus a basis label.
    Positive = a cost; negative = a credit (you're on the receiving side).
    Returns (bps, basis) or (None, reason)."""
    row = get_rate(conn, venue, coin)
    if row is None:
        return None, "no funding data"
    mean_hourly, apr, n = row
    sign = 1.0 if side == "long" else -1.0
    bps = sign * mean_hourly * hold_hours * 1e4
    return bps, f"{apr*100:+.1f}%APR n={n}"
