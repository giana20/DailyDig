"""resolve -> verify -> download (pluggable backend) -> post-validate -> MBID-tag."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from ..models import norm
from . import files
from .backends import DLRequest, DownloadError, RetryLater, get_backend  # noqa: F401  (RetryLater re-exported)
from .matcher import pick_best

log = logging.getLogger("dailydig.download")


def _album_hint(ctx, artist: str, title: str, vid: str) -> dict:
    """Album id/name/thumbnail from the cached YTM search that picked this video (if any)."""
    r = ctx.db.one("SELECT value FROM kv_cache WHERE key=?", (f"ytm:search:{norm(artist)}|{norm(title)}",))
    if r:
        try:
            for c in json.loads(r["value"]) or []:
                if c.get("video_id") == vid:
                    return {"album": c.get("album"), "album_id": c.get("album_id"), "thumbnail": c.get("thumbnail")}
        except (ValueError, TypeError, AttributeError):
            pass
    return {}


def _set(ctx, row_id: int, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    ctx.db.execute(f"UPDATE playlist_tracks SET {sets} WHERE id=?", (*cols.values(), row_id))


async def resolve_mbid(ctx, artist: str, title: str) -> dict | None:
    if not ctx.mb.enabled:
        return None

    async def f():
        return await ctx.mb.search_recording(artist, title)
    return await ctx.cache.get_or_fetch(f"mb:rec:{norm(artist)}|{norm(title)}", f, 30 * 86400, miss_ttl=12 * 3600)


async def download_track(ctx, row_id: int) -> dict:
    row = ctx.db.one("SELECT * FROM playlist_tracks WHERE id=?", (row_id,))
    if not row:
        return {"skipped": "missing"}
    if row["status"] not in ("pending", "failed", "approved"):
        return {"skipped": row["status"]}
    _set(ctx, row_id, status="downloading", error=None)
    try:
        return await _download(ctx, row)
    except RetryLater:
        _set(ctx, row_id, status="pending")
        raise
    except Exception as e:  # noqa: BLE001
        _set(ctx, row_id, status="failed", error=f"{type(e).__name__}: {e}"[:500])
        raise


async def _download(ctx, row) -> dict:
    rid, title, artist = row["id"], row["title"], row["artist"]
    expected_s = row["duration_ms"] / 1000 if row["duration_ms"] else None
    mbid, amb = row["recording_mbid"], row["artist_mbid"]

    if not mbid:
        r = await resolve_mbid(ctx, artist, title)
        if r:
            mbid, amb = r["mbid"], amb or r.get("artist_mbid")
            expected_s = expected_s or (r["duration_ms"] / 1000 if r.get("duration_ms") else None)
            _set(ctx, rid, recording_mbid=mbid, artist_mbid=amb)

    vid = row["video_id"]
    if not vid:
        async def search():
            return await ctx.ytm.search_songs(f"{artist} {title}", limit=5)
        results = await ctx.cache.get_or_fetch(f"ytm:search:{norm(artist)}|{norm(title)}", search, 6 * 3600)
        verdict = pick_best(title, artist, expected_s, results or [])
        if verdict.level == "reject":
            _set(ctx, rid, status="failed", error="no confident match: " + "; ".join(verdict.reasons)[:300])
            return {"status": "failed", "reasons": verdict.reasons}
        if verdict.level != "accept":
            _set(ctx, rid, status="review", video_id=verdict.video_id,
                 error=f"{verdict.level}: " + "; ".join(verdict.reasons)[:300])
            return {"status": "review", "reasons": verdict.reasons}
        vid = verdict.video_id
        _set(ctx, rid, video_id=vid)

    req = DLRequest(video_id=vid, url=ctx.ytm.watch_url(vid), title=title, artist=artist, duration_s=expected_s,
                    recording_mbid=mbid, artist_mbid=amb, **_album_hint(ctx, artist, title, vid))
    path = await get_backend(ctx).download(req)
    if not path or not os.path.exists(path):
        raise DownloadError("download finished but no audio file was found")

    ok, why = await asyncio.to_thread(files.validate, path, expected_s)
    if not ok:
        try:
            os.remove(path)
        except OSError:
            pass
        _set(ctx, rid, status="failed", error=f"post-validation: {why}")
        return {"status": "failed", "reason": why}
    tagged = await asyncio.to_thread(files.write_mbids, path, mbid, amb)
    _set(ctx, rid, status="downloaded", file_path=path, error=None)
    return {"status": "downloaded", "path": path, "tagged": tagged}


def approve(ctx, row_id: int, video_id: str | None = None) -> bool:
    """Manual review decision overrides the matcher."""
    row = ctx.db.one("SELECT video_id FROM playlist_tracks WHERE id=?", (row_id,))
    if not row:
        return False
    vid = video_id or row["video_id"]
    if not vid:
        return False
    _set(ctx, row_id, status="approved", video_id=vid, error=None)
    ctx.queue.enqueue("download_track", {"id": row_id}, dedupe_key=f"dl:{row_id}", max_attempts=2)
    return True
