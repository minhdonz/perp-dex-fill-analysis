"""One-off probe: connect to each venue WS, subscribe, dump first few raw messages.

Used during the spike to confirm subscription formats and payload shapes
before committing to collector code. Not part of the running service.
"""
import asyncio
import json
import sys

import websockets


async def probe(name, url, sub_msgs, n=6, timeout=25, reply_ping=None):
    print(f"\n=== {name} ({url}) ===", flush=True)
    try:
        async with websockets.connect(url, max_size=2**24) as ws:
            for m in sub_msgs:
                await ws.send(json.dumps(m))
            got = 0
            while got < n:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(raw)
                if reply_ping and msg.get("type") == "ping":
                    await ws.send(json.dumps(reply_ping))
                    continue
                txt = raw if isinstance(raw, str) else raw.decode()
                print(f"--- msg {got}: {txt[:900]}", flush=True)
                got += 1
    except Exception as e:
        print(f"!!! {name} failed: {type(e).__name__}: {e}", flush=True)


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "hl"):
        await probe(
            "hyperliquid", "wss://api.hyperliquid.xyz/ws",
            [
                {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}},
                {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}},
            ],
        )
    if which in ("all", "lighter"):
        await probe(
            "lighter", "wss://mainnet.zklighter.elliot.ai/stream",
            [
                {"type": "subscribe", "channel": "trade/1"},
                {"type": "subscribe", "channel": "order_book/1"},
                {"type": "subscribe", "channel": "market_stats/1"},
            ],
            reply_ping={"type": "pong"},
        )
    if which in ("all", "pacifica"):
        await probe(
            "pacifica", "wss://ws.pacifica.fi/ws",
            [
                {"method": "subscribe", "params": {"source": "trades", "symbol": "BTC"}},
                {"method": "subscribe", "params": {"source": "book", "symbol": "BTC", "agg_level": 1}},
            ],
        )


asyncio.run(main())
