"""Base venue collector: reconnect loop with backoff, per-feed liveness
tracking, integrity logging — spec §6 ("build these alongside, not after").
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

from .storage import Store

log = logging.getLogger("collector")

# A feed silent longer than this during a live connection is flagged stale.
STALE_AFTER_S = {"trades": 600.0, "book": 120.0, "stats": 300.0, "liq": 1800.0}
LIVENESS_CHECK_INTERVAL_S = 30.0


class VenueCollector(ABC):
    name: str = "?"

    def __init__(self, store: Store, coins: list[str]):
        self.store = store
        self.coins = coins
        # (coin, feed) -> monotonic time of last message
        self.last_msg: dict[tuple[str, str], float] = {}
        self._stale: set[tuple[str, str]] = set()
        self._connected = False

    def mark(self, coin: str, feed: str) -> None:
        key = (coin, feed)
        self.last_msg[key] = time.monotonic()
        if key in self._stale:
            self._stale.discard(key)
            self.store.log_integrity(self.name, "recovered", coin=coin, feed=feed)

    async def run(self) -> None:
        """Reconnect-forever loop. A clean long session resets the backoff."""
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                self.store.log_integrity(self.name, "connect")
                self._connected = True
                await self._run_session()
            except asyncio.CancelledError:
                self.store.log_integrity(self.name, "disconnect", detail="shutdown")
                raise
            except Exception as e:
                self._connected = False
                self.store.log_integrity(
                    self.name, "disconnect", detail=f"{type(e).__name__}: {e}"
                )
            uptime = time.monotonic() - started
            backoff = 1.0 if uptime > 120 else min(backoff * 2, 60.0)
            log.warning("%s: reconnecting in %.0fs (uptime %.0fs)", self.name, backoff, uptime)
            await asyncio.sleep(backoff)

    @abstractmethod
    async def _run_session(self) -> None:
        """One websocket session: connect, subscribe, consume until error/close."""

    async def liveness_monitor(self) -> None:
        """Flag feeds that go silent while the connection is up (spec §6)."""
        while True:
            await asyncio.sleep(LIVENESS_CHECK_INTERVAL_S)
            if not self._connected:
                continue
            now = time.monotonic()
            for (coin, feed), last in self.last_msg.items():
                threshold = STALE_AFTER_S.get(feed, 600.0)
                key = (coin, feed)
                if now - last > threshold and key not in self._stale:
                    self._stale.add(key)
                    self.store.log_integrity(
                        self.name, "stale", coin=coin, feed=feed,
                        detail=f"silent {now - last:.0f}s",
                    )
