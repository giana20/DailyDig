"""ListenBrainz adapter: the recommendation brain (free, server-side CF).

Response shapes are parsed defensively (`.get` everywhere) because LB evolves its
labs/radio endpoints; unknown shapes degrade to empty results, never exceptions.
"""
from __future__ import annotations

from typing import Any

from ..models import mbid_from_url
from .base import Provider


class ListenBrainz(Provider):
    name = "listenbrainz"
    min_interval = 0.25

    def __init__(self, ctx):
        super().__init__(ctx)
        self.base_url = ctx.settings.lb_url.rstrip("/")

    @property
    def user(self) -> str:
        return self.ctx.settings.lb_user

    def headers(self) -> dict[str, str]:
        t = self.ctx.settings.lb_token
        return {"Authorization": f"Token {t}"} if t else {}

    def _after_response(self, r) -> None:
        # obey x-ratelimit-* instead of guessing
        try:
            rem = int(r.headers.get("x-ratelimit-remaining", "99"))
            reset = float(r.headers.get("x-ratelimit-reset-in", "0"))
            if rem <= 1 and reset and self.limiter:
                self.limiter.pause(reset)
        except ValueError:
            pass

    # -- history / stats ----------------------------------------------------
    async def listens(self, user: str | None = None, min_ts: int | None = None, max_ts: int | None = None,
                      count: int = 100) -> list[dict]:
        j = await self.get_json(f"/1/user/{user or self.user}/listens",
                                {"min_ts": min_ts, "max_ts": max_ts, "count": count})
        return ((j or {}).get("payload") or {}).get("listens") or []

    async def stats_artists(self, user: str | None = None, range_: str = "this_month", count: int = 50) -> list[dict]:
        j = await self.get_json(f"/1/stats/user/{user or self.user}/artists", {"range": range_, "count": count})
        return ((j or {}).get("payload") or {}).get("artists") or []

    async def stats_recordings(self, user: str | None = None, range_: str = "this_month", count: int = 50) -> list[dict]:
        j = await self.get_json(f"/1/stats/user/{user or self.user}/recordings", {"range": range_, "count": count})
        return ((j or {}).get("payload") or {}).get("recordings") or []

    # -- radio -----------------------------------------------------------------
    @staticmethod
    def _flatten_radio(j: Any) -> list[dict]:
        out: list[dict] = []
        if isinstance(j, dict):
            for v in j.values():
                if isinstance(v, list):
                    out += [x for x in v if isinstance(x, dict)]
        elif isinstance(j, list):
            out = [x for x in j if isinstance(x, dict)]
        return [x for x in out if x.get("recording_mbid")]

    async def radio_artist(self, artist_mbid: str, mode: str = "medium", pop_begin: int = 0, pop_end: int = 100,
                           max_recordings_per_artist: int = 3, max_similar_artists: int = 20) -> list[dict]:
        j = await self.get_json(f"/1/lb-radio/artist/{artist_mbid}", {
            "mode": mode, "pop_begin": pop_begin, "pop_end": pop_end,
            "max_recordings_per_artist": max_recordings_per_artist, "max_similar_artists": max_similar_artists})
        return self._flatten_radio(j)

    async def radio_tags(self, tags: list[str], operator: str = "OR", pop_begin: int = 0, pop_end: int = 60,
                         count: int = 30) -> list[dict]:
        j = await self.get_json("/1/lb-radio/tags", {
            "tag": tags, "operator": operator, "pop_begin": pop_begin, "pop_end": pop_end, "count": count})
        return self._flatten_radio(j)

    # -- server-side playlists ----------------------------------------------------
    async def created_for(self, user: str | None = None) -> list[dict]:
        j = await self.get_json(f"/1/user/{user or self.user}/playlists/createdfor", {"count": 25})
        out = []
        for p in (j or {}).get("playlists") or []:
            pl = p.get("playlist") or {}
            meta = (((pl.get("extension") or {}).get("https://musicbrainz.org/doc/jspf#playlist") or {})
                    .get("additional_metadata") or {}).get("algorithm_metadata") or {}
            title = (pl.get("title") or "")
            kind = meta.get("source_patch") or _kind_from_title(title)
            out.append({"mbid": (pl.get("identifier") or "").rstrip("/").split("/")[-1], "title": title,
                        "kind": kind, "date": pl.get("date")})
        return out

    async def playlist(self, mbid: str) -> list[dict]:
        j = await self.get_json(f"/1/playlist/{mbid}")
        tracks = ((j or {}).get("playlist") or {}).get("track") or []
        out = []
        for t in tracks:
            ident = t.get("identifier")
            ident = ident[0] if isinstance(ident, list) and ident else ident
            ext = (t.get("extension") or {}).get("https://musicbrainz.org/doc/jspf#track") or {}
            arts = ext.get("artist_identifiers") or []
            out.append({"recording_mbid": mbid_from_url(ident), "title": t.get("title"), "artist": t.get("creator"),
                        "album": t.get("album"), "duration_ms": t.get("duration"),
                        "artist_mbid": mbid_from_url(arts[0]) if arts else None})
        return [x for x in out if x["title"] and x["artist"]]

    # -- discovery bits ---------------------------------------------------------------
    async def fresh_releases(self, user: str | None = None, days: int = 30, future: bool = False) -> list[dict]:
        j = await self.get_json(f"/1/user/{user or self.user}/fresh_releases",
                                {"days": days, "future": str(future).lower(), "sort": "release_date"})
        return ((j or {}).get("payload") or {}).get("releases") or []

    async def similar_users(self, user: str | None = None) -> list[dict]:
        j = await self.get_json(f"/1/user/{user or self.user}/similar-users")
        return (j or {}).get("payload") or []

    # -- metadata (recording MBID -> title/artist/length) ---------------------------------
    async def recording_metadata(self, mbids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(mbids), 40):
            chunk = mbids[i:i + 40]
            j = await self.get_json("/1/metadata/recording/",
                                    {"recording_mbids": ",".join(chunk), "inc": "artist release"})
            for mbid, m in (j or {}).items():
                if not isinstance(m, dict):
                    continue
                rec, art, rel = m.get("recording") or {}, m.get("artist") or {}, m.get("release") or {}
                arts = art.get("artists") or []
                if rec.get("name") and art.get("name"):
                    out[mbid] = {"title": rec["name"], "artist": art["name"], "duration_ms": rec.get("length"),
                                 "artist_mbid": (arts[0].get("artist_mbid") if arts else None),
                                 "album": rel.get("name")}
        return out

    # -- feedback --------------------------------------------------------------------------
    async def submit_feedback(self, recording_mbid: str, score: int) -> bool:
        if not self.ctx.settings.lb_token:
            return False
        r = await self.ctx.http.post(f"{self.base_url}/1/feedback/recording-feedback",
                                     json={"recording_mbid": recording_mbid, "score": score},
                                     headers=self.headers(), timeout=20)
        return r.status_code == 200

    async def submit_listens(self, body: dict, token: str | None = None) -> bool:
        tok = token or self.ctx.settings.lb_token
        if not tok:
            return False
        r = await self.ctx.http.post(f"{self.base_url}/1/submit-listens", json=body,
                                     headers={"Authorization": f"Token {tok}"}, timeout=30)
        return r.status_code == 200


def _kind_from_title(title: str) -> str:
    t = title.lower()
    for k in ("daily jams", "weekly jams", "weekly exploration"):
        if k in t:
            return k.replace(" ", "-")
    return "other"
