"""Navidrome (Subsonic API) client: stars, library lookups, playlist delivery, scans."""
from __future__ import annotations

import asyncio
import hashlib
import secrets

from rapidfuzz import fuzz

from .models import norm
from .providers.base import Provider, ProviderError


class Navidrome(Provider):
    name = "navidrome"
    min_interval = 0.05

    @property
    def configured(self) -> bool:
        s = self.ctx.settings
        return bool(s.navidrome_url and s.navidrome_user)

    def _auth(self) -> dict:
        s = self.ctx.settings
        salt = secrets.token_hex(6)
        token = hashlib.md5((s.navidrome_pass + salt).encode()).hexdigest()
        return {"u": s.navidrome_user, "t": token, "s": salt, "v": "1.16.1", "c": "dailydig", "f": "json"}

    async def _api(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.configured:
            raise ProviderError("navidrome not configured")
        url = f"{self.ctx.settings.navidrome_url.rstrip('/')}/rest/{endpoint}"
        j = await self.get_json(url, {**self._auth(), **(params or {})})
        resp = (j or {}).get("subsonic-response") or {}
        if resp.get("status") != "ok":
            raise ProviderError(f"navidrome {endpoint}: {resp.get('error') or 'bad response'}")
        return resp

    async def ping(self) -> bool:
        try:
            await self._api("ping")
            return True
        except Exception:
            return False

    async def starred(self) -> list[dict]:
        r = await self._api("getStarred2")
        return ((r.get("starred2") or {}).get("song")) or []

    async def find_song(self, artist: str, title: str) -> dict | None:
        r = await self._api("search3", {"query": f"{artist} {title}", "songCount": 10, "artistCount": 0, "albumCount": 0})
        best, best_s = None, 0.0
        for s in ((r.get("searchResult3") or {}).get("song")) or []:
            sc = (fuzz.ratio(norm(s.get("title", "")), norm(title)) * 0.6 +
                  fuzz.ratio(norm(s.get("artist", "")), norm(artist)) * 0.4)
            if sc > best_s:
                best, best_s = s, sc
        return best if best_s >= 85 else None

    async def playlists(self) -> list[dict]:
        r = await self._api("getPlaylists")
        return ((r.get("playlists") or {}).get("playlist")) or []

    async def sync_playlist(self, name: str, song_ids: list[str]) -> str:
        """Create or fully replace the playlist called `name`."""
        existing = next((p for p in await self.playlists() if p.get("name") == name), None)
        params: dict = {"songId": song_ids}
        if existing:
            params["playlistId"] = existing["id"]
        else:
            params["name"] = name
        r = await self._api("createPlaylist", params)
        return (r.get("playlist") or existing or {}).get("id", "")

    async def start_scan(self, wait: bool = True, timeout: float = 600) -> None:
        await self._api("startScan")
        if not wait:
            return
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(3)
            waited += 3
            st = (await self._api("getScanStatus")).get("scanStatus") or {}
            if not st.get("scanning"):
                return
