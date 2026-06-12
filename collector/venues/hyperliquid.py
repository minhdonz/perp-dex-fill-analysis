"""Hyperliquid collector — spec §2.1 [FLAGSHIP].

Payload shapes verified live 2026-06-12:
  trades: {"channel":"trades","data":[{coin, side("B"|"A"), px, sz, time(ms),
           hash, tid, users:[buyer, seller]}, ...]}
  book:   {"channel":"l2Book","data":{coin, time(ms),
           levels:[[{px,sz,n},...bids], [{px,sz,n},...asks]]}}

Notes:
- `side` is the aggressor side: "B" taker bought, "A" taker sold.
- Taker address = users[0] (buyer) when side=="B", users[1] (seller) when "A".
- Book pushes at most ~every 0.5s (block-gated), top 20 levels per side on
  this feed (spec's N=50 target isn't available over WS; we store what the
  venue exposes).
- is_liquidation: the public trades feed does not flag liquidations (the
  zero-hash marker also covers TWAP/internal fills, verified live), so this
  is always False for HL; raw is retained if a marker becomes available.
- HL closes connections that send nothing for ~60s → app-level ping every 45s.
- Trades dedupe on tid via the (venue, trade_id) primary key, which makes
  post-reconnect overlap/snapshot replays safe.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from ..base import VenueCollector
from ..models import BookSnapshot, TradeRecord

log = logging.getLogger("hyperliquid")

WS_URL = "wss://api.hyperliquid.xyz/ws"
PING_INTERVAL_S = 45


class HyperliquidCollector(VenueCollector):
    name = "hyperliquid"

    async def _run_session(self) -> None:
        async with websockets.connect(WS_URL, max_size=2**24) as ws:
            for coin in self.coins:
                for typ in ("trades", "l2Book"):
                    await ws.send(json.dumps(
                        {"method": "subscribe", "subscription": {"type": typ, "coin": coin}}
                    ))
            ping = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    self._handle(json.loads(raw))
            finally:
                ping.cancel()

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send('{"method":"ping"}')

    def _handle(self, msg: dict) -> None:
        channel = msg.get("channel")
        if channel == "trades":
            for t in msg["data"]:
                self._on_trade(t)
        elif channel == "l2Book":
            self._on_book(msg["data"])
        elif channel in ("subscriptionResponse", "pong"):
            pass
        else:
            log.debug("unhandled channel: %s", channel)

    def _on_trade(self, t: dict) -> None:
        coin = t["coin"]
        self.mark(coin, "trades")
        side = "buy" if t["side"] == "B" else "sell"
        users = t.get("users") or [None, None]
        taker = users[0] if side == "buy" else users[1]
        self.store.put_trade(TradeRecord(
            venue=self.name,
            coin=coin,
            ts_ns=int(t["time"]) * 1_000_000,
            ts_venue_raw=str(t["time"]),
            price=str(t["px"]),
            size_base=str(t["sz"]),
            notional_usd=TradeRecord.notional(str(t["px"]), str(t["sz"])),
            aggressor_side=side,
            taker_id=taker,
            taker_size_before=None,  # not on public feed (spec §2.1)
            trade_id=str(t["tid"]),
            is_liquidation=False,
            raw=t,
        ))

    def _on_book(self, d: dict) -> None:
        coin = d["coin"]
        self.mark(coin, "book")
        bids, asks = d["levels"]
        self.store.put_book(BookSnapshot(
            venue=self.name,
            coin=coin,
            ts_ns=int(d["time"]) * 1_000_000,
            ts_venue_raw=str(d["time"]),
            bids=[[lvl["px"], lvl["sz"]] for lvl in bids[:50]],
            asks=[[lvl["px"], lvl["sz"]] for lvl in asks[:50]],
            raw=d,
        ))
