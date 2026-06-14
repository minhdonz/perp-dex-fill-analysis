"""Shared net-cost assembly so the live view (cost.py) and the time-series view
(history.py) compute the same number, the same way.

entry      = slippage/leg + taker fee/leg          (cost to get in)
round-trip = 2*(slip + fee) + funding              (enter + hold + exit)
Funding is a single hold cost (not per leg); fees are taker on both legs.
"""
from __future__ import annotations


def net_cost(slip_bps: float, fee_bps: float, funding_bps: float) -> tuple[float, float]:
    entry = slip_bps + fee_bps
    return entry, 2 * entry + funding_bps
