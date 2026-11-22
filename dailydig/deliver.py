"""Hand playlists to Navidrome: M3U8 in the playlists folder + Subsonic createPlaylist."""
from __future__ import annotations

import logging
import os
import re

from .db import now

log = logging.getLogger("dailydig.deliver")
MAX_WAITS = 40          # x 30 s = 20 min of waiting for downloads before delivering what we have


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "playlist"


def write_m3u8(ctx, name: str, rows: list) -> str | None:
    items = [r for r in rows if r["file_path"] and os.path.exists(r["file_path"])]
    if not items:
        return None
    os.makedirs(ctx.settings.playlists_dir, exist_ok=True)
    path = os.path.join(ctx.settings.playlists_dir, f"{_safe(name)}.m3u8")
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for r in items:
        secs = int((r["duration_ms"] or 0) / 1000) or -1
        lines.append(f"#EXTINF:{secs},{r['artist']} - {r['title']}")
        lines.append(os.path.relpath(r["file_path"], ctx.settings.playlists_dir))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


async def deliver_playlist(ctx, playlist_id: int, waits: int = 0) -> dict:
    pl = ctx.db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    rows = ctx.db.query("SELECT * FROM playlist_tracks WHERE playlist_id=? AND status!='retired' ORDER BY position",
                        (playlist_id,))
    if not pl or not rows:
        return {"delivered": 0}
    busy = [r for r in rows if r["status"] in ("pending", "downloading", "approved")]
    if busy and waits < MAX_WAITS:
        return {"wait": 30, "pending": len(busy)}

    ready = [r for r in rows if r["status"] in ("downloaded", "delivered", "library")]
    out: dict = {"tracks": len(ready), "waiting_on": len(busy)}
    out["m3u8"] = write_m3u8(ctx, pl["name"], [r for r in ready if r["status"] != "library"])
    nd = ctx.navidrome
    if nd.configured and nd.enabled:
        try:
            if any(r["status"] == "downloaded" for r in ready):
                await nd.start_scan(wait=True)   # scan imports the m3u8
        except Exception as e:  # noqa: BLE001
            out["navidrome_error"] = f"{type(e).__name__}: {e}"
    ctx.db.execute("UPDATE playlist_tracks SET status='delivered' WHERE playlist_id=? AND status='downloaded'", (playlist_id,))
    return out
