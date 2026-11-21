"""Candidate generators. Each returns list[Candidate]; failures are isolated per source."""
from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import date, datetime

from .models import Candidate, norm
from .providers.deezer import rank_to_pct
from .taste import Profile

log = logging.getLogger("dailydig.generators")

# prior trust per generator (feeds the Tier-A cf weight)
PRIOR = {"lb_radio": 1.0, "lb_jams": 0.9, "ytm_radio": 0.85, "fresh": 0.8, "deezer_2hop": 0.6,
         "taste_twins": 0.55, "tag_vector": 0.5}
NEEDS = {"lb_radio": ["listenbrainz"], "lb_jams": ["listenbrainz"], "taste_twins": ["listenbrainz"],
         "tag_vector": ["listenbrainz", "musicbrainz"], "ytm_radio": ["ytm"], "deezer_2hop": ["deezer"],
         "fresh": ["listenbrainz", "deezer"]}
JUNK_TAGS = {"seen live", "favorites", "favourites", "favorite", "american", "british", "canadian", "usa", "uk",
             "male vocalists", "female vocalists", "albums i own", "love", "under 2000 listeners"}


def _decay(i: int, k: float = 0.08) -> float:
    return 1.0 / (1.0 + k * i)


def _pop_from_listens(n: int | None) -> float | None:
    return None if n is None else max(0.0, min(100.0, math.log10(n + 1) / 7.0 * 100.0))


def sample_seeds(profile: Profile, n: int, rng: random.Random, need_mbid: bool = False) -> list[dict]:
    pool = [s for s in profile.seeds if s.get("mbid")] if need_mbid else list(profile.seeds)
    out: list[dict] = []
    while pool and len(out) < n:
        pick = rng.choices(pool, weights=[max(s["weight"], 1e-6) for s in pool], k=1)[0]
        out.append(pick)
        pool.remove(pick)
    return out


async def _metadata(ctx, mbids: list[str]) -> dict[str, dict]:
    """recording MBID -> {title, artist, ...}; cached per recording for 30 days."""
    out, missing = {}, []
    for m in dict.fromkeys(mbids):
        r = ctx.db.one("SELECT value, stale_until FROM kv_cache WHERE key=?", (f"meta:{m}",))
        if r:
            import json
            v = json.loads(r["value"])
            if isinstance(v, dict) and "title" in v:
                out[m] = v
                continue
        missing.append(m)
    if missing:
        try:
            got = await ctx.lb.recording_metadata(missing)
        except Exception as e:  # noqa: BLE001
            log.warning("recording metadata failed: %s", e)
            got = {}
        for m, v in got.items():
            ctx.cache.put(f"meta:{m}", v, 30 * 86400)
            out[m] = v
    return out


