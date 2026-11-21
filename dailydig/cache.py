"""TTL cache in SQLite with stale-while-revalidate and in-flight coalescing."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from .db import DB, now

log = logging.getLogger("dailydig.cache")
_MISS = {"__miss__": True}


class Cache:
    def __init__(self, db: DB):
        self.db = db
        self._inflight: dict[str, asyncio.Task] = {}

    def _row(self, key: str):
        return self.db.one("SELECT value, fresh_until, stale_until FROM kv_cache WHERE key=?", (key,))

    def put(self, key: str, value: Any, ttl: float, stale: float | None = None) -> None:
        t = now()
        stale = ttl * 4 if stale is None else stale
        self.db.execute(
            "INSERT INTO kv_cache(key,value,fresh_until,stale_until) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, fresh_until=excluded.fresh_until, stale_until=excluded.stale_until",
            (key, json.dumps(_MISS if value is None else value), t + ttl, t + ttl + stale))

    @staticmethod
    def _decode(raw: str):
        v = json.loads(raw)
        return None if v == _MISS else v

    async def get_or_fetch(self, key: str, fetch: Callable[[], Awaitable[Any]], ttl: float,
                           *, miss_ttl: float | None = None, stale: float | None = None) -> Any:
        r, t = self._row(key), now()
        if r and r["fresh_until"] > t:
            return self._decode(r["value"])
        if r and r["stale_until"] > t:  # serve stale, refresh behind the scenes
            self._spawn(key, fetch, ttl, miss_ttl, stale)
            return self._decode(r["value"])
        try:
            return await self._spawn(key, fetch, ttl, miss_ttl, stale)
        except Exception:
            if r:  # even expired data beats a failure
                return self._decode(r["value"])
            raise

    def _spawn(self, key, fetch, ttl, miss_ttl, stale) -> asyncio.Task:
        task = self._inflight.get(key)
        if task and not task.done():
            return task

        async def run():
            try:
                val = await fetch()
                self.put(key, val, miss_ttl if (val is None and miss_ttl) else ttl, stale)
                return val
            finally:
                self._inflight.pop(key, None)

        task = asyncio.ensure_future(run())
        self._inflight[key] = task
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)  # silence unhandled
        return task

    def prune(self) -> int:
        return self.db.execute("DELETE FROM kv_cache WHERE stale_until < ?", (now(),))

    def size(self) -> int:
        r = self.db.one("SELECT COUNT(*) n FROM kv_cache")
        return r["n"] if r else 0
