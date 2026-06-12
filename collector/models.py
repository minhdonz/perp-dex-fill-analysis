"""Common data model — spec §1. Every venue normalizes into these two records.

Prices/sizes are kept as strings exactly as the venue sent them (no float
round-trip); `notional_usd` is computed with Decimal when the venue doesn't
provide it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


def ts_ns_to_date(ts_ns: int) -> str:
    """UTC date partition key (venue/coin/date pruning, spec §5)."""
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(slots=True)
class TradeRecord:
    venue: str
    coin: str
    ts_ns: int
    ts_venue_raw: str
    price: str
    size_base: str
    notional_usd: str
    aggressor_side: str | None  # "buy" | "sell" | None
    taker_id: str | None
    taker_size_before: str | None
    trade_id: str
    is_liquidation: bool
    raw: dict

    @staticmethod
    def notional(price: str, size_base: str) -> str:
        return str(Decimal(price) * Decimal(size_base))

    def to_row(self) -> tuple:
        return (
            self.venue, self.coin, self.ts_ns, self.ts_venue_raw,
            self.price, self.size_base, self.notional_usd,
            self.aggressor_side, self.taker_id, self.taker_size_before,
            self.trade_id, int(self.is_liquidation),
            json.dumps(self.raw, separators=(",", ":")),
            ts_ns_to_date(self.ts_ns),
        )


@dataclass(slots=True)
class BookSnapshot:
    venue: str
    coin: str
    ts_ns: int
    ts_venue_raw: str
    bids: list[list[str]]  # [[price, size], ...] best-first
    asks: list[list[str]]
    raw: dict

    def to_row(self) -> tuple:
        return (
            self.venue, self.coin, self.ts_ns, self.ts_venue_raw,
            json.dumps(self.bids, separators=(",", ":")),
            json.dumps(self.asks, separators=(",", ":")),
            json.dumps(self.raw, separators=(",", ":")),
            ts_ns_to_date(self.ts_ns),
        )


@dataclass(slots=True)
class MarketStats:
    """Funding/stats stream (Lighter `market_stats`) — needed downstream for
    funding-inclusive true cost (PRD §5.3). Light extension to the spec §1 model."""
    venue: str
    coin: str
    ts_ns: int
    funding_rate: str | None
    mark_price: str | None
    index_price: str | None
    raw: dict

    def to_row(self) -> tuple:
        return (
            self.venue, self.coin, self.ts_ns,
            self.funding_rate, self.mark_price, self.index_price,
            json.dumps(self.raw, separators=(",", ":")),
            ts_ns_to_date(self.ts_ns),
        )
