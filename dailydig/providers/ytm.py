"""YouTube Music via ytmusicapi (sync library -> threads). Used for resolving downloads,
track radio and charts. Cookies (optional) live at <config>/ytm_headers.json."""
from __future__ import annotations

import asyncio
import os

from .base import Provider, ProviderError


def _parse_len(s: str | None) -> int | None:
    if not s:
        return None
    try:
        secs = 0
        for p in s.split(":"):
            secs = secs * 60 + int(p)
        return secs
    except ValueError:
        return None


class YTM(Provider):
    name = "ytm"
    min_interval = 0.5

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api = None

    def _client(self):
        if self._api is None:
            try:
                from ytmusicapi import YTMusic
            except ImportError as e:  # pragma: no cover
                raise ProviderError("ytmusicapi not installed") from e
            auth = os.path.join(self.ctx.settings.config_dir, "ytm_headers.json")
            self._api = YTMusic(auth) if os.path.exists(auth) else YTMusic()
        return self._api

    async def _call(self, fn, *a, **kw):
        if self.limiter:
            await self.limiter.wait()
        try:
            res = await asyncio.to_thread(fn, *a, **kw)
            self._record(True)
            return res
        except Exception as e:  # network / parse errors from the unofficial API
            self._record(False, f"{type(e).__name__}: {e}")
            raise ProviderError(f"ytm: {e}") from e

    async def search_songs(self, query: str, limit: int = 5) -> list[dict]:
        api = self._client()
        res = await self._call(api.search, query, filter="songs", limit=limit, ignore_spelling=True)
        out = []
        for r in res or []:
            if not r.get("videoId"):
                continue
            out.append({"video_id": r["videoId"], "title": r.get("title", ""),
                        "artists": [a.get("name", "") for a in r.get("artists") or []],
                        "album": (r.get("album") or {}).get("name"),
                        "album_id": (r.get("album") or {}).get("id"),
                        "thumbnail": ((r.get("thumbnails") or [{}])[-1]).get("url"),
                        "duration_s": r.get("duration_seconds") or _parse_len(r.get("duration")),
                        "explicit": bool(r.get("isExplicit"))})
        return out

    async def radio(self, video_id: str, limit: int = 25) -> list[dict]:
        api = self._client()
        res = await self._call(api.get_watch_playlist, videoId=video_id, radio=True, limit=limit)
        out = []
        for t in (res or {}).get("tracks") or []:
            out.append({"video_id": t.get("videoId"), "title": t.get("title", ""),
                        "artists": [a.get("name", "") for a in t.get("artist", []) or t.get("artists", []) or []],
                        "duration_s": t.get("length_seconds") or _parse_len(t.get("length"))})
        return [t for t in out if t["video_id"] and t["title"]]

    async def album_info(self, browse_id: str, video_id: str | None = None) -> dict | None:
        """Album name/artist/year plus this track's number, for building library paths."""
        res = await self._call(self._client().get_album, browse_id)
        if not res:
            return None
        tracks = res.get("tracks") or []
        num, total = None, len(tracks)
        for i, t in enumerate(tracks):
            if video_id and t.get("videoId") == video_id:
                num = t.get("trackNumber") or i + 1
        arts = [a.get("name") for a in res.get("artists") or [] if a.get("name")]
        thumbs = res.get("thumbnails") or [{}]
        return {"album": res.get("title"), "album_artist": arts[0] if arts else None, "album_artists": arts,
                "year": str(res.get("year") or "")[:4] or None, "track": num, "tracks_total": total or None,
                "cover": thumbs[-1].get("url"), "type": res.get("type")}

    async def charts(self, country: str = "US") -> dict:
        return await self._call(self._client().get_charts, country.upper())

    @staticmethod
    def watch_url(video_id: str) -> str:
        return f"https://music.youtube.com/watch?v={video_id}"
