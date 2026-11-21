"""Download backends. A backend turns a verified YouTube Music video into an audio file on disk.

  yubal_cli  runs your yubal CLI command template (e.g. over ssh + `pct exec`) and parses its output
  yubal_api  talks to yubal's web API (endpoint discovered from its OpenAPI spec)
  ytdlp      native yt-dlp with a configurable folder layout, lyrics, ReplayGain, format choice

All of them return the final file path as seen from THIS container.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass

from ..providers.base import ProviderError
from . import files, layout, tagging
from .yubal import QueueFull, Yubal

log = logging.getLogger("dailydig.download")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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


def map_path(settings, p: str) -> str:
    """Translate a path printed by yubal (inside its container) into this container's view."""
    src, _, dst = (settings.yubal_path_map or "").partition("=")
    if src and dst and (p == src or p.startswith(src.rstrip("/") + "/")):
        return dst.rstrip("/") + p[len(src.rstrip("/")):]
    return p


def parse_yubal_stdout(stdout: str) -> list[dict]:
    """Same extraction your n8n node does: 'OK → <path>' lines (paths may wrap across lines) and the
    Title/Artist metadata table rows."""
    text = ANSI.sub("", stdout)
    paths = [re.sub(r"[\r\n]+", "", m.group(1)).strip()
             for m in re.finditer(r"OK\s*→\s*([\s\S]*?)(?=\s*\n\s*Downloading|\Z)", text)]
    titles = [m.group(1).strip() for m in re.finditer(r"│\s*Title\s*│\s*(.*?)\s*│", text)]
    artists = [m.group(1).strip() for m in re.finditer(r"│\s*Artist\s*│\s*(.*?)\s*│", text)]
    return [{"path": p, "title": titles[i] if i < len(titles) else None,
             "artist": artists[i] if i < len(artists) else None} for i, p in enumerate(paths) if p]


class Backend:
    name = "base"

    def __init__(self, ctx):
        self.ctx = ctx
        sems = ctx.__dict__.setdefault("_dl_sems", {})
        self.sem = sems.setdefault(self.name, asyncio.Semaphore(max(1, ctx.settings.dl_concurrency)))

    async def health(self) -> dict:
        return {"ok": True}

    async def download(self, req: DLRequest) -> str:
        async with self.sem:
            return await self._download(req)

    async def _download(self, req: DLRequest) -> str:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------------------------------
class YubalCLI(Backend):
    name = "yubal_cli"

    async def health(self) -> dict:
        cmd = self.ctx.settings.yubal_cmd
        return {"ok": bool(cmd), "detail": cmd or "DD_YUBAL_CMD not set"}

    def argv(self, url: str, dest: str) -> list[str]:
        parts = shlex.split(self.ctx.settings.yubal_cmd)
        if not parts:
            raise DownloadError("DD_YUBAL_CMD is empty")
        return [p.replace("{url}", url).replace("{dest}", dest) for p in parts]

    async def _download(self, req: DLRequest) -> str:
        s = self.ctx.settings
        started = time.time()
        argv = self.argv(req.url, s.yubal_dest)
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as e:
            raise DownloadError(f"cannot run {argv[0]!r}: {e}. Is it installed in this container "
                                f"(ssh needs openssh-client and a mounted key)?") from e
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=s.dl_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise DownloadError(f"yubal command timed out after {s.dl_timeout}s") from None
        text = ANSI.sub("", out.decode("utf-8", "replace"))
        if proc.returncode != 0:
            raise DownloadError(f"yubal exited {proc.returncode}: {text.strip()[-300:]}")
        for item in parse_yubal_stdout(text):
            p = map_path(s, item["path"])
            if os.path.exists(p):
                return p
        # output format changed or path map is off: fall back to "newest audio file under the destination"
        dest_local = map_path(s, s.yubal_dest)
        found = await asyncio.to_thread(files.locate_new_file, dest_local, started, req.title, req.artist)
        if found:
            return found
        raise DownloadError("yubal finished but its output file was not found. Check DD_YUBAL_PATH_MAP "
                            f"(now {s.yubal_path_map!r}). Output tail: {text.strip()[-200:]}")


# --------------------------------------------------------------------------------------------------
class YubalAPI(Backend):
    name = "yubal_api"

    async def health(self) -> dict:
        return await Yubal(self.ctx).health()

    async def _download(self, req: DLRequest) -> str:
        s, yb, started = self.ctx.settings, Yubal(self.ctx), time.time()
        if not (await yb.health()).get("ok"):
            raise DownloadError(f"yubal at {s.yubal_url} is not reachable")
        try:
            job_id = await yb.create_job(req.url, max_items=1)
        except QueueFull as e:
            raise RetryLater(45, str(e)) from e
        await yb.wait(job_id, timeout=s.dl_timeout)
        root = map_path(s, s.yubal_data_dir or s.yubal_dest)
        found = await asyncio.to_thread(files.locate_new_file, root, started, req.title, req.artist)
        if not found:
            raise DownloadError("yubal job finished but no new audio file was found (check DD_YUBAL_DATA_DIR)")
        return found


# --------------------------------------------------------------------------------------------------
class YtDlp(Backend):
    name = "ytdlp"

    async def health(self) -> dict:
        return {"ok": bool(shutil.which("yt-dlp")), "detail": shutil.which("yt-dlp") or "yt-dlp not installed"}

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


BACKENDS = {"yubal_cli": YubalCLI, "yubal_api": YubalAPI, "ytdlp": YtDlp}


def choose(settings) -> str:
    d = settings.downloader
    if d in BACKENDS:
        return d
    if settings.yubal_cmd:
        return "yubal_cli"
    if settings.yubal_url:
        return "yubal_api"
    return "ytdlp"


def get_backend(ctx) -> Backend:
    return BACKENDS[choose(ctx.settings)](ctx)
