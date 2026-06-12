"""Lighter collector — spec §2.2 [BEST DATA].

Payload shapes verified live 2026-06-12:
  connect ack: {"type":"connected", session_id}
  trades:  channel "trade:{mkt}", type "subscribed/trade" (recent history) or
           "update/trade"; trades under "trades" and/or "liquidation_trades".
           Trade fields: trade_id, market_id, size, price, usd_amount,
           ask_account_id, bid_account_id, is_maker_ask, timestamp(ms),
           transaction_time(us), taker_position_size_before, type, ...
  book:    channel "order_book:{mkt}", type "subscribed/order_book" (full
           snapshot) then "update/order_book" deltas; size "0.00000" removes a
           level; continuity via begin_nonce/nonce.
  stats:   channel "market_stats:{mkt}" with funding/mark/index.
  Server sends {"type":"ping"} → reply {"type":"pong"}.

Book handling: we maintain the full book locally from snapshot+deltas and emit
a top-50 BookSnapshot row at most every BOOK_EMIT_INTERVAL_MS per market
(storing every delta would be ~10-100x the rows for no extra matching
precision — emission cadence is still finer than HL's 0.5s floor). The
snapshot row's bids/asks are the reconstructed state; `raw` carries the
triggering delta plus offset/nonce for audit. On a nonce gap we log integrity
"gap" and resubscribe for a fresh snapshot.

Market index → symbol map fetched once at startup from REST /api/v1/orderBooks.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

import aiohttp
import websockets

from ..base import VenueCollector
from ..models import BookSnapshot, MarketStats, TradeRecord

log = logging.getLogger("lighter")

WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
REST_ORDERBOOKS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
BOOK_EMIT_INTERVAL_MS = 250
BOOK_DEPTH = 50


class _BookState:
    __slots__ = ("bids", "asks", "nonce", "last_emit_ms", "synced")

    def __init__(self):
        self.bids: dict[str, str] = {}
        self.asks: dict[str, str] = {}
        self.nonce: int | None = None
        self.last_emit_ms = 0.0
        self.synced = False


class LighterCollector(VenueCollector):
    name = "lighter"

    def __init__(self, store, coins):
        super().__init__(store, coins)
        self.market_to_coin: dict[int, str] = {}
        self.books: dict[int, _BookState] = {}
        self._ws = None

    async def _resolve_markets(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(REST_ORDERBOOKS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
        wanted = set(self.coins)
        self.market_to_coin = {
            ob["market_id"]: ob["symbol"]
            for ob in data.get("order_books", [])
            if ob.get("symbol") in wanted and ob.get("status") == "active"
        }
        missing = wanted - set(self.market_to_coin.values())
        if missing:
            log.warning("not listed on lighter, skipping: %s", sorted(missing))
        log.info("lighter markets: %s", self.market_to_coin)

    async def _run_session(self) -> None:
        if not self.market_to_coin:
            await self._resolve_markets()
        self.books = {mkt: _BookState() for mkt in self.market_to_coin}
        async with websockets.connect(WS_URL, max_size=2**26) as ws:
            self._ws = ws
            for mkt in self.market_to_coin:
                for chan in (f"trade/{mkt}", f"order_book/{mkt}", f"market_stats/{mkt}"):
                    await ws.send(json.dumps({"type": "subscribe", "channel": chan}))
            async for raw in ws:
                await self._handle(json.loads(raw))

    async def _handle(self, msg: dict) -> None:
        typ = msg.get("type", "")
        if typ == "ping":
            await self._ws.send('{"type":"pong"}')
            return
        if typ in ("connected", "subscribed", "unsubscribed"):
            return
        channel = msg.get("channel", "")
        if channel.startswith("trade:"):
            self._on_trades(int(channel.split(":")[1]), msg)
        elif channel.startswith("order_book:"):
            await self._on_book(int(channel.split(":")[1]), msg)
        elif channel.startswith("market_stats:"):
            self._on_stats(int(channel.split(":")[1]), msg)

    # -- trades ------------------------------------------------------------
    def _on_trades(self, mkt: int, msg: dict) -> None:
        coin = self.market_to_coin[mkt]
        self.mark(coin, "trades")
        for t in msg.get("trades") or []:
            self._ingest_trade(coin, t, liq=t.get("type") == "liquidation")
        for t in msg.get("liquidation_trades") or []:
            self._ingest_trade(coin, t, liq=True)

    def _ingest_trade(self, coin: str, t: dict, liq: bool) -> None:
        # taker is the non-maker side: maker ask => taker bought
        is_maker_ask = t.get("is_maker_ask")
        side = None if is_maker_ask is None else ("buy" if is_maker_ask else "sell")
        taker_acct = t.get("bid_account_id") if side == "buy" else t.get("ask_account_id")
        tx_us = t.get("transaction_time")
        if tx_us:
            ts_ns, ts_raw = int(tx_us) * 1_000, str(tx_us)
        else:
            ts_ns, ts_raw = int(t["timestamp"]) * 1_000_000, str(t["timestamp"])
        price, size = str(t["price"]), str(t["size"])
        self.store.put_trade(TradeRecord(
            venue=self.name,
            coin=coin,
            ts_ns=ts_ns,
            ts_venue_raw=ts_raw,
            price=price,
            size_base=size,
            notional_usd=str(t.get("usd_amount") or Decimal(price) * Decimal(size)),
            aggressor_side=side,
            taker_id=str(taker_acct) if taker_acct is not None else None,
            taker_size_before=(
                str(t["taker_position_size_before"])
                if t.get("taker_position_size_before") is not None else None
            ),
            trade_id=str(t.get("trade_id_str") or t["trade_id"]),
            is_liquidation=liq,
            raw=t,
        ))

    # -- order book ---------------------------------------------------------
    async def _on_book(self, mkt: int, msg: dict) -> None:
        coin = self.market_to_coin[mkt]
        self.mark(coin, "book")
        state = self.books[mkt]
        body = msg.get("order_book") or {}
        typ = msg.get("type", "")

        if typ == "subscribed/order_book":
            state.bids = {lvl["price"]: lvl["size"] for lvl in body.get("bids") or []}
            state.asks = {lvl["price"]: lvl["size"] for lvl in body.get("asks") or []}
            state.nonce = body.get("nonce")
            state.synced = True
        elif typ == "update/order_book":
            if not state.synced:
                return
            begin = body.get("begin_nonce")
            if state.nonce is not None and begin is not None and begin > state.nonce:
                self.store.log_integrity(
                    self.name, "gap", coin=coin, feed="book",
                    detail=f"nonce gap: have {state.nonce}, update begins {begin}",
                )
                state.synced = False
                await self._resubscribe_book(mkt)
                return
            for lvl in body.get("bids") or []:
                self._apply(state.bids, lvl)
            for lvl in body.get("asks") or []:
                self._apply(state.asks, lvl)
            if body.get("nonce") is not None:
                state.nonce = body["nonce"]
        else:
            return

        now_ms = time.monotonic() * 1000
        if now_ms - state.last_emit_ms >= BOOK_EMIT_INTERVAL_MS:
            state.last_emit_ms = now_ms
            self._emit_book(coin, msg, state)

    @staticmethod
    def _apply(side: dict[str, str], lvl: dict) -> None:
        if Decimal(lvl["size"]) == 0:
            side.pop(lvl["price"], None)
        else:
            side[lvl["price"]] = lvl["size"]

    def _emit_book(self, coin: str, msg: dict, state: _BookState) -> None:
        ts_us = msg.get("last_updated_at")  # microseconds
        if ts_us:
            ts_ns, ts_raw = int(ts_us) * 1_000, str(ts_us)
        else:
            ts_ns, ts_raw = int(msg["timestamp"]) * 1_000_000, str(msg["timestamp"])
        bids = sorted(state.bids.items(), key=lambda kv: Decimal(kv[0]), reverse=True)[:BOOK_DEPTH]
        asks = sorted(state.asks.items(), key=lambda kv: Decimal(kv[0]))[:BOOK_DEPTH]
        self.store.put_book(BookSnapshot(
            venue=self.name,
            coin=coin,
            ts_ns=ts_ns,
            ts_venue_raw=ts_raw,
            bids=[[p, s] for p, s in bids],
            asks=[[p, s] for p, s in asks],
            # state is reconstructed from deltas; keep the trigger + cursor for audit
            raw={"trigger": msg.get("type"), "offset": msg.get("offset"),
                 "nonce": state.nonce, "last_updated_at": ts_us},
        ))

    async def _resubscribe_book(self, mkt: int) -> None:
        await self._ws.send(json.dumps({"type": "unsubscribe", "channel": f"order_book/{mkt}"}))
        await self._ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{mkt}"}))

    # -- stats ---------------------------------------------------------------
    def _on_stats(self, mkt: int, msg: dict) -> None:
        coin = self.market_to_coin[mkt]
        self.mark(coin, "stats")
        s = msg.get("market_stats") or {}
        ts_ms = msg.get("timestamp")
        if not ts_ms:
            return
        self.store.put_stats(MarketStats(
            venue=self.name,
            coin=coin,
            ts_ns=int(ts_ms) * 1_000_000,
            funding_rate=s.get("current_funding_rate"),
            mark_price=s.get("mark_price"),
            index_price=s.get("index_price"),
            raw=s,
        ))