async def gen_lb_radio(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    lb = ctx.lb
    seeds = sample_seeds(profile, lb.fanout(params.get("seeds", 6)), rng, need_mbid=True)
    mode, pb, pe = params.get("lb_mode", "medium"), params.get("pop_begin", 0), params.get("pop_end", 60)

    async def one(seed):
        async def f():
            return await lb.radio_artist(seed["mbid"], mode, pb, pe, 3, 20)
        items = await ctx.cache.get_or_fetch(f"lb:radio:{seed['mbid']}:{mode}:{pb}:{pe}", f, 6 * 3600)
        return seed, items or []

    results = [r for r in await asyncio.gather(*[one(s) for s in seeds], return_exceptions=True)
               if not isinstance(r, Exception)]
    mbids = [it["recording_mbid"] for _, items in results for it in items]
    meta = await _metadata(ctx, mbids)
    out = []
    for seed, items in results:
        for i, it in enumerate(items):
            m = meta.get(it["recording_mbid"])
            if not m:
                continue
            out.append(Candidate(
                title=m["title"], artist=m["artist"], generator="lb_radio", recording_mbid=it["recording_mbid"],
                artist_mbid=m.get("artist_mbid") or it.get("similar_artist_mbid"), album=m.get("album"),
                duration_ms=m.get("duration_ms"), raw_score=_decay(i), seed=seed["name"], seeds={seed["name"]},
                popularity=_pop_from_listens(it.get("total_listen_count")),
                reason=f"LB radio · like {seed['name']}"))
    return out


async def gen_lb_jams(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    kinds = params.get("jam_kinds", ["daily-jams", "weekly-jams", "weekly-exploration"])
    pls = await ctx.cache.get_or_fetch("lb:createdfor", ctx.lb.created_for, 3600) or []
    out = []
    for kind in kinds:
        pl = next((p for p in pls if p["kind"] == kind and p["mbid"]), None)
        if not pl:
            continue
        mbid = pl["mbid"]

        async def f(mbid=mbid):
            return await ctx.lb.playlist(mbid)
        tracks = await ctx.cache.get_or_fetch(f"lb:pl:{mbid}", f, 24 * 3600) or []
        label = kind.replace("-", " ").title()
        for i, t in enumerate(tracks):
            out.append(Candidate(title=t["title"], artist=t["artist"], generator="lb_jams",
                                 recording_mbid=t.get("recording_mbid"), artist_mbid=t.get("artist_mbid"),
                                 album=t.get("album"), duration_ms=t.get("duration_ms"), raw_score=_decay(i, 0.03),
                                 seed=label, seeds={label}, reason=f"LB {label}"))
    return out


async def gen_ytm_radio(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    seeds = profile.recent_tracks[: ctx.ytm.fanout(params.get("ytm_seeds", 4))]
    out = []
    for s in seeds:
        q = f"{s['artist']} {s['title']}"

        async def find(q=q):
            r = await ctx.ytm.search_songs(q, limit=1)
            return r[0]["video_id"] if r else None
        vid = await ctx.cache.get_or_fetch(f"ytm:seed:{norm(q)}", find, 30 * 86400, miss_ttl=3600)
        if not vid:
            continue

        async def rad(vid=vid):
            return await ctx.ytm.radio(vid, 25)
        tracks = await ctx.cache.get_or_fetch(f"ytm:radio:{vid}", rad, 6 * 3600) or []
        for i, t in enumerate(tracks[1:]):  # [0] is the seed itself
            artist = (t["artists"] or [""])[0]
            if not artist:
                continue
            out.append(Candidate(title=t["title"], artist=artist, generator="ytm_radio",
                                 duration_ms=(t["duration_s"] or 0) * 1000 or None, raw_score=_decay(i),
                                 seed=s["artist"], seeds={s["artist"]}, reason=f"YTM radio · like {s['artist']}",
                                 extra={"video_id": t["video_id"]}))
    return out


async def gen_deezer_2hop(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    dz = ctx.deezer
    seeds = sample_seeds(profile, dz.fanout(params.get("seeds", 8)), rng)
    scores: dict[int, dict] = {}
    seed_ids = set()

    async def dz_artist(name):
        async def f():
            a = await dz.search_artist(name)
            return {"id": a["id"], "name": a["name"]} if a else None
        return await ctx.cache.get_or_fetch(f"dz:artist:{norm(name)}", f, 30 * 86400, miss_ttl=24 * 3600)

    async def dz_related(aid):
        async def f():
            return [{"id": r["id"], "name": r["name"]} for r in await dz.related(aid, 20)]
        return await ctx.cache.get_or_fetch(f"dz:related:{aid}", f, 7 * 86400) or []

    wmax = max((s["weight"] for s in seeds), default=1.0) or 1.0
    for s in seeds:
        da = await dz_artist(s["name"])
        if not da:
            continue
        seed_ids.add(da["id"])
        base = 0.4 + 0.6 * (s["weight"] / wmax)
        rel = await dz_related(da["id"])
        for i, r in enumerate(rel):
            e = scores.setdefault(r["id"], {"name": r["name"], "score": 0.0, "seeds": set()})
            e["score"] += base * _decay(i); e["seeds"].add(s["name"])
        for r in rel[:3]:  # depth-2 bridge artists, damped
            hop2 = (await dz_related(r["id"]))[:10]
            for j, r2 in enumerate(hop2):
                e = scores.setdefault(r2["id"], {"name": r2["name"], "score": 0.0, "seeds": set()})
                e["score"] += base * 0.55 * _decay(j); e["seeds"].add(s["name"])
    top = sorted(((aid, e) for aid, e in scores.items() if aid not in seed_ids), key=lambda x: -x[1]["score"])
    top = top[: params.get("artists", 40)]
    smax = top[0][1]["score"] if top else 1.0
    out = []

    async def tracks_of(aid):
        async def f():
            return [{k: t.get(k) for k in ("id", "title", "duration", "rank")} | {"album": (t.get("album") or {}).get("title")}
                    for t in await dz.top_tracks(aid, 5)]
        return await ctx.cache.get_or_fetch(f"dz:top:{aid}", f, 7 * 86400) or []

    for aid, e in top:
        for k, t in enumerate((await tracks_of(aid))[:2]):
            out.append(Candidate(
                title=t["title"], artist=e["name"], generator="deezer_2hop", album=t.get("album"),
                duration_ms=(t.get("duration") or 0) * 1000 or None, raw_score=(e["score"] / smax) * _decay(k, 0.3),
                popularity=rank_to_pct(t.get("rank")), seed=sorted(e["seeds"])[0], seeds=set(e["seeds"]),
                deezer_id=str(t["id"]), reason=f"Related to {sorted(e['seeds'])[0]} (Deezer)"))
    return out


async def gen_taste_twins(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    twins = await ctx.cache.get_or_fetch("lb:twins", ctx.lb.similar_users, 12 * 3600) or []
    twins = sorted(twins, key=lambda t: -float(t.get("similarity", 0)))[: ctx.lb.fanout(params.get("twins", 5))]
    out = []
    for rank, tw in enumerate(twins):
        name = tw.get("user_name")
        if not name:
            continue

        async def f(name=name):
            return await ctx.lb.stats_recordings(name, "this_month", 30)
        recs = await ctx.cache.get_or_fetch(f"lb:twinrec:{name}", f, 12 * 3600) or []
        sim = float(tw.get("similarity", 0.5))
        for i, r in enumerate(recs):
            if not (r.get("track_name") and r.get("artist_name")):
                continue
            out.append(Candidate(
                title=r["track_name"], artist=r["artist_name"], generator="taste_twins",
                recording_mbid=r.get("recording_mbid"), artist_mbid=(r.get("artist_mbids") or [None])[0],
                raw_score=min(1.0, sim * 1.5) * _decay(i, 0.1), seed=f"twin:{name}", seeds={f"twin:{name}"},
                reason=f"Taste twin {name} is into this"))
    return out


async def gen_tag_vector(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    tags = await profile_tags(ctx, profile)
    top = [t for t, _ in sorted(tags.items(), key=lambda x: -x[1])[:3]]
    if not top:
        return []
    pb, pe = params.get("pop_begin", 0), params.get("pop_end", 60)

    async def f():
        return await ctx.lb.radio_tags(top, "OR", pb, pe, 40)
    items = await ctx.cache.get_or_fetch(f"lb:tags:{'+'.join(top)}:{pb}:{pe}", f, 6 * 3600) or []
    meta = await _metadata(ctx, [i["recording_mbid"] for i in items])
    out = []
    for i, it in enumerate(items):
        m = meta.get(it["recording_mbid"])
        if m:
            out.append(Candidate(title=m["title"], artist=m["artist"], generator="tag_vector",
                                 recording_mbid=it["recording_mbid"], artist_mbid=m.get("artist_mbid"),
                                 duration_ms=m.get("duration_ms"), raw_score=_decay(i, 0.05), seed="/".join(top),
                                 seeds={"/".join(top)}, tags=set(top),
                                 popularity=_pop_from_listens(it.get("total_listen_count")),
                                 reason=f"Tag radio · {', '.join(top)}"))
    return out


async def profile_tags(ctx, profile: Profile) -> dict[str, float]:
    if profile.tags:
        return profile.tags
    agg: dict[str, float] = {}
    for s in profile.seeds[:8]:
        if not s.get("mbid"):
            continue

        async def f(mbid=s["mbid"]):
            return await ctx.mb.artist_tags(mbid)
        try:
            tags = await ctx.cache.get_or_fetch(f"mb:tags:{s['mbid']}", f, 30 * 86400, miss_ttl=86400) or {}
        except Exception:  # noqa: BLE001
            continue
        for t, c in tags.items():
            if t not in JUNK_TAGS and len(t) > 2:
                agg[t] = agg.get(t, 0.0) + s["weight"] * math.log1p(c)
    profile.tags = agg
    return agg


async def gen_fresh(ctx, profile: Profile, params: dict, rng: random.Random) -> list[Candidate]:
    days = params.get("fresh_days", 30)

    async def f():
        return await ctx.lb.fresh_releases(days=days, future=False)
    rels = await ctx.cache.get_or_fetch(f"lb:fresh:{ctx.settings.lb_user}:{days}", f, 6 * 3600) or []
    out, today = [], date.today()
    for rel in rels[: ctx.deezer.fanout(params.get("fresh_max", 40))]:
        artist, album = rel.get("artist_credit_name"), rel.get("release_name")
        if not (artist and album):
            continue
        try:
            rd = datetime.strptime((rel.get("release_date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - rd).days
        if age < 0:
            continue  # unreleased: picked up by a later run once it is out

        async def find(artist=artist, album=album):
            a = await ctx.deezer.search_album(artist, album)
            if not a:
                return None
            tr = await ctx.deezer.album_tracks(a["id"])
            tr = sorted(tr, key=lambda t: -(t.get("rank") or 0))
            return {"title": tr[0]["title"], "id": tr[0]["id"], "duration": tr[0].get("duration")} if tr else None
        t = await ctx.cache.get_or_fetch(f"dz:focus:{norm(artist)}|{norm(album)}", find, 7 * 86400, miss_ttl=86400)
        if not t:
            continue
        out.append(Candidate(
            title=t["title"], artist=artist, generator="fresh", album=album,
            artist_mbid=(rel.get("artist_mbids") or [None])[0], duration_ms=(t.get("duration") or 0) * 1000 or None,
            raw_score=min(1.0, float(rel.get("confidence") or 0.6)), freshness=max(0.0, 1 - age / max(days, 1)),
            seed=album, seeds={album}, deezer_id=str(t["id"]), tags=set(rel.get("release_tags") or []),
            reason=f"New release · {album}", extra={"release_date": rel.get("release_date")}))
    return out


GENERATORS = {"lb_radio": gen_lb_radio, "lb_jams": gen_lb_jams, "ytm_radio": gen_ytm_radio,
              "deezer_2hop": gen_deezer_2hop, "taste_twins": gen_taste_twins, "tag_vector": gen_tag_vector,
              "fresh": gen_fresh}


async def run_generators(ctx, profile: Profile, names: list[str], params: dict,
                         rng: random.Random | None = None) -> tuple[list[Candidate], dict]:
    rng = rng or random.Random()
    sem = asyncio.Semaphore(4)
    info: dict[str, dict] = {}

    async def run(name):
        needs = NEEDS.get(name, [])
        if any(not ctx.providers[p].enabled for p in needs):
            info[name] = {"count": 0, "error": "provider disabled"}
            return []
        async with sem:
            try:
                res = await GENERATORS[name](ctx, profile, params, rng)
                info[name] = {"count": len(res), "error": None}
                return res
            except Exception as e:  # noqa: BLE001 - one sick source must not sink the build
                log.warning("generator %s failed: %s", name, e)
                info[name] = {"count": 0, "error": f"{type(e).__name__}: {e}"}
                return []

    lists = await asyncio.gather(*[run(n) for n in names if n in GENERATORS])
    return [c for lst in lists for c in lst], info
