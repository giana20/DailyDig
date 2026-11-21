"""Taste ingestion (ListenBrainz history, Navidrome stars) and the taste profile."""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

from .db import now
from .models import norm, track_key

log = logging.getLogger("dailydig.taste")
MAX_PAGES = 20


@dataclass
class Profile:
    user: str
    seeds: list[dict] = field(default_factory=list)          # {"mbid","name","weight"}
    artist_plays: dict[str, int] = field(default_factory=dict)  # by mbid and norm(name)
    played: dict[str, float] = field(default_factory=dict)   # track_key/name_key -> last played ts
    blocked_artists: set[str] = field(default_factory=set)
    blocked_keys: set[str] = field(default_factory=set)
    artist_neg: dict[str, float] = field(default_factory=dict)  # artist -> negative-outcome rate
    tags: dict[str, float] = field(default_factory=dict)
    recent_tracks: list[dict] = field(default_factory=list)
    total_plays: int = 0

    def plays_for(self, artist: str, mbid: str | None) -> int:
        return max(self.artist_plays.get(mbid or "", 0), self.artist_plays.get(norm(artist), 0))

    def was_played_within(self, key: str, artist: str, title: str, days: float | None) -> bool:
        ts = max(self.played.get(key, 0), self.played.get(f"n:{norm(artist)}|{norm(title)}", 0))
        if not ts:
            return False
        return True if days is None else ts >= now() - days * 86400


def parse_listen(user: str, l: dict) -> dict | None:
    tm = l.get("track_metadata") or {}
    ai, mm = tm.get("additional_info") or {}, tm.get("mbid_mapping") or {}
    artist, title = tm.get("artist_name"), tm.get("track_name")
    if not (artist and title and l.get("listened_at")):
        return None
    rec = ai.get("recording_mbid") or mm.get("recording_mbid")
    ams = ai.get("artist_mbids") or mm.get("artist_mbids") or []
    dur = ai.get("duration_ms") or (int(ai["duration"]) * 1000 if ai.get("duration") else None)
    return {"user": user, "track_key": track_key(rec, artist, title), "recording_mbid": rec, "title": title,
            "artist": artist, "artist_mbid": ams[0] if ams else None, "played_at": int(l["listened_at"]),
            "duration_ms": dur, "source": ai.get("submission_client") or "lb"}


def store_events(ctx, events: list[dict]) -> int:
    n = 0
    for e in events:
        n += ctx.db.execute(
            "INSERT OR IGNORE INTO play_events(user,track_key,recording_mbid,title,artist,artist_mbid,played_at,"
            "duration_ms,listened_ms,source,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,'completed')",
            (e["user"], e["track_key"], e["recording_mbid"], e["title"], e["artist"], e["artist_mbid"],
             e["played_at"], e["duration_ms"], e["duration_ms"], e["source"]))
    return n


async def sync_listens(ctx) -> int:
    """Incremental pull of ListenBrainz listens, newest page first, stopping at the last seen timestamp."""
    lb, user = ctx.lb, ctx.settings.lb_user
    if not user or not lb.enabled:
        return 0
    last = int(ctx.db.kv_get("lb_last_ts", 0))
    cursor, added, newest = None, 0, last
    for _ in range(MAX_PAGES):
        batch = await lb.listens(user, min_ts=last or None, max_ts=cursor, count=100)
        if not batch:
            break
        evs = [e for e in (parse_listen(user, l) for l in batch) if e]
        added += store_events(ctx, evs)
        ts = [int(l["listened_at"]) for l in batch if l.get("listened_at")]
        newest = max(newest, max(ts, default=0))
        oldest = min(ts, default=0)
        if len(batch) < 100 or (last and oldest <= last):
            break
        cursor = oldest
    if newest:
        ctx.db.kv_set("lb_last_ts", newest)
    return added


async def resolve_artist_mbid(ctx, name: str) -> str | None:
    async def f():
        r = await ctx.mb.search_artist(name)
        return r["mbid"] if r else None
    return await ctx.cache.get_or_fetch(f"mb:artist:{norm(name)}", f, 30 * 86400, miss_ttl=12 * 3600)


