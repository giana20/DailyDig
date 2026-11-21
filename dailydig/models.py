"""Shared small helpers and the Candidate model."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_MB_RE = re.compile(r"musicbrainz\.org/(?:recording|artist|release|release-group)/([0-9a-f\-]{36})", re.I)
_DIAC = {"ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l", "þ": "th", "ß": "ss"}


def fold(s: str) -> str:
    s = (s or "").lower()
    s = "".join(_DIAC.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm(s: str) -> str:
    s = fold(s)
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # drop (feat. x), [Remastered]
    s = re.sub(r"\b(feat|ft|featuring)\.?\s.*$", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def track_key(recording_mbid: str | None, artist: str, title: str) -> str:
    return recording_mbid or f"n:{norm(artist)}|{norm(title)}"


def mbid_from_url(u: str | None) -> str | None:
    m = _MB_RE.search(u or "")
    return m.group(1).lower() if m else None


@dataclass
class Candidate:
    title: str
    artist: str
    generator: str
    recording_mbid: str | None = None
    artist_mbid: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    raw_score: float = 0.0          # generator-native score, 0..1 after normalisation
    popularity: float | None = None  # percentile 0..100 when known (lower = more obscure)
    seed: str | None = None
    seeds: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    freshness: float = 0.0
    reason: str = ""
    deezer_id: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return track_key(self.recording_mbid, self.artist, self.title)
