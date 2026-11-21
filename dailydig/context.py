"""Application context: one place that wires settings, DB, queue, cache and providers."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .cache import Cache
from .config import Settings
from .db import DB
from .jobs import JobQueue


@dataclass
class Context:
    settings: Settings
    db: DB
    queue: JobQueue
    cache: Cache
    http: httpx.AsyncClient
    providers: dict = field(default_factory=dict)

    @classmethod
    def create(cls, settings: Settings) -> "Context":
        db = DB(settings.db_path)
        db.init()
        http = httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, follow_redirects=True)
        ctx = cls(settings, db, JobQueue(db), Cache(db), http)
        from .navidrome import Navidrome
        from .providers.deezer import Deezer
        from .providers.listenbrainz import ListenBrainz
        from .providers.musicbrainz import MusicBrainz
        from .providers.ytm import YTM
        for cls_ in (ListenBrainz, Deezer, MusicBrainz, YTM, Navidrome):
            p = cls_(ctx)
            ctx.providers[p.name] = p
        # persisted provider toggles
        disabled = set(db.kv_get("providers_disabled", []))
        for n, p in ctx.providers.items():
            p.enabled = n not in disabled
        return ctx

    def __getitem__(self, name: str):
        return self.providers[name]

    @property
    def lb(self):
        return self.providers["listenbrainz"]

    @property
    def deezer(self):
        return self.providers["deezer"]

    @property
    def mb(self):
        return self.providers["musicbrainz"]

    @property
    def ytm(self):
        return self.providers["ytm"]

    @property
    def navidrome(self):
        return self.providers["navidrome"]

    async def close(self) -> None:
        await self.http.aclose()