async def sync_navidrome_stars(ctx) -> list[dict]:
    nd = ctx.navidrome
    if not (nd.configured and nd.enabled):
        return []
    out = []
    for s in await nd.starred():
        key = track_key(s.get("musicBrainzId"), s.get("artist", ""), s.get("title", ""))
        if ctx.db.one("SELECT 1 FROM feedback WHERE user=? AND target_type='track' AND target_id=? AND action='star'",
                      (ctx.settings.lb_user or "me", key)):
            continue
        ctx.db.execute(
            "INSERT INTO feedback(user,target_type,target_id,action,context_json,created_at) VALUES(?,?,?,?,?,?)",
            (ctx.settings.lb_user or "me", "track", key, "star", "{}", now()))
        out.append(s)
    return out


async def load_profile(ctx, *, max_seeds: int = 30) -> Profile:
    user = ctx.settings.lb_user or "me"
    p = Profile(user=user)
    t = now()
    rows = ctx.db.query(
        "SELECT track_key, title, artist, artist_mbid, played_at FROM play_events WHERE user=? "
        "ORDER BY played_at DESC LIMIT 20000", (user,))
    p.total_plays = len(rows)
    weights: dict[str, dict] = {}
    for r in rows:
        age_d = max(0.0, (t - r["played_at"]) / 86400)
        for k in (r["artist_mbid"], norm(r["artist"])):
            if k:
                p.artist_plays[k] = p.artist_plays.get(k, 0) + 1
        p.played[r["track_key"]] = max(p.played.get(r["track_key"], 0), r["played_at"])
        nk = f"n:{norm(r['artist'])}|{norm(r['title'])}"
        p.played[nk] = max(p.played.get(nk, 0), r["played_at"])
        if age_d <= 120:
            ak = r["artist_mbid"] or norm(r["artist"])
            w = weights.setdefault(ak, {"mbid": r["artist_mbid"], "name": r["artist"], "weight": 0.0})
            w["weight"] += math.exp(-age_d / 30)
    p.recent_tracks = [{"title": r["title"], "artist": r["artist"]} for r in rows[:10]]
    seeds = sorted(weights.values(), key=lambda w: -w["weight"])[:max_seeds]

    if not seeds and ctx.lb.enabled and ctx.settings.lb_user:  # cold start: use LB's own stats
        try:
            for i, a in enumerate(await ctx.lb.stats_artists(range_="all_time", count=max_seeds)):
                seeds.append({"mbid": (a.get("artist_mbids") or [None])[0] or a.get("artist_mbid"),
                              "name": a.get("artist_name"), "weight": 1.0 / (i + 1)})
        except Exception as e:  # noqa: BLE001 - taste must never crash a build
            log.warning("LB stats fallback failed: %s", e)
    for s in seeds:  # make sure every seed has an MBID where possible
        if not s.get("mbid") and s.get("name") and ctx.mb.enabled:
            try:
                s["mbid"] = await resolve_artist_mbid(ctx, s["name"])
            except Exception:  # noqa: BLE001
                pass
    p.seeds = [s for s in seeds if s.get("name")]

    for f in ctx.db.query(
            "SELECT target_type, target_id, action FROM feedback WHERE user=? AND action IN ('block','dislike')"
            " AND (expires_at IS NULL OR expires_at>?)", (user, t)):
        if f["target_type"] == "artist":
            p.blocked_artists.add(f["target_id"])
        else:
            p.blocked_keys.add(f["target_id"])
    for r in ctx.db.query(
            "SELECT artist, AVG(CASE WHEN label=0 THEN 1.0 ELSE 0.0 END) neg, COUNT(*) n FROM playlist_tracks "
            "WHERE label IS NOT NULL GROUP BY artist HAVING n>=2"):
        p.artist_neg[norm(r["artist"])] = r["neg"]
    return p


def music_roots(ctx) -> list[str]:
    return [d for d in (ctx.settings.music_dir,) if d and os.path.isdir(d)]
