"""Tags, ReplayGain, lyrics and cover for the native backend (the extras yubal gives you)."""
from __future__ import annotations

import logging
import os
import re
import subprocess

log = logging.getLogger("dailydig.tagging")
RG_REFERENCE_LUFS = -18.0


def write_basic_tags(path: str, meta: dict) -> bool:
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is None:
            return False
        if f.tags is None:
            f.add_tags()
        mapping = {"title": meta.get("title"), "artist": meta.get("artist"), "album": meta.get("album"),
                   "albumartist": meta.get("album_artist"), "date": meta.get("year")}
        for k, v in mapping.items():
            if v:
                f[k] = [str(v)]
        if meta.get("track"):
            total = f"/{meta['tracks_total']}" if meta.get("tracks_total") else ""
            f["tracknumber"] = [f"{meta['track']}{total}"]
        f.save()
        return True
    except Exception as e:  # noqa: BLE001 - a tagging hiccup must not lose the download
        log.warning("tagging %s failed: %s", path, e)
        return False


def measure_loudness(path: str) -> tuple[float, float] | None:
    """EBU R128 integrated loudness (LUFS) and true peak (dBFS) via ffmpeg."""
    try:
        p = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", path, "-af", "ebur128=peak=true",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=180)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    tail = p.stderr[p.stderr.rfind("Summary:"):] if "Summary:" in p.stderr else p.stderr
    i = re.findall(r"\bI:\s+(-?\d+(?:\.\d+)?)\s+LUFS", tail)
    pk = re.findall(r"\bPeak:\s+(-?\d+(?:\.\d+)?)\s+dBFS", tail)
    if not i:
        return None
    return float(i[-1]), float(pk[-1]) if pk else 0.0


def write_replaygain(path: str) -> dict | None:
    m = measure_loudness(path)
    if not m:
        return None
    lufs, peak_db = m
    gain = RG_REFERENCE_LUFS - lufs
    peak = 10 ** (peak_db / 20.0)
    vals = {"REPLAYGAIN_TRACK_GAIN": f"{gain:+.2f} dB", "REPLAYGAIN_TRACK_PEAK": f"{peak:.6f}"}
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
            try:
                t = ID3(path)
            except ID3NoHeaderError:
                t = ID3()
            for k, v in vals.items():
                t.add(TXXX(encoding=3, desc=k, text=v))
            t.save(path)
        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4, MP4FreeForm
            mp = MP4(path)
            for k, v in vals.items():
                mp[f"----:com.apple.iTunes:{k}"] = [MP4FreeForm(v.encode())]
            mp.save()
        else:
            import mutagen
            f = mutagen.File(path)
            if f is None:
                return None
            if f.tags is None:
                f.add_tags()
            for k, v in vals.items():
                f[k.lower()] = [v]
            f.save()
        return {"lufs": lufs, "gain_db": round(gain, 2)}
    except Exception as e:  # noqa: BLE001
        log.warning("replaygain %s failed: %s", path, e)
        return None


async def fetch_lrc(ctx, artist: str, title: str, album: str | None, duration_s: float | None) -> str | None:
    """Synced lyrics from lrclib.net (same source yubal uses). Returns .lrc text or None."""
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_s:
        params["duration"] = int(round(duration_s))
    try:
        r = await ctx.http.get("https://lrclib.net/api/get", params=params, timeout=15,
                               headers={"User-Agent": ctx.settings.user_agent})
        if r.status_code == 200:
            return (r.json() or {}).get("syncedLyrics") or None
    except Exception as e:  # noqa: BLE001
        log.info("lrclib lookup failed: %s", e)
    return None


async def save_cover(ctx, url: str | None, folder: str) -> None:
    dest = os.path.join(folder, "cover.jpg")
    if not url or os.path.exists(dest):
        return
    try:
        r = await ctx.http.get(url, timeout=20)
        if r.status_code == 200 and r.content:
            with open(dest, "wb") as fh:
                fh.write(r.content)
    except Exception as e:  # noqa: BLE001
        log.info("cover download failed: %s", e)
