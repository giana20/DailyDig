"""Keep/rotate: unloved discovery files expire; starred ones are promoted into the main library."""
from __future__ import annotations

import logging
import os
import shutil

from .db import now
from .models import norm

log = logging.getLogger("dailydig.cleanup")


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)


def _prune_empty(start: str, stop: str) -> None:
    d = os.path.abspath(start)
    stop = os.path.abspath(stop)
    while d != stop and _inside(d, stop):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def expire_discovery(ctx) -> dict:
    if not ctx.settings.staging or ctx.settings.expire_days <= 0:
        return {"expired_files": 0}     # downloads live in the library itself: never delete anything
    cutoff = now() - ctx.settings.expire_days * 86400
    root = ctx.settings.discovery_dir
    removed = 0
    for r in ctx.db.query("SELECT id,file_path FROM playlist_tracks WHERE status='retired' AND file_path IS NOT NULL AND added_at<?",
                          (cutoff,)):
        fp = r["file_path"]
        in_use = ctx.db.one("SELECT 1 FROM playlist_tracks WHERE file_path=? AND status NOT IN ('retired','expired')", (fp,))
        if not in_use and _inside(fp, root) and os.path.exists(fp):
            try:
                os.remove(fp)
                _prune_empty(os.path.dirname(fp), root)
                removed += 1
            except OSError as e:
                log.warning("could not remove %s: %s", fp, e)
                continue
        if not in_use:
            ctx.db.execute("UPDATE playlist_tracks SET status='expired', file_path=NULL WHERE id=?", (r["id"],))
    return {"expired_files": removed}


def promote(ctx, row_id: int) -> str | None:
    """Move the track's album folder from _discovery/ into the main library (same relative path)."""
    if not ctx.settings.staging:
        return None                      # already in the library
    r = ctx.db.one("SELECT file_path FROM playlist_tracks WHERE id=?", (row_id,))
    root, lib = ctx.settings.discovery_dir, ctx.settings.music_dir
    if not r or not r["file_path"] or not os.path.exists(r["file_path"]) or not _inside(r["file_path"], root):
        return None
    src_dir = os.path.dirname(r["file_path"])
    rel = os.path.relpath(src_dir, root)
    dst_dir = os.path.join(lib, rel)
    os.makedirs(dst_dir, exist_ok=True)
    for fn in os.listdir(src_dir):
        shutil.move(os.path.join(src_dir, fn), os.path.join(dst_dir, fn))
    _prune_empty(src_dir, root)
    new_path = os.path.join(dst_dir, os.path.basename(r["file_path"]))
    ctx.db.execute("UPDATE playlist_tracks SET file_path=? WHERE file_path=?", (new_path, r["file_path"]))
    ctx.db.execute("UPDATE playlist_tracks SET status='promoted' WHERE id=?", (row_id,))
    return new_path


async def promote_starred(ctx, starred: list[dict]) -> int:
    """Navidrome stars are the strongest positive: keep the song permanently."""
    n = 0
    if not ctx.settings.staging:
        return 0
    for s in starred:
        name_key = f"{norm(s.get('artist', ''))}|{norm(s.get('title', ''))}"
        for r in ctx.db.query("SELECT id, artist, title, file_path, status FROM playlist_tracks WHERE file_path IS NOT NULL "
                              "AND status IN ('downloaded','delivered','retired')"):
            if f"{norm(r['artist'])}|{norm(r['title'])}" == name_key and promote(ctx, r["id"]):
                n += 1
    if n:
        try:
            if ctx.navidrome.configured:
                await ctx.navidrome.start_scan(wait=False)
        except Exception:  # noqa: BLE001
            pass
    return n
