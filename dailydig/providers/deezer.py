"""Deezer public API: related artists, top tracks, charts, albums, 30 s previews."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..models import norm
from .base import Provider


class Deezer(Provider):
    name = "deezer"
    min_interval = 0.11
    base_url = "https://api.deezer.com"

    async def search_artist(self, name: str) -> dict | None:
        j = await self.get_json("/search/artist", {"q": name, "limit": 5})
        best, best_s = None, 0
        for a in (j or {}).get("data") or []:
            s = fuzz.ratio(norm(a.get("name", "")), norm(name))
            if s > best_s:
                best, best_s = a, s
        return best if best and best_s >= 85 else None

    async def related(self, artist_id: int | str, limit: int = 20) -> list[dict]:
        j = await self.get_json(f"/artist/{artist_id}/related", {"limit": limit})
        return (j or {}).get("data") or []

    async def top_tracks(self, artist_id: int | str, limit: int = 10) -> list[dict]:
        j = await self.get_json(f"/artist/{artist_id}/top", {"limit": limit})
        return (j or {}).get("data") or []

    async def chart_tracks(self, limit: int = 50, genre_id: int = 0) -> list[dict]:
        j = await self.get_json(f"/chart/{genre_id}/tracks", {"limit": limit})
        return (j or {}).get("data") or []

    async def search_album(self, artist: str, album: str) -> dict | None:
        j = await self.get_json("/search/album", {"q": f'artist:"{artist}" album:"{album}"', "limit": 5})
        best, best_s = None, 0
        for a in (j or {}).get("data") or []:
            s = (fuzz.ratio(norm(a.get("title", "")), norm(album)) +
                 fuzz.ratio(norm((a.get("artist") or {}).get("name", "")), norm(artist))) / 2
            if s > best_s:
                best, best_s = a, s
        return best if best and best_s >= 80 else None

    async def album_tracks(self, album_id: int | str) -> list[dict]:
        j = await self.get_json(f"/album/{album_id}/tracks", {"limit": 100})
        return (j or {}).get("data") or []

    async def track(self, track_id: int | str) -> dict | None:
        return await self.get_json(f"/track/{track_id}")

    async def search_track(self, artist: str, title: str) -> dict | None:
        j = await self.get_json("/search", {"q": f'artist:"{artist}" track:"{title}"', "limit": 5})
        for t in (j or {}).get("data") or []:
            if fuzz.ratio(norm(t.get("title", "")), norm(title)) >= 85:
                return t
        return None


def rank_to_pct(rank: int | float | None) -> float | None:
    """Deezer 'rank' is roughly 0..1,000,000; map to a coarse 0..100 popularity percentile."""
    if rank is None:
        return None
    return max(0.0, min(100.0, float(rank) / 10_000.0))
