"""In-process workers, janitor and cron scheduler. Everything is queued through SQLite jobs,
so a crash mid-job just means the lease expires and another worker picks it up."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from . import bandit, cleanup, deliver, learning, playlists, taste
from .db import now
from .download import pipeline

log = logging.getLogger("dailydig.runtime")

# name -> cron. taste-sync also labels outcomes; training also decays the bandit.
SYSTEM_JOBS = {"taste_sync": "7 * * * *", "train_ranker": "30 3 * * *", "cleanup": "30 4 * * *"}


async def h_build(ctx, job):
    pid = job.payload["playlist_id"]
    owner = f"job:{job.id}"
    if not ctx.queue.acquire_lock(f"build:{pid}", owner, ttl=900):
        return {"skipped": "another build is running"}
    try:
        return await playlists.build_playlist(ctx, pid)
    finally:
        ctx.queue.release_lock(f"build:{pid}", owner)


async def h_download(ctx, job):
    return await pipeline.download_track(ctx, job.payload["id"])


async def h_deliver(ctx, job):
    return await deliver.deliver_playlist(ctx, job.payload["playlist_id"], job.payload.get("waits", 0))


async def h_taste_sync(ctx, job):
    out = {"listens_added": await taste.sync_listens(ctx)}
    starred = await taste.sync_navidrome_stars(ctx)
    out["new_stars"] = len(starred)
    out["promoted"] = await cleanup.promote_starred(ctx, starred) if starred else 0
    out.update(learning.label_outcomes(ctx))
    return out


async def h_train(ctx, job):
    bandit.decay(ctx.db, 1.0)
    return await learning.train_ranker(ctx)


async def h_cleanup(ctx, job):
    out = cleanup.expire_discovery(ctx)
    out["cache_pruned"] = ctx.cache.prune()
    out["jobs_pruned"] = ctx.queue.prune()
    return out


HANDLERS = {"build_playlist": h_build, "download_track": h_download, "deliver_playlist": h_deliver,
            "taste_sync": h_taste_sync, "train_ranker": h_train, "cleanup": h_cleanup}


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        n = max(1, self.ctx.settings.workers)
        self.tasks = [asyncio.create_task(self._worker(f"w{i}-{uuid.uuid4().hex[:6]}")) for i in range(n)]
        self.tasks.append(asyncio.create_task(self._janitor()))
        if self.ctx.settings.enable_scheduler:
            self.tasks.append(asyncio.create_task(self._scheduler()))

    async def stop(self) -> None:
        self._stop.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _heartbeat(self, job) -> None:
        while True:
            await asyncio.sleep(20)
            if not self.ctx.queue.heartbeat(job):
                return

    async def _worker(self, wid: str) -> None:
        q = self.ctx.queue
        while not self._stop.is_set():
            job = q.claim(wid, list(HANDLERS))
            if not job:
                await asyncio.sleep(1.0)
                continue
            hb = asyncio.create_task(self._heartbeat(job))
            try:
                res = await HANDLERS[job.kind](self.ctx, job)
                if isinstance(res, dict) and res.get("wait"):      # e.g. downloads still running
                    payload = {**job.payload, "waits": job.payload.get("waits", 0) + 1}
                    self.ctx.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job.id))
                    q.reschedule(job, float(res["wait"]))
                else:
                    q.complete(job, res)
            except pipeline.RetryLater as e:
                q.reschedule(job, e.delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("job %s (%s) failed", job.id, job.kind)
                q.fail(job, f"{type(e).__name__}: {e}")
            finally:
                hb.cancel()

    async def _janitor(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.ctx.queue.reset_expired()
                if n:
                    log.warning("reset %d expired job leases", n)
            except Exception:  # noqa: BLE001
                log.exception("janitor")
            await asyncio.sleep(30)

    async def tick(self) -> None:
        """One scheduler pass (also called directly by tests)."""
        ctx, t = self.ctx, now()
        for pl in ctx.db.query("SELECT id, schedule_cron FROM playlists WHERE enabled=1 AND next_run<=?", (t,)):
            ctx.queue.enqueue("build_playlist", {"playlist_id": pl["id"]}, dedupe_key=f"build:{pl['id']}", max_attempts=2)
            ctx.db.execute("UPDATE playlists SET next_run=? WHERE id=?", (playlists.next_run(pl["schedule_cron"], t), pl["id"]))
        for name, cron in SYSTEM_JOBS.items():
            due = ctx.db.kv_get(f"sched:{name}", 0)
            if due <= t:
                ctx.queue.enqueue(name, {}, dedupe_key=f"sys:{name}", max_attempts=2)
                ctx.db.kv_set(f"sched:{name}", playlists.next_run(cron, t))

    async def _scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick")
            await asyncio.sleep(30)
