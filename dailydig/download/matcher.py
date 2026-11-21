"""Rule-based match verification (Aurral-inspired): accept | verify | review | reject.

Hard rules beat fuzzy scores: a strong title can never override a duration / version /
artist contradiction, and missing evidence is never treated as agreement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..models import fold, norm

EXACT_WINDOW_S = 10.0     # title+artist match exactly: mastering / fade variance
PARTIAL_WINDOW_S = 2.5    # partial title match: must be tight
DIFFERENT_RECORDING_S = 30.0
FLOOR_LOSSLESS_KBPS = 48.0
FLOOR_LOSSY_KBPS = 12.0

_STRIP = re.compile(r"\s*[\(\[]\s*(official\s*(music\s*)?(video|audio|lyric\s*video|visuali[sz]er)|lyrics?|"
                    r"audio|visuali[sz]er|hd|hq|4k|explicit|clean)\s*[\)\]]", re.I)
_VARIANTS = {
    "live": r"\blive\b", "remix": r"\b(re)?mix\b(?!ed)", "sped": r"\b(sped\s*up|speed\s*up|nightcore)\b",
    "slowed": r"\b(slowed|reverb)\b", "8d": r"\b8d\b", "cover": r"\bcover\b", "karaoke": r"\bkaraoke\b",
    "instrumental": r"\binstrumental\b", "acoustic": r"\bacoustic\b", "demo": r"\bdemo\b",
    "clean": r"\b(clean|censored|radio\s*edit)\b",
}
_NON_LATIN = re.compile(r"[^\u0000-ɏḀ-ỿ\s\W\d_]", re.U)


@dataclass
class Verdict:
    level: str                  # accept | verify | review | reject
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    video_id: str | None = None
    duration_s: float | None = None


def clean_title(t: str) -> str:
    return _STRIP.sub("", t or "").strip()


def title_sim(a: str, b: str) -> float:
    na, nb = norm(clean_title(a)), norm(clean_title(b))
    if not na or not nb:
        return 0.0
    return max(fuzz.ratio(na, nb), 0.95 * fuzz.token_sort_ratio(na, nb))


def artist_sim(wanted: str, credited: list[str]) -> float:
    w = norm(wanted)
    best = 0.0
    for c in credited + [" ".join(credited)]:
        n = norm(c)
        best = max(best, fuzz.ratio(w, n), 95.0 if (w and (w in n.split() or f" {w} " in f" {n} ")) else 0.0)
    return best


def variants(title: str) -> set[str]:
    t = fold(title)
    return {k for k, rx in _VARIANTS.items() if re.search(rx, t)}


def verify(req_title: str, req_artist: str, req_duration_s: float | None, *, cand_title: str,
           cand_artists: list[str], cand_duration_s: float | None, video_id: str | None = None) -> Verdict:
    v = Verdict("reject", video_id=video_id, duration_s=cand_duration_s)
    ts, as_ = title_sim(req_title, cand_title), artist_sim(req_artist, cand_artists)
    v.score = round(0.6 * ts + 0.4 * as_, 1)

    bad = variants(cand_title) - variants(req_title)
    if "clean" in bad and "clean" not in variants(req_title):
        bad.add("clean")
    if bad:
        v.reasons.append(f"version mismatch: {', '.join(sorted(bad))}")
        return v
    if ts < 50:
        v.reasons.append(f"title too different ({ts:.0f})")
        return v
    if as_ < 60:
        v.reasons.append(f"artist too different ({as_:.0f})")
        return v
    # non-Latin: two different normalised titles = contradiction, needs a human
    if (_NON_LATIN.search(req_title) or _NON_LATIN.search(cand_title)) and norm(req_title) != norm(cand_title):
        v.level, v.reasons = "review", ["non-Latin title differs; transliteration needs a human"]
        return v

    exact = ts >= 95 and as_ >= 90
    if req_duration_s and cand_duration_s:
        diff = abs(req_duration_s - cand_duration_s)
        if diff > DIFFERENT_RECORDING_S:
            v.reasons.append(f"duration off by {diff:.0f}s: different recording")
            return v
        window = EXACT_WINDOW_S if exact else PARTIAL_WINDOW_S
        if diff > window:
            v.level, v.reasons = "verify", [f"duration off by {diff:.0f}s (window {window:.0f}s)"]
            return v
        if ts >= 80 and as_ >= 80:
            v.level = "accept"
            v.reasons.append(f"title {ts:.0f}, artist {as_:.0f}, duration Δ{diff:.1f}s")
            return v
    elif ts >= 90 and as_ >= 90:   # no duration evidence: only very strong text matches pass
        v.level, v.reasons = "accept", [f"title {ts:.0f}, artist {as_:.0f}, no duration evidence"]
        return v
    v.level = "verify"
    v.reasons.append(f"title {ts:.0f}, artist {as_:.0f}: not enough evidence")
    return v


def pick_best(req_title: str, req_artist: str, req_duration_s: float | None, cands: list[dict]) -> Verdict:
    """cands: ytm search results ({video_id,title,artists,duration_s}). Best accept, else best verify/review."""
    verdicts = [verify(req_title, req_artist, req_duration_s, cand_title=c["title"], cand_artists=c["artists"],
                       cand_duration_s=c.get("duration_s"), video_id=c["video_id"]) for c in cands]
    rank = {"accept": 3, "verify": 2, "review": 1, "reject": 0}
    verdicts.sort(key=lambda v: (rank[v.level], v.score), reverse=True)
    return verdicts[0] if verdicts else Verdict("reject", reasons=["no candidates"])


def passes_bitrate_floor(size_bytes: int, duration_s: float, lossless: bool) -> bool:
    """Anti-fake: generated digital silence at exact lengths has absurdly low bitrate."""
    if duration_s <= 0:
        return False
    kbps = size_bytes * 8 / duration_s / 1000.0
    return kbps >= (FLOOR_LOSSLESS_KBPS if lossless else FLOOR_LOSSY_KBPS)
