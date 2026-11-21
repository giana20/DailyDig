import asyncio
import time

import pytest

from dailydig.models import norm, track_key, mbid_from_url
from dailydig.ratelimit import RateLimiter


def test_norm_and_keys():
    assert norm("Beyoncé – Halo (feat. X) [Remastered]") == "beyonce halo"
    assert track_key(None, "Sigur Rós", "Hoppípolla").startswith("n:sigur ros|")
    assert track_key("abc", "x", "y") == "abc"
    u = "https://musicbrainz.org/recording/0e1f5d2b-1c3e-4c0a-9d6e-111111111111"
    assert mbid_from_url(u) == "0e1f5d2b-1c3e-4c0a-9d6e-111111111111"


async def test_queue_claim_fencing(ctx):
    q = ctx.queue
    q.enqueue("x", {"a": 1})
    j1 = q.claim("w1")
    assert j1 and j1.payload == {"a": 1} and j1.attempts == 1
    assert q.claim("w2") is None                       # nothing else queued
    # lease expires -> job reset -> w2 claims it; zombie w1 cannot settle it
    ctx.db.execute("UPDATE jobs SET lease_until=0")
    assert q.reset_expired() == 1
    j2 = q.claim("w2")
    assert j2 and j2.attempts == 2
    assert q.complete(j1, "zombie") is False
    assert q.complete(j2, "ok") is True
    assert q.counts() == {"done": 1}


async def test_queue_retry_and_dedupe(ctx):
    q = ctx.queue
    assert q.enqueue("k", dedupe_key="d") is not None
    assert q.enqueue("k", dedupe_key="d") is None
    j = q.claim("w")
    q.fail(j, "boom", retry_in=0)
    j = q.claim("w")
    assert j.attempts == 2
    q.fail(j, "boom", final=True)
    assert q.counts()["failed"] == 1


async def test_locks(ctx):
    q = ctx.queue
    assert q.acquire_lock("a", "me") and not q.acquire_lock("a", "you")
    q.release_lock("a", "me")
    assert q.acquire_lock("a", "you")


async def test_rate_limiter_books_distinct_slots(ctx):
    rl = RateLimiter(ctx.db, "t", 0.2)
    waits = [rl.book() for _ in range(4)]
    assert waits[0] < 0.05
    assert all(b - a > 0.15 for a, b in zip(waits, waits[1:]))


async def test_cache_swr_and_coalescing(ctx):
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"v": calls}

    res = await asyncio.gather(*[ctx.cache.get_or_fetch("k", fetch, 60) for _ in range(5)])
    assert calls == 1 and all(r == {"v": 1} for r in res)
    # expire freshness, keep stale window: stale value returned, refresh in background
    ctx.db.execute("UPDATE kv_cache SET fresh_until=?", (time.time() - 1,))
    assert await ctx.cache.get_or_fetch("k", fetch, 60) == {"v": 1}
    await asyncio.sleep(0.2)
    assert await ctx.cache.get_or_fetch("k", fetch, 60) == {"v": 2}


async def test_cache_negative_caching(ctx):
    n = 0

    async def miss():
        nonlocal n
        n += 1
        return None

    assert await ctx.cache.get_or_fetch("m", miss, 60, miss_ttl=30) is None
    assert await ctx.cache.get_or_fetch("m", miss, 60, miss_ttl=30) is None
    assert n == 1
