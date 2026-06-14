"""Funding model — Phase 2 of the net-cost build.

All three venues settle funding HOURLY and publish a per-hour rate, but in
DIFFERENT UNITS (verified 2026-06-14 against each venue's funding history and
the cross-exchange funding-rates feed):

  - Hyperliquid : fraction of notional per hour   (use directly)
  - Pacifica    : fraction of notional per hour   (use directly)
  - Lighter     : PERCENT per hour                (÷100 for the fraction)

Funding cost over a held position = mean hourly rate × hours held. Positive
rate ⇒ longs pay shorts. Hold length is a fund parameter (not inferred).

Beyond the mean we store STABILITY metrics, because a low average funding is
only useful if it's dependable — a venue whose funding constantly flips sign
is an unpredictable carry environment even if its mean is near zero:
  - pct_positive : share of hours funding was > 0 (directional persistence)
  - flip_rate    : share of consecutive hours where the sign flipped
                   (high = unstable/unpredictable to carry)
  - std_hourly   : stdev of the hourly rate (magnitude volatility)
The mean is a trailing average — funding is mean-reverting and regime
dependent, so it's a forward estimate, not a guarantee; flip_rate/std say how
much to trust it.
"""
from __future__ import annotations

HOURS_PER_YEAR = 24 * 365

FUNDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_rates (
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    mean_hourly REAL NOT NULL,   -- fraction of notional per hour (normalized)
    apr REAL NOT NULL,           -- mean_hourly * 24 * 365
    pct_positive REAL,           -- share of hours funding > 0 (persistence)
    flip_rate REAL,              -- share of consecutive hours sign flipped (instability)
    std_hourly REAL,             -- stdev of hourly rate (volatility)
    n_samples INTEGER NOT NULL,
    window_days REAL NOT NULL,
    fetched_ns INTEGER NOT NULL,
    PRIMARY KEY (venue, coin)
);
"""


def ensure_schema(conn) -> None:
    conn.execute(FUNDING_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(funding_rates)")}
    for col in ("pct_positive", "flip_rate", "std_hourly"):
        if col not in cols:  # migrate older tables in place
            conn.execute(f"ALTER TABLE funding_rates ADD COLUMN {col} REAL")


def stability(rates: list[float]):
    """(mean_hourly, pct_positive, flip_rate, std_hourly) from a TIME-ORDERED
    list of hourly rates. flip_rate counts sign changes between consecutive
    non-zero hours."""
    n = len(rates)
    mean = sum(rates) / n
    pct_pos = sum(1 for r in rates if r > 0) / n
    flips = pairs = 0
    for a, b in zip(rates, rates[1:]):
        sa, sb = (a > 0) - (a < 0), (b > 0) - (b < 0)
        if sa and sb:
            pairs += 1
            flips += sa != sb
    flip_rate = flips / pairs if pairs else 0.0
    std = (sum((r - mean) ** 2 for r in rates) / n) ** 0.5
    return mean, pct_pos, flip_rate, std


def get_rate(conn, venue: str, coin: str):
    """(mean_hourly, apr, n_samples) or None."""
    return conn.execute(
        "SELECT mean_hourly, apr, n_samples FROM funding_rates WHERE venue=? AND coin=?",
        (venue, coin),
    ).fetchone()


def get_stability(conn, venue: str, coin: str):
    """(pct_positive, flip_rate, std_hourly, window_days) or None."""
    return conn.execute(
        "SELECT pct_positive, flip_rate, std_hourly, window_days "
        "FROM funding_rates WHERE venue=? AND coin=?",
        (venue, coin),
    ).fetchone()


def funding_cost_bps(conn, venue: str, coin: str, hold_hours: float, side: str = "long"):
    """Funding cost (bps) over the hold for the chosen side, plus a basis label.
    Positive = a cost; negative = a credit (you're on the receiving side)."""
    row = get_rate(conn, venue, coin)
    if row is None:
        return None, "no funding data"
    mean_hourly, apr, n = row
    sign = 1.0 if side == "long" else -1.0
    bps = sign * mean_hourly * hold_hours * 1e4
    return bps, f"{apr*100:+.1f}%APR n={n}"
