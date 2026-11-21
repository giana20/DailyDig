"""Playlist engine: definitions (the matrix), candidate -> filter -> score -> assemble -> persist."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime

from croniter import croniter

from . import bandit
from .db import now
from .generators import run_generators
from .learning import load_ranker
from .models import norm
from .ranker import Scored, apply_final, features_json, merge, tier_a
from .taste import Profile, load_profile

log = logging.getLogger("dailydig.playlists")
PERMANENT = 9e12

DEFAULTS: list[dict] = [
    {"kind": "daily_dig", "name": "Daily Dig", "cron": "0 5 * * *", "config": {
        "size": 25, "generators": ["lb_radio", "lb_jams", "ytm_radio", "deezer_2hop", "taste_twins"],
        "mode": "balanced", "exploration": 0.2, "cooldown_days": 14, "pop_begin": 0, "pop_end": 60,
        "max_artist_plays": 40, "include_library": False, "download": True, "jam_kinds": ["daily-jams", "weekly-jams"]}},
    {"kind": "discover_weekly", "name": "Discover Weekly", "cron": "0 4 * * 1", "config": {
        "size": 30, "generators": ["lb_jams", "lb_radio", "deezer_2hop", "taste_twins", "tag_vector"],
        "mode": "deeper", "exploration": 0.25, "cooldown_days": None, "pop_begin": 0, "pop_end": 45,
        "max_artist_plays": 8, "include_library": False, "download": True, "jam_kinds": ["weekly-exploration"]}},
    {"kind": "release_radar", "name": "Release Radar", "cron": "0 6 * * 5", "config": {
        "size": 30, "generators": ["fresh"], "mode": "balanced", "exploration": 0.0, "cooldown_days": None,
        "include_library": False, "download": True, "fresh_days": 30, "max_artist_plays": None}},
    {"kind": "on_repeat", "name": "On Repeat", "cron": "30 5 * * *", "config": {
        "size": 50, "source": "history", "days": 30, "download": False}},
]


def next_run(cron: str, base: float | None = None) -> float:
    return croniter(cron, datetime.fromtimestamp(base or now())).get_next(float)


def seed_playlists(ctx) -> None:
    for d in DEFAULTS:
        ctx.db.execute(
            "INSERT OR IGNORE INTO playlists(kind,name,config_json,schedule_cron,enabled,next_run) VALUES(?,?,?,?,?,?)",
            (d["kind"], d["name"], json.dumps(d["config"]), d["cron"], 1 if d["kind"] != "on_repeat" else 0,
             next_run(d["cron"])))


# --------------------------------------------------------------------------------------
def filter_candidates(ctx, scored: list[Scored], profile: Profile, cfg: dict) -> list[Scored]:
    cooldown = cfg.get("cooldown_days", 14)
    max_plays = cfg.get("max_artist_plays")
    t = now()
    ledger = {r["track_key"]: r["cooldown_until"] for r in ctx.db.query(
        "SELECT track_key, cooldown_until FROM seen_ledger WHERE user=? AND cooldown_until>?", (profile.user, t))}
    out = []
    for s in scored:
        c = s.cand
        if not c.title or not c.artist:
            continue
        if norm(c.artist) in profile.blocked_artists or (c.artist_mbid and c.artist_mbid in profile.blocked_artists):
            continue
        if c.key in profile.blocked_keys or f"n:{norm(c.artist)}|{norm(c.title)}" in profile.blocked_keys:
            continue
        if ledger.get(c.key):
            continue
        if profile.was_played_within(c.key, c.artist, c.title, cooldown if cooldown is not None else None):
            continue
        if max_plays is not None and profile.plays_for(c.artist, c.artist_mbid) > max_plays:
            continue
        out.append(s)
    return out


async def drop_library_tracks(ctx, scored: list[Scored], limit: int) -> list[Scored]:
    """Remove tracks Navidrome already has (checked for the top `limit` only, to stay polite)."""
    nd = ctx.navidrome
    if not (nd.configured and nd.enabled):
        return scored
    sem = asyncio.Semaphore(4)
    head, tail = scored[:limit], scored[limit:]

    async def check(s: Scored) -> bool:
        async with sem:
            key = f"nd:has:{norm(s.cand.artist)}|{norm(s.cand.title)}"

            async def f():
                return bool(await nd.find_song(s.cand.artist, s.cand.title))
            try:
                return bool(await ctx.cache.get_or_fetch(key, f, 12 * 3600))
            except Exception:  # noqa: BLE001 - if Navidrome is down, don't block the build
                return False
    have = await asyncio.gather(*[check(s) for s in head])
    return [s for s, h in zip(head, have) if not h] + tail


def _mmr_pick(pool: list[Scored], chosen: list[Scored], one_per_artist: bool) -> Scored | None:
    best, best_v = None, -1e9
    artists = {norm(c.cand.artist) for c in chosen}
    for s in pool:
        if one_per_artist and norm(s.cand.artist) in artists:
            continue
        shared_seed = sum(len(s.seeds & c.seeds) > 0 for c in chosen)
        shared_tag = sum(len(s.cand.tags & c.cand.tags) > 0 for c in chosen)
        v = s.final - 2.6 * shared_seed - 1.8 * shared_tag
        if v > best_v:
            best, best_v = s, v
    return best


def assemble(scored: list[Scored], size: int, quotas: dict[str, int], *, one_per_artist: bool = True) -> list[tuple[Scored, str]]:
    """Fill per-generator quotas, backfill any shortfall by score, MMR-diversified. Returns (track, arm)."""
    pool = sorted(scored, key=lambda s: -s.final)
    chosen: list[tuple[Scored, str]] = []
    picked: list[Scored] = []

    def take(gen: str | None) -> bool:
        cand_pool = [s for s in pool if gen is None or gen in s.generators]
        s = _mmr_pick(cand_pool, picked, one_per_artist)
        if not s:
            return False
        pool.remove(s)
        picked.append(s)
        from .generators import PRIOR
        arm = gen or max(s.generators, key=lambda g: PRIOR.get(g, 0.0))
        chosen.append((s, arm))
        return True

    for gen, q in sorted(quotas.items(), key=lambda x: -x[1]):
        for _ in range(q):
            if len(chosen) >= size or not take(gen):
                break
    while len(chosen) < size and take(None):
        pass
    return chosen


# --------------------------------------------------------------------------------------
async def build_playlist(ctx, playlist_id: int, *, rng: random.Random | None = None) -> dict:
    row = ctx.db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    if not row:
        raise ValueError(f"playlist {playlist_id} not found")
    cfg = json.loads(row["config_json"] or "{}")
    rng = rng or random.Random()
    size = int(cfg.get("size", 25))
    profile = await load_profile(ctx)
    summary: dict = {"playlist": row["name"], "size": size}

    if cfg.get("source") == "history":
        picks = _history_picks(ctx, profile, cfg)
        run_id = _persist(ctx, row, cfg, picks, history=True)
        summary.update(run_id=run_id, count=len(picks), tier="history")
        _finish(ctx, row)
        ctx.queue.enqueue("deliver_playlist", {"playlist_id": playlist_id}, dedupe_key=f"deliver:{playlist_id}")
        return summary

    names = cfg.get("generators", [])
    cands, info = await run_generators(ctx, profile, names, cfg, rng)
    scored = merge(cands)
    tier_a(scored, profile, cfg.get("mode", "balanced"))
    ranker = load_ranker(ctx) if cfg.get("use_ml", True) else None
    ml_used = bool(ranker and ranker.predict(scored))
    apply_final(scored)
    scored = filter_candidates(ctx, scored, profile, cfg)
    scored.sort(key=lambda s: -s.final)
    if not cfg.get("include_library", False):
        scored = await drop_library_tracks(ctx, scored, size * 4)
    # arms = generators that actually produced something
    live = [g for g in names if any(g in s.generators for s in scored)]
    quotas = bandit.allocate(ctx.db, live, size, cfg.get("exploration", 0.2), rng) if live else {}
    picks = assemble(scored, size, quotas, one_per_artist=cfg.get("one_per_artist", True))
    run_id = _persist(ctx, row, cfg, picks)
    _store_scores(ctx, profile.user, scored[:200], ml_used)
    summary.update(run_id=run_id, count=len(picks), generators=info, quotas=quotas, tier="B" if ml_used else "A",
                   candidates=len(scored))
    _finish(ctx, row)
    if picks:
        _enqueue_followups(ctx, row, cfg, run_id)
    return summary


def _history_picks(ctx, profile: Profile, cfg: dict):
    since = int(now() - cfg.get("days", 30) * 86400)
    rows = ctx.db.query(
        "SELECT track_key, title, artist, artist_mbid, recording_mbid, MAX(duration_ms) dur, COUNT(*) n FROM play_events "
        "WHERE user=? AND played_at>=? GROUP BY track_key ORDER BY n DESC, MAX(played_at) DESC LIMIT ?",
        (profile.user, since, int(cfg.get("size", 50))))
    out = []
    for r in rows:
        from .models import Candidate
        c = Candidate(title=r["title"], artist=r["artist"], generator="history", recording_mbid=r["recording_mbid"],
                      artist_mbid=r["artist_mbid"], duration_ms=r["dur"], reason=f"Played {r['n']}× in {cfg.get('days', 30)} d")
        s = Scored(cand=c, generators={"history"})
        s.final = float(r["n"])
        out.append((s, "history"))
    return out


def _persist(ctx, row, cfg: dict, picks: list[tuple[Scored, str]], history: bool = False) -> str:
    run_id, t = uuid.uuid4().hex[:8], now()
    ctx.db.execute("UPDATE playlist_tracks SET status='retired' WHERE playlist_id=? AND status NOT IN ('promoted','retired')",
                   (row["id"],))
    cooldown = cfg.get("cooldown_days", 14)
    until = PERMANENT if cooldown is None else t + cooldown * 86400
    user = ctx.settings.lb_user or "me"
    status = "library" if history else "pending"
    for pos, (s, arm) in enumerate(picks):
        c = s.cand
        ctx.db.insert(
            "INSERT INTO playlist_tracks(playlist_id,run_id,track_key,position,title,artist,artist_mbid,recording_mbid,"
            "duration_ms,generator,reason,score,status,video_id,added_at,in_library,features_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], run_id, c.key, pos, c.title, c.artist, c.artist_mbid, c.recording_mbid, c.duration_ms, arm,
             s.reason, round(s.final, 2), status, s.video_id, t, 1 if history else 0,
             None if history else features_json(s)))
        if not history:
            ctx.db.execute(
                "INSERT INTO seen_ledger(track_key,user,first_offered,last_offered,times_offered,cooldown_until) "
                "VALUES(?,?,?,?,1,?) ON CONFLICT(track_key,user) DO UPDATE SET last_offered=excluded.last_offered,"
                " times_offered=times_offered+1, cooldown_until=MAX(cooldown_until, excluded.cooldown_until)",
                (c.key, user, t, t, until))
    return run_id


def _store_scores(ctx, user: str, scored: list[Scored], ml_used: bool) -> None:
    t = now()
    ctx.db.executemany(
        "INSERT INTO scores(track_key,user,heuristic,ml,final,reason_codes,tier,scored_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(track_key,user) DO UPDATE SET heuristic=excluded.heuristic, ml=excluded.ml, final=excluded.final,"
        " reason_codes=excluded.reason_codes, tier=excluded.tier, scored_at=excluded.scored_at",
        [(s.key, user, s.heuristic, s.ml, s.final, json.dumps(sorted(s.generators)), "B" if ml_used else "A", t)
         for s in scored])


def _finish(ctx, row) -> None:
    ctx.db.execute("UPDATE playlists SET last_run=?, next_run=? WHERE id=?",
                   (now(), next_run(row["schedule_cron"]), row["id"]))


def _enqueue_followups(ctx, row, cfg: dict, run_id: str) -> None:
    pid = row["id"]
    if cfg.get("download", True) and ctx.settings.auto_download:
        for r in ctx.db.query("SELECT id FROM playlist_tracks WHERE playlist_id=? AND run_id=? AND status='pending'", (pid, run_id)):
            ctx.queue.enqueue("download_track", {"id": r["id"]}, dedupe_key=f"dl:{r['id']}", max_attempts=2)
    ctx.queue.enqueue("deliver_playlist", {"playlist_id": pid, "waits": 0}, run_at=now() + 30, dedupe_key=f"deliver:{pid}")


def current_tracks(ctx, kind: str) -> list[dict]:
    pl = ctx.db.one("SELECT id FROM playlists WHERE kind=?", (kind,))
    if not pl:
        return []
    rows = ctx.db.query(
        "SELECT id,position,title,artist,reason,score,status,generator,file_path,error FROM playlist_tracks "
        "WHERE playlist_id=? AND status!='retired' ORDER BY position", (pl["id"],))
    return [dict(r) for r in rows]
