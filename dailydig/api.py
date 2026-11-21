"""FastAPI app: REST + SSE for the web UI, plus an LB-compatible ingest endpoint."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from croniter import croniter
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, bandit, cleanup, playlists, taste
from .config import get_settings
from .context import Context
from .db import now
from .download import pipeline
from .download.backends import choose, get_backend
from .models import norm, track_key
from .runtime import Runtime

log = logging.getLogger("dailydig.api")
STATIC = os.path.join(os.path.dirname(__file__), "static")
CONFIG_KEYS = {"size", "generators", "mode", "exploration", "cooldown_days", "pop_begin", "pop_end", "max_artist_plays",
               "include_library", "download", "jam_kinds", "days", "fresh_days", "use_ml", "one_per_artist", "lb_mode"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ctx = Context.create(settings)
    playlists.seed_playlists(ctx)
    rt = Runtime(ctx)
    await rt.start()
    app.state.ctx, app.state.rt = ctx, rt
    try:
        yield
    finally:
        await rt.stop()
        await ctx.close()


app = FastAPI(title="dailydig", version=__version__, lifespan=lifespan)


def C(request: Request) -> Context:
    return request.app.state.ctx


# ---- auth (optional single admin password; HTTP Basic, any username) -----------------------------
@app.middleware("http")
async def auth(request: Request, call_next):
    pw = get_settings().admin_password
    p = request.url.path
    if pw and not (p.startswith("/apis/") or p == "/api/health"):
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, pw)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return JSONResponse({"detail": "auth required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="dailydig"'})
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
async def health():
    return {"ok": True, "version": __version__}


# ---- status / system -----------------------------------------------------------------------------
def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


@app.get("/api/status")
async def status(request: Request):
    ctx = C(request)
    s = ctx.settings
    return {
        "version": __version__, "user": s.lb_user, "region": s.region, "tier": s.tier,
        "providers": [p.health() for p in ctx.providers.values()],
        "jobs": ctx.queue.counts(), "ranker": ctx.db.kv_get("ranker_stats"),
        "configured": {"listenbrainz_user": bool(s.lb_user), "lb_token": bool(s.lb_token),
                       "navidrome": ctx.navidrome.configured, "yubal": bool(s.yubal_url)},
    }


@app.get("/api/system")
async def system(request: Request):
    ctx = C(request)
    dbp = ctx.settings.db_path
    return {"rss_mb": _rss_mb(), "db_mb": round(os.path.getsize(dbp) / 1e6, 2) if os.path.exists(dbp) else 0,
            "cache_entries": ctx.cache.size(), "jobs": ctx.queue.counts(), "recent_jobs": ctx.queue.recent(40),
            "bandit": bandit.precision(ctx.db), "ranker": ctx.db.kv_get("ranker_stats"),
            "plays": ctx.db.one("SELECT COUNT(*) n FROM play_events")["n"]}


@app.get("/api/jobs")
async def jobs(request: Request):
    return C(request).queue.recent(100)


@app.post("/api/sync")
async def sync(request: Request):
    return {"queued": C(request).queue.enqueue("taste_sync", {}, dedupe_key="sys:taste_sync")}


@app.post("/api/ranker/train")
async def train(request: Request):
    return {"queued": C(request).queue.enqueue("train_ranker", {}, dedupe_key="sys:train_ranker")}


# ---- playlists -------------------------------------------------------------------------------------
def _pl_out(ctx, r) -> dict:
    counts = {x["status"]: x["n"] for x in ctx.db.query(
        "SELECT status, COUNT(*) n FROM playlist_tracks WHERE playlist_id=? AND status!='retired' GROUP BY status", (r["id"],))}
    return {"id": r["id"], "kind": r["kind"], "name": r["name"], "enabled": bool(r["enabled"]), "cron": r["schedule_cron"],
            "config": json.loads(r["config_json"] or "{}"), "last_run": r["last_run"], "next_run": r["next_run"],
            "counts": counts}


@app.get("/api/playlists")
async def list_playlists(request: Request):
    ctx = C(request)
    return [_pl_out(ctx, r) for r in ctx.db.query("SELECT * FROM playlists ORDER BY id")]


class PlaylistPatch(BaseModel):
    enabled: bool | None = None
    schedule_cron: str | None = None
    config: dict | None = None


@app.patch("/api/playlists/{pid}")
async def patch_playlist(pid: int, body: PlaylistPatch, request: Request):
    ctx = C(request)
    r = ctx.db.one("SELECT * FROM playlists WHERE id=?", (pid,))
    if not r:
        raise HTTPException(404, "no such playlist")
    if body.enabled is not None:
        ctx.db.execute("UPDATE playlists SET enabled=? WHERE id=?", (1 if body.enabled else 0, pid))
    if body.schedule_cron is not None:
        if not croniter.is_valid(body.schedule_cron):
            raise HTTPException(422, "invalid cron expression")
        ctx.db.execute("UPDATE playlists SET schedule_cron=?, next_run=? WHERE id=?",
                       (body.schedule_cron, playlists.next_run(body.schedule_cron), pid))
    if body.config:
        cfg = json.loads(r["config_json"] or "{}")
        bad = set(body.config) - CONFIG_KEYS
        if bad:
            raise HTTPException(422, f"unknown config keys: {sorted(bad)}")
        if "size" in body.config and not (1 <= int(body.config["size"]) <= 200):
            raise HTTPException(422, "size must be 1..200")
        cfg.update(body.config)
        ctx.db.execute("UPDATE playlists SET config_json=? WHERE id=?", (json.dumps(cfg), pid))
    return _pl_out(ctx, ctx.db.one("SELECT * FROM playlists WHERE id=?", (pid,)))


@app.post("/api/playlists/{pid}/build")
async def build_now(pid: int, request: Request):
    ctx = C(request)
    if not ctx.db.one("SELECT 1 FROM playlists WHERE id=?", (pid,)):
        raise HTTPException(404, "no such playlist")
    jid = ctx.queue.enqueue("build_playlist", {"playlist_id": pid}, dedupe_key=f"build:{pid}", max_attempts=2)
    return {"queued": jid, "note": None if jid else "a build is already queued or running"}


@app.get("/api/playlists/{pid}/tracks")
async def playlist_tracks(pid: int, request: Request):
    rows = C(request).db.query(
        "SELECT id,position,title,artist,reason,score,status,generator,error,video_id FROM playlist_tracks "
        "WHERE playlist_id=? AND status!='retired' ORDER BY position", (pid,))
    return [dict(r) for r in rows]


@app.get("/api/today")
async def today(request: Request):
    ctx = C(request)
    return {"playlist": "daily_dig", "tracks": playlists.current_tracks(ctx, "daily_dig"),
            "build_running": bool(ctx.db.one("SELECT 1 FROM jobs WHERE kind='build_playlist' AND state IN ('queued','running')"))}


# ---- tracks / feedback -----------------------------------------------------------------------------------
@app.post("/api/tracks/{tid}/approve")
async def approve(tid: int, request: Request, video_id: str | None = None):
    if not pipeline.approve(C(request), tid, video_id):
        raise HTTPException(404, "track not found or no video to approve")
    return {"ok": True}


@app.post("/api/tracks/{tid}/retry")
async def retry(tid: int, request: Request):
    ctx = C(request)
    n = ctx.db.execute("UPDATE playlist_tracks SET status='pending', error=NULL, video_id=NULL WHERE id=? AND status IN ('failed','review')", (tid,))
    if not n:
        raise HTTPException(404, "track not found or not retryable")
    ctx.queue.enqueue("download_track", {"id": tid}, dedupe_key=f"dl:{tid}", max_attempts=2)
    return {"ok": True}


@app.post("/api/tracks/{tid}/promote")
async def promote(tid: int, request: Request):
    new = cleanup.promote(C(request), tid)
    if not new:
        raise HTTPException(404, "track has no file in the discovery folder")
    return {"path": new}


class Feedback(BaseModel):
    action: str                  # like | dislike | block_artist | unblock_artist
    track_id: int | None = None
    artist: str | None = None


@app.post("/api/feedback")
async def feedback(body: Feedback, request: Request):
    ctx = C(request)
    user = ctx.settings.lb_user or "me"
    row = ctx.db.one("SELECT * FROM playlist_tracks WHERE id=?", (body.track_id,)) if body.track_id else None
    if body.action in ("like", "dislike") and row:
        ctx.db.execute("INSERT INTO feedback(user,target_type,target_id,action,context_json,created_at) VALUES(?,?,?,?,?,?)",
                       (user, "track", row["track_key"], "star" if body.action == "like" else "dislike", "{}", now()))
        if row["recording_mbid"]:
            try:
                await ctx.lb.submit_feedback(row["recording_mbid"], 1 if body.action == "like" else -1)
            except Exception:  # noqa: BLE001 - feedback sync is best effort
                pass
        if body.action == "dislike":
            ctx.db.execute("UPDATE playlist_tracks SET status='retired' WHERE id=? AND status IN ('pending','review','failed')", (row["id"],))
        return {"ok": True}
    if body.action in ("block_artist", "unblock_artist"):
        artist = body.artist or (row["artist"] if row else None)
        if not artist:
            raise HTTPException(422, "artist or track_id required")
        if body.action == "block_artist":
            ctx.db.execute("INSERT INTO feedback(user,target_type,target_id,action,context_json,created_at) VALUES(?,?,?,?,?,?)",
                           (user, "artist", norm(artist), "block", "{}", now()))
        else:
            ctx.db.execute("DELETE FROM feedback WHERE user=? AND target_type='artist' AND target_id=? AND action='block'",
                           (user, norm(artist)))
        return {"ok": True}
    raise HTTPException(422, "unknown action")


# ---- providers / downloader / taste -----------------------------------------------------------------------
@app.get("/api/providers")
async def providers(request: Request):
    return [p.health() for p in C(request).providers.values()]


class ProviderPatch(BaseModel):
    enabled: bool


@app.patch("/api/providers/{name}")
async def patch_provider(name: str, body: ProviderPatch, request: Request):
    ctx = C(request)
    if name not in ctx.providers:
        raise HTTPException(404, "unknown provider")
    ctx.providers[name].enabled = body.enabled
    ctx.db.kv_set("providers_disabled", [n for n, p in ctx.providers.items() if not p.enabled])
    return ctx.providers[name].health()


@app.get("/api/downloader")
async def downloader(request: Request):
    ctx = C(request)
    s = ctx.settings
    be = get_backend(ctx)
    review = ctx.db.query("SELECT id,title,artist,status,error,video_id FROM playlist_tracks "
                          "WHERE status IN ('review','failed') ORDER BY id DESC LIMIT 100")
    return {"backend": choose(s), "backend_health": await be.health(), "yubal_url": s.yubal_url,
            "format": s.audio_format, "path_template": s.path_template, "concurrency": s.dl_concurrency,
            "lyrics": s.fetch_lyrics, "replaygain": s.replaygain,
            "navidrome": {"configured": ctx.navidrome.configured,
                          "ok": await ctx.navidrome.ping() if ctx.navidrome.configured else None},
            "discovery_dir": s.discovery_dir, "playlists_dir": s.playlists_dir,
            "expire_days": s.expire_days, "review": [dict(r) for r in review]}


@app.get("/api/taste")
async def taste_view(request: Request):
    ctx = C(request)
    p = await taste.load_profile(ctx)
    from .generators import profile_tags
    tags = {}
    try:
        tags = await profile_tags(ctx, p) if ctx.mb.enabled else {}
    except Exception:  # noqa: BLE001
        pass
    blocked = [r["target_id"] for r in ctx.db.query("SELECT target_id FROM feedback WHERE target_type='artist' AND action='block'")]
    return {"plays": p.total_plays, "seeds": p.seeds[:20],
            "tags": sorted(tags.items(), key=lambda x: -x[1])[:15], "blocked_artists": blocked,
            "seen_ledger": ctx.db.one("SELECT COUNT(*) n FROM seen_ledger")["n"]}


# ---- events (SSE) ------------------------------------------------------------------------------------------
@app.get("/api/events")
async def events(request: Request):
    ctx = C(request)

    async def gen():
        last = None
        while not await request.is_disconnected():
            snap = {"jobs": ctx.queue.counts(),
                    "tracks": {r["status"]: r["n"] for r in ctx.db.query(
                        "SELECT status, COUNT(*) n FROM playlist_tracks WHERE status!='retired' GROUP BY status")}}
            if snap != last:
                yield f"data: {json.dumps(snap)}\n\n"
                last = snap
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---- ListenBrainz-compatible ingest (point Navidrome at http://dailydig:8090/apis/listenbrainz) ---------------
def _ingest_ok(ctx: Context, request: Request) -> bool:
    given = request.headers.get("authorization", "").removeprefix("Token ").strip()
    s = ctx.settings
    allowed = [t for t in (s.ingest_token, s.lb_token) if t]
    return bool(given) and any(secrets.compare_digest(given, t) for t in allowed) if allowed else True


@app.get("/apis/listenbrainz/1/validate-token")
async def validate_token(request: Request):
    ctx = C(request)
    if not _ingest_ok(ctx, request):
        return JSONResponse({"code": 401, "message": "Invalid token", "valid": False}, status_code=200)
    return {"code": 200, "message": "Token valid.", "valid": True, "user_name": ctx.settings.lb_user or "me"}


@app.post("/apis/listenbrainz/1/submit-listens")
async def submit_listens(request: Request):
    ctx = C(request)
    if not _ingest_ok(ctx, request):
        raise HTTPException(401, "Invalid token")
    body = await request.json()
    kind, payload = body.get("listen_type"), body.get("payload") or []
    stored = 0
    if kind in ("single", "import"):
        user = ctx.settings.lb_user or "me"
        evs = [e for e in (taste.parse_listen(user, {**l, "listened_at": l.get("listened_at") or int(now())}) for l in payload) if e]
        stored = taste.store_events(ctx, evs)
    fan = ctx.db.kv_get("scrobble_fanout", {"listenbrainz": True})
    if fan.get("listenbrainz") and ctx.settings.lb_token and kind in ("single", "import", "playing_now"):
        try:  # forward to the real ListenBrainz; local copy is already safe
            await ctx.lb.submit_listens(body)
        except Exception as e:  # noqa: BLE001
            log.warning("fan-out to ListenBrainz failed: %s", e)
    return {"status": "ok", "stored": stored}
