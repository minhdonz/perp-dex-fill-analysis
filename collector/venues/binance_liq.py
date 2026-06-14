"""Binance market-wide liquidation collector — the cross-market cascade signal
for Behavior Under Stress (PRD §6.3).

Subscribes to Binance USD-M futures `!forceOrder@arr` (all-market liquidation
orders). Free, no API key. Binance throttles this to ~1 order/sec/symbol, so it
captures cascade *timing and intensity* market-wide, not the full liquidation
count — which is exactly what we need to *define* stress windows; we then
measure our own venues' execution during them.

Payload (per event):
  {"e":"forceOrder","E":<eventMs>,"o":{
     "s":"BTCUSDT","S":"SELL","q":"0.014","p":"63200","ap":"63190",
     "X":"FILLED","T":<tradeMs>, ...}}
  S=SELL => a LONG was force-sold (long_liq); S=BUY => a SHORT was force-bought.
"""
from __future__ import annotations

import json
import logging

import websockets

from ..base import VenueCollector
from ..models import ts_ns_to_date

log = logging.getLogger("binance_liq")

WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
QUOTES = ("USDT", "USDC", "BUSD", "USD")  # strip to get the coin


def _coin(symbol: str) -> str:
    for q in QUOTES:
        if symbol.endswith(q):
            base = symbol[: -len(q)]
            # strip leverage-token prefixes like 1000PEPE -> PEPE
            return base[4:] if base.startswith("1000") and len(base) > 4 else base
    return symbol


class BinanceLiqCollector(VenueCollector):
    name = "binance_liq"

    def __init__(self, store):
        super().__init__(store, coins=["*"])

    async def _run_session(self) -> None:
        async with websockets.connect(WS_URL, max_size=2**24) as ws:
            async for raw in ws:
                self._handle(json.loads(raw))

    def _handle(self, msg: dict) -> None:
        if msg.get("e") != "forceOrder":
            return
        o = msg["o"]
        self.mark("*", "liq")
        symbol = o["s"]
        price = float(o.get("ap") or o.get("p") or 0)
        size = float(o.get("q") or 0)
        ts_ns = int(o.get("T") or msg.get("E")) * 1_000_000
        side = "long_liq" if o.get("S") == "SELL" else "short_liq"
        self.store.put_ext_liq((
            "binance", _coin(symbol), symbol, ts_ns, side,
            size, price, size * price, ts_ns_to_date(ts_ns),
        ))
