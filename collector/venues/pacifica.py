"""Pacifica collector — spec §2.3.

Payload shapes verified live 2026-06-12:
  subscribe: {"method":"subscribe","params":{"source":"trades","symbol":"BTC"}}
             {"method":"subscribe","params":{"source":"book","symbol":"BTC","agg_level":1}}
  trades: {"channel":"trades","data":[{h, s, a, p, d, tc, t(ms), li, it}, ...]}
          h=trade id, s=symbol, a=size, p=price, d=taker direction
          (open_long/close_short => taker buy; open_short/close_long => sell),
          tc=cause ("normal"/"market_liquidation"/...). WS emits taker-side
          prints only.
  book:   {"channel":"book","data":{s, l:[[{p,a,n},...bids],[...asks]], t(ms), li}}
          Full snapshot per push.

No taker id / size-before on the public feed (spec §2.3) → taker_id=NULL,
clip grouping downstream falls back to the sweep heuristic.

Backfill (spec §6): REST GET /api/v1/trades?symbol=X returns recent trades as
fulfill_taker/fulfill_maker *pairs* (verified live) — we keep fulfill_taker
rows only. REST rows carry no trade id, so backfilled rows get a synthetic id
and the WS/backfill boundary is partitioned strictly by timestamp: backfill
covers (last_seen, backfill_until]; live WS trades with t <= backfill_until in
the first post-backfill messages are skipped as already covered. If REST's
window doesn't reach back to last_seen, the residual hole is logged as a
"gap" integrity event (so the window gets excluded at publish time rather
than silently averaged — PRD §8.1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
import websockets

from ..base import VenueCollector
from ..models import BookSnapshot, TradeRecord

log = logging.getLogger("pacifica")

WS_URL = "wss://ws.pacifica.fi/ws"
REST_TRADES = "https://api.pacifica.fi/api/v1/trades"
PING_INTERVAL_S = 30
# Pacifica pushes full book snapshots ~4/s; store at the methodology's 0.5s
# resolution floor (see lighter.py) instead of every push.
BOOK_EMIT_INTERVAL_MS = 500

BUY_DIRECTIONS = {"open_long", "close_short"}
SELL_DIRECTIONS = {"open_short", "close_long"}


def _side_from_direction(d: str | None) -> str | None:
    if d in BUY_DIRECTIONS:
        return "buy"
    if d in SELL_DIRECTIONS:
        return "sell"
    return None


class PacificaCollector(VenueCollector):
    name = "pacifica"

    def __init__(self, store, coins):
        super().__init__(store, coins)
        self.last_trade_ms: dict[str, int] = {}     # per coin, for backfill
        self.backfill_until_ms: dict[str, int] = {}  # WS trades <= this are skipped
        self.last_book_emit: dict[str, float] = {}   # per coin, monotonic ms

    async def _run_session(self) -> None:
        await self._backfill_all()
        async with websockets.connect(WS_URL, max_size=2**24) as ws:
            for coin in self.coins:
                await ws.send(json.dumps(
                    {"method": "subscribe", "params": {"source": "trades", "symbol": coin}}))
                await ws.send(json.dumps(
                    {"method": "subscribe", "params": {"source": "book", "symbol": coin, "agg_level": 1}}))
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
        elif channel == "book":
            self._on_book(msg["data"])
        elif channel in ("subscribe", "pong", "unsubscribe"):
            pass
        else:
            log.debug("unhandled channel: %s", channel)

    def _on_trade(self, t: dict) -> None:
        coin = t["s"]
        if coin not in self.coins:
            return
        self.mark(coin, "trades")
        ts_ms = int(t["t"])
        if ts_ms <= self.backfill_until_ms.get(coin, 0):
            return  # covered by REST backfill (see module docstring)
        self.last_trade_ms[coin] = max(self.last_trade_ms.get(coin, 0), ts_ms)
        price, size = str(t["p"]), str(t["a"])
        self.store.put_trade(TradeRecord(
            venue=self.name,
            coin=coin,
            ts_ns=ts_ms * 1_000_000,
            ts_venue_raw=str(t["t"]),
            price=price,
            size_base=size,
            notional_usd=TradeRecord.notional(price, size),
            aggressor_side=_side_from_direction(t.get("d")),
            taker_id=None,
            taker_size_before=None,
            trade_id=str(t["h"]),
            is_liquidation="liquidation" in str(t.get("tc", "")),
            raw=t,
        ))

    def _on_book(self, d: dict) -> None:
        coin = d["s"]
        if coin not in self.coins:
            return
        self.mark(coin, "book")
        now_ms = time.monotonic() * 1000
        if now_ms - self.last_book_emit.get(coin, 0.0) < BOOK_EMIT_INTERVAL_MS:
            return
        self.last_book_emit[coin] = now_ms
        bids, asks = d["l"]
        self.store.put_book(BookSnapshot(
            venue=self.name,
            coin=coin,
            ts_ns=int(d["t"]) * 1_000_000,
            ts_venue_raw=str(d["t"]),
            bids=[[lvl["p"], lvl["a"]] for lvl in bids[:50]],
            asks=[[lvl["p"], lvl["a"]] for lvl in asks[:50]],
            raw=d,
        ))

    # -- REST backfill after a disconnect ------------------------------------
    async def _backfill_all(self) -> None:
        for coin in self.coins:
            last_seen = self.last_trade_ms.get(coin)
            if not last_seen:
                continue  # first session of this process: nothing to bridge
            try:
                await self._backfill(coin, last_seen)
            except Exception as e:
                self.store.log_integrity(
                    self.name, "gap", coin=coin, feed="trades",
                    detail=f"backfill failed: {type(e).__name__}: {e}",
                )

    async def _backfill(self, coin: str, last_seen_ms: int) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                REST_TRADES, params={"symbol": coin},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                payload = await resp.json()
        rows = payload.get("data") or []
        takers = [r for r in rows if r.get("event_type") == "fulfill_taker"]
        if not takers:
            return
        oldest = min(int(r["created_at"]) for r in takers)
        newest = max(int(r["created_at"]) for r in takers)
        if oldest > last_seen_ms:
            # REST history doesn't reach back to where we stopped — honest gap.
            self.store.log_integrity(
                self.name, "gap", coin=coin, feed="trades",
                detail=f"backfill incomplete: hole {last_seen_ms}..{oldest} ms",
            )
        n = 0
        for r in takers:
            ts_ms = int(r["created_at"])
            if ts_ms <= last_seen_ms:
                continue
            price, size = str(r["price"]), str(r["amount"])
            self.store.put_trade(TradeRecord(
                venue=self.name,
                coin=coin,
                ts_ns=ts_ms * 1_000_000,
                ts_venue_raw=str(r["created_at"]),
                price=price,
                size_base=size,
                notional_usd=TradeRecord.notional(price, size),
                aggressor_side=_side_from_direction(r.get("side")),
                taker_id=None,
                taker_size_before=None,
                # REST rows expose no id — synthetic, stable across re-fetches
                trade_id=f"bf-{ts_ms}-{price}-{size}-{r.get('side')}",
                is_liquidation="liquidation" in str(r.get("cause", "")),
                raw={"source": "rest_backfill", **r},
            ))
            n += 1
        self.backfill_until_ms[coin] = newest
        self.last_trade_ms[coin] = max(last_seen_ms, newest)
        self.store.log_integrity(
            self.name, "backfill", coin=coin, feed="trades",
            detail=f"{n} trades, window {last_seen_ms}..{newest} ms",
        )
