"""OKX market-wide liquidation collector — cross-market cascade signal (§6.3).

Binance's futures websocket is geo-restricted from our VPS (it accepts the
handshake but withholds all data — verified 2026-06-14, same from a US IP), so
we use OKX's public `liquidation-orders` stream instead: free, no key,
reachable, and OKX is a top venue, so it's a solid market-wide cascade proxy.

OKX reports liquidation size in CONTRACTS, so we convert to USD via each
instrument's contract value (ctVal): linear (USDT/USDC-settled) notional =
sz * ctVal * price; inverse (USD-denominated ctVal) notional = sz * ctVal.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
import websockets

from ..base import VenueCollector
from ..models import ts_ns_to_date

log = logging.getLogger("okx_liq")

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
PING_INTERVAL_S = 20  # OKX drops idle connections after 30s


class OkxLiqCollector(VenueCollector):
    name = "okx_liq"

    def __init__(self, store):
        super().__init__(store, coins=["*"])
        self.ctval: dict[str, tuple[float, bool]] = {}  # instId -> (ctVal, is_inverse)

    async def _load_instruments(self) -> None:
        async with aiohttp.ClientSession() as s:
            async with s.get(INSTRUMENTS_URL, timeout=aiohttp.ClientTimeout(total=15)) as r:
                d = await r.json()
        for i in d.get("data", []):
            try:
                self.ctval[i["instId"]] = (float(i["ctVal"]), i.get("ctValCcy") == "USD")
            except (KeyError, ValueError, TypeError):
                pass
        log.info("okx instruments loaded: %d", len(self.ctval))

    async def _run_session(self) -> None:
        if not self.ctval:
            await self._load_instruments()
        async with websockets.connect(WS_URL, max_size=2**24) as ws:
            await ws.send(json.dumps(
                {"op": "subscribe",
                 "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}))
            ping = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    self._handle(json.loads(raw))
            finally:
                ping.cancel()

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send("ping")

    def _handle(self, msg: dict) -> None:
        for item in msg.get("data") or []:  # subscribe/error acks have no 'data'
            inst = item.get("instId", "")
            coin = inst.split("-")[0]
            cv = self.ctval.get(inst)
            for det in item.get("details") or []:
                self.mark("*", "liq")
                try:
                    sz = float(det["sz"])
                    px = float(det.get("bkPx") or 0)
                    ts_ns = int(det["ts"]) * 1_000_000
                except (KeyError, ValueError, TypeError):
                    continue
                if cv:
                    ctval, inverse = cv
                    notional = sz * ctval if inverse else sz * ctval * px
                else:
                    notional = sz * px  # unknown instrument — rough fallback
                side = "long_liq" if det.get("posSide") == "long" else "short_liq"
                self.store.put_ext_liq((
                    "okx", coin, inst, ts_ns, side, sz, px, notional, ts_ns_to_date(ts_ns)))
