"""File-side helpers: find what yubal wrote, probe/validate it, write MusicBrainz IDs."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from ..models import norm
from .matcher import DIFFERENT_RECORDING_S, passes_bitrate_floor

AUDIO_EXT = {".opus", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wav"}
LOSSLESS_EXT = {".flac", ".wav"}


@dataclass
class Probe:
    duration_s: float
    bitrate_kbps: float
    size: int


def probe(path: str) -> Probe:
    size = os.path.getsize(path)
    dur = 0.0
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                                 capture_output=True, text=True, timeout=30)
            dur = float(json.loads(out.stdout or "{}").get("format", {}).get("duration") or 0)
        except (subprocess.SubprocessError, ValueError):
            dur = 0.0
    if not dur:
        import mutagen
        f = mutagen.File(path)
        dur = float(getattr(getattr(f, "info", None), "length", 0) or 0)
    return Probe(dur, (size * 8 / dur / 1000) if dur else 0.0, size)


def validate(path: str, expected_s: float | None) -> tuple[bool, str]:
    p = probe(path)
    if p.duration_s <= 0:
        return False, "unreadable audio (no duration)"
    lossless = os.path.splitext(path)[1].lower() in LOSSLESS_EXT
    if not passes_bitrate_floor(p.size, p.duration_s, lossless):
        return False, f"bitrate {p.bitrate_kbps:.0f} kbit/s below anti-fake floor"
    if expected_s and abs(p.duration_s - expected_s) > DIFFERENT_RECORDING_S:
        return False, f"duration {p.duration_s:.0f}s vs expected {expected_s:.0f}s: different recording"
    return True, "ok"


def _tags(path: str) -> tuple[str, str]:
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f and f.tags:
            return (f.get("title") or [""])[0], (f.get("artist") or [""])[0]
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def locate_new_file(root: str, since: float, title: str, artist: str) -> str | None:
    """Newest audio file under `root` created since `since`, preferring the one whose tags match."""
    from rapidfuzz import fuzz
    fresh = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                p = os.path.join(dp, fn)
                try:
                    if os.path.getmtime(p) >= since - 5:
                        fresh.append(p)
                except OSError:
                    pass
    if not fresh:
        return None
    best, best_s = None, -1.0
    for p in fresh:
        t, a = _tags(p)
        s = fuzz.ratio(norm(t or os.path.basename(p)), norm(title)) * 0.7 + fuzz.partial_ratio(norm(a), norm(artist)) * 0.3
        if s > best_s:
            best, best_s = p, s
    return best if (best_s >= 60 or len(fresh) == 1) else None


def write_mbids(path: str, recording: str | None, artist: str | None, release: str | None = None) -> bool:
    """Picard-compatible MusicBrainz tags so Navidrome's scrobbles carry MBIDs (the accuracy flywheel)."""
    if not (recording or artist or release):
        return False
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, TXXX, UFID
            try:
                t = ID3(path)
            except ID3NoHeaderError:
                t = ID3()
            if recording:
                t.add(UFID(owner="http://musicbrainz.org", data=recording.encode()))
            if artist:
                t.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=artist))
            if release:
                t.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=release))
            t.save(path)
        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4, MP4FreeForm
            m = MP4(path)
            for name, val in (("MusicBrainz Track Id", recording), ("MusicBrainz Artist Id", artist),
                              ("MusicBrainz Album Id", release)):
                if val:
                    m[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(val.encode())]
            m.save()
        else:  # opus / ogg / flac: vorbis comments
            import mutagen
            f = mutagen.File(path)
            if f is None:
                return False
            if f.tags is None:
                f.add_tags()
            for k, val in (("musicbrainz_trackid", recording), ("musicbrainz_artistid", artist),
                           ("musicbrainz_albumid", release)):
                if val:
                    f[k] = [val]
            f.save()
        return True
    except Exception:  # noqa: BLE001 - tagging is best effort; the file is still good
        return False


async def aprobe(path: str) -> Probe:
    return await asyncio.to_thread(probe, path)
