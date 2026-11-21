"""Download backend: yt-dlp only. Turns a verified YouTube Music video into a tagged audio file
inside the library (configurable layout, synced lyrics, ReplayGain, format choice)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass

from ..providers.base import ProviderError
from . import files, layout, tagging  # noqa: F401  (files imported for patching in tests)

log = logging.getLogger("dailydig.download")


class DownloadError(ProviderError):
    pass


class RetryLater(Exception):
    def __init__(self, delay: float, why: str = ""):
        super().__init__(why or f"retry in {delay:.0f}s")
        self.delay = delay


@dataclass
class DLRequest:
    video_id: str
    url: str
    title: str
    artist: str
    duration_s: float | None = None
    album: str | None = None
    album_id: str | None = None
    thumbnail: str | None = None
    recording_mbid: str | None = None
    artist_mbid: str | None = None


class YtDlp:
    name = "ytdlp"

    def __init__(self, ctx):
        self.ctx = ctx
        sems = ctx.__dict__.setdefault("_dl_sems", {})
        self.sem = sems.setdefault(self.name, asyncio.Semaphore(max(1, ctx.settings.dl_concurrency)))

    async def health(self) -> dict:
        return {"ok": bool(shutil.which("yt-dlp")), "detail": shutil.which("yt-dlp") or "yt-dlp not installed"}

    async def download(self, req: DLRequest) -> str:
        async with self.sem:
            return await self._download(req)

    async def _metadata(self, req: DLRequest) -> dict:
        meta = {"title": req.title, "artist": req.artist, "album_artist": req.artist, "album": req.album,
                "year": None, "track": None, "tracks_total": None, "cover": req.thumbnail}
        album_id = req.album_id
        if not album_id:  # recover album info from YTM when the search result had none
            try:
                res = await self.ctx.ytm.search_songs(f"{req.artist} {req.title}", limit=5)
                hit = next((r for r in res if r["video_id"] == req.video_id), None)
                if hit:
                    album_id, meta["album"] = hit.get("album_id"), hit.get("album") or meta["album"]
                    meta["cover"] = hit.get("thumbnail") or meta["cover"]
            except Exception:  # noqa: BLE001
                pass
        if album_id:
            try:
                async def f():
                    return await self.ctx.ytm.album_info(album_id, req.video_id)
                info = await self.ctx.cache.get_or_fetch(f"ytm:album:{album_id}:{req.video_id}", f, 30 * 86400,
                                                         miss_ttl=3600)
                if info:
                    meta.update({k: v for k, v in info.items() if v})
            except Exception as e:  # noqa: BLE001
                log.info("album lookup failed for %s: %s", album_id, e)
        if not meta.get("album"):                 # singles: album = track title, like most rippers
            meta["album"], meta["track"] = req.title, 1
        meta["track"] = meta.get("track") or 1
        meta["year"] = meta.get("year") or "0000"
        return meta

    async def run_ytdlp(self, url: str, workdir: str) -> str:
        """Run yt-dlp into `workdir`; returns the downloaded file path. Separate method so tests can stub it."""
        s = self.ctx.settings
        exe = shutil.which("yt-dlp")
        if not exe:
            raise DownloadError("yt-dlp is not installed in this container")
        os.makedirs(workdir, exist_ok=True)
        have_cookies = os.path.exists(s.cookies_path)
        msg = ""
        for use_cookies in ((False, True) if have_cookies else (False,)):
            cmd = [exe, "-f", "bestaudio/best", "-x", "--audio-format", s.audio_format,
                   "--audio-quality", str(s.audio_quality), "--no-playlist", "--no-progress", "--no-warnings",
                   "-o", os.path.join(workdir, "%(id)s.%(ext)s"), "--print", "after_move:filepath"]
            if use_cookies:
                local = os.path.join(workdir, "cookies.txt")   # private copy: yt-dlp rewrites it
                shutil.copyfile(s.cookies_path, local)
                cmd += ["--cookies", local]
            cmd.append(url)
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=s.dl_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise DownloadError(f"yt-dlp timed out after {s.dl_timeout}s") from None
            lines = [l.strip() for l in out.decode("utf-8", "replace").splitlines() if l.strip()]
            if proc.returncode == 0 and lines and os.path.exists(lines[-1]):
                return lines[-1]
            msg = err.decode("utf-8", "replace").strip()
            if not re.search(r"age|sign in|not a bot|cookies|confirm your", msg, re.I):
                break                       # not an auth problem: retrying with cookies won't help
        hint = ""
        if re.search(r"age|sign in|not a bot|cookies|confirm your", msg, re.I):
            hint = (" [cookies rejected: re-export from a private window (youtube.com) and re-upload]"
                    if have_cookies else " [upload YouTube cookies in Downloader → YouTube cookies]")
        raise DownloadError("yt-dlp failed: " + msg[-300:] + hint)

    async def _download(self, req: DLRequest) -> str:
        s = self.ctx.settings
        meta = await self._metadata(req)
        ext = s.audio_format
        target = layout.render(s.path_template, {**meta, "ext": ext, "track": int(meta["track"]),
                                                 "album_artist": meta.get("album_artist") or req.artist},
                               s.discovery_dir, s.ascii_filenames)
        if os.path.exists(target):               # dedupe: same track already stored, reuse it
            return target
        work = os.path.join(s.config_dir, "tmp", req.video_id)
        try:
            tmp = await self.run_ytdlp(req.url, work)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(tmp, target)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        await asyncio.to_thread(tagging.write_basic_tags, target, meta)
        if s.replaygain:
            await asyncio.to_thread(tagging.write_replaygain, target)
        folder = os.path.dirname(target)
        await tagging.save_cover(self.ctx, meta.get("cover"), folder)
        if s.fetch_lyrics:
            lrc = await tagging.fetch_lrc(self.ctx, req.artist, req.title, meta.get("album"), req.duration_s)
            if lrc:
                with open(os.path.splitext(target)[0] + ".lrc", "w", encoding="utf-8") as fh:
                    fh.write(lrc)
        return target


def choose(settings) -> str:
    return "ytdlp"


def get_backend(ctx) -> YtDlp:
    return YtDlp(ctx)
