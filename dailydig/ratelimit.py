"""Cross-process rate limiting without a broker.

Every caller books a future time slot in SQLite with one atomic UPDATE...RETURNING,
so any number of workers/processes share one polite request budget.
"""
from __future__ import annotations

import asyncio

from .db import DB, now


class RateLimiter:
    def __init__(self, db: DB, name: str, min_interval_s: float):
        self.db, self.name, self.min_ms = db, name, int(min_interval_s * 1000)

    def book(self) -> float:
        """Reserve the next slot; returns seconds the caller must wait."""
        t_ms = now() * 1000
        with self.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO rate_slots(name,next_slot) VALUES(?,0)", (self.name,))
            row = c.execute(
                "UPDATE rate_slots SET next_slot = MAX(next_slot, ?) + ? WHERE name=? RETURNING next_slot",
                (t_ms, self.min_ms, self.name)).fetchone()
        slot = row[0] - self.min_ms
        return max(0.0, (slot - t_ms) / 1000)

    async def wait(self) -> None:
        w = self.book()
        if w > 0:
            await asyncio.sleep(w)

    def pause(self, seconds: float, cap: float = 900) -> None:
        """Push the whole budget back (e.g. a 429 / x-ratelimit-reset-in)."""
        until = (now() + min(seconds, cap)) * 1000
        with self.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO rate_slots(name,next_slot) VALUES(?,0)", (self.name,))
            c.execute("UPDATE rate_slots SET next_slot=MAX(next_slot, ?) WHERE name=?", (until, self.name))
