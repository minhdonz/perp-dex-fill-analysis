"""Run the collectors: python -m collector [--venues ...] [--coins ...] [--db ...]

Always-on ingestion service (spec §0): subscribes to trades + book per venue,
normalizes, writes to SQLite. Ctrl-C to stop (flushes cleanly).
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .storage import Store
from .venues.hyperliquid import HyperliquidCollector
from .venues.okx_liq import OkxLiqCollector
from .venues.lighter import LighterCollector
from .venues.pacifica import PacificaCollector

VENUES = {
    "hyperliquid": HyperliquidCollector,
    "lighter": LighterCollector,
    "pacifica": PacificaCollector,
}
DEFAULT_COINS = ["BTC", "ETH", "SOL", "HYPE", "ZEC"]
# Per-venue extra markets not listed on every venue. SpaceX (SPCX) is a HIP-3
# builder perp on HL (namespaced "xyz:SPCX", normalized to "SPCX" by the HL
# collector) and a native market on Lighter; Pacifica doesn't list it.
EXTRA_COINS = {"hyperliquid": ["xyz:SPCX"], "lighter": ["SPCX"], "pacifica": ["SPCX"]}
STATUS_INTERVAL_S = 60


async def status_loop(store: Store) -> None:
    while True:
        await asyncio.sleep(STATUS_INTERVAL_S)
        logging.info(
            "written: %(trade)d trades, %(book)d books, %(stats)d stats, "
            "%(ext_liq)d liqs, %(integrity)d integrity", store.counts,
        )


async def amain(args: argparse.Namespace) -> None:
    store = Store(args.db)
    collectors = [VENUES[v](store, list(args.coins) + EXTRA_COINS.get(v, []))
                  for v in args.venues]
    if not args.no_liq_feed:
        collectors.append(OkxLiqCollector(store))
    tasks = [asyncio.create_task(store.run()), asyncio.create_task(status_loop(store))]
    for c in collectors:
        tasks.append(asyncio.create_task(c.run(), name=f"{c.name}-run"))
        tasks.append(asyncio.create_task(c.liveness_monitor(), name=f"{c.name}-liveness"))
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Perp DEX fill collector")
    p.add_argument("--db", default="data/fills.db")
    p.add_argument("--venues", nargs="+", choices=sorted(VENUES), default=sorted(VENUES))
    p.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    p.add_argument("--no-liq-feed", action="store_true",
                   help="disable the OKX market-wide liquidation feed (cascade signal)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        logging.info("stopped")


if __name__ == "__main__":
    main()
