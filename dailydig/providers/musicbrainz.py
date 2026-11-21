"""MusicBrainz: canonical identity. 1 req/s, descriptive User-Agent, aggressive caching."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..models import norm
from .base import Provider


class MusicBrainz(Provider):
    name = "musicbrainz"
    min_interval = 1.05
    base_url = "https://musicbrainz.org/ws/2"

    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.ctx.settings.user_agent, "Accept": "application/json"}

    async def search_artist(self, name: str) -> dict | None:
        j = await self.get_json("/artist", {"query": f'artist:"{name}"', "fmt": "json", "limit": 3})
        for a in (j or {}).get("artists") or []:
            if int(a.get("score", 0)) >= 90 and fuzz.ratio(norm(a.get("name", "")), norm(name)) >= 85:
                return {"mbid": a["id"], "name": a["name"]}
        return None

    async def search_recording(self, artist: str, title: str) -> dict | None:
        j = await self.get_json("/recording", {"query": f'recording:"{title}" AND artist:"{artist}"',
                                               "fmt": "json", "limit": 5})
        for r in (j or {}).get("recordings") or []:
            if int(r.get("score", 0)) < 90 or fuzz.ratio(norm(r.get("title", "")), norm(title)) < 88:
                continue
            ac = (r.get("artist-credit") or [{}])[0].get("artist") or {}
            return {"mbid": r["id"], "title": r["title"], "duration_ms": r.get("length"),
                    "artist": ac.get("name") or artist, "artist_mbid": ac.get("id")}
        return None

    async def recording(self, mbid: str) -> dict | None:
        j = await self.get_json(f"/recording/{mbid}", {"inc": "artist-credits+releases", "fmt": "json"})
        if not j:
            return None
        ac = (j.get("artist-credit") or [{}])[0].get("artist") or {}
        rel = (j.get("releases") or [{}])[0]
        return {"mbid": j["id"], "title": j.get("title"), "duration_ms": j.get("length"),
                "artist": ac.get("name"), "artist_mbid": ac.get("id"), "album": rel.get("title"),
                "release_mbid": rel.get("id")}

    async def artist_tags(self, mbid: str) -> dict[str, int]:
        j = await self.get_json(f"/artist/{mbid}", {"inc": "tags+genres", "fmt": "json"})
        tags: dict[str, int] = {}
        for t in ((j or {}).get("genres") or []) + ((j or {}).get("tags") or []):
            tags[t["name"].lower()] = max(tags.get(t["name"].lower(), 0), int(t.get("count", 1)))
        return tags
