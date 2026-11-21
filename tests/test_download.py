import os
import subprocess
import time

import httpx
import pytest

from dailydig import cleanup, deliver
from dailydig.download import backends, files, layout, pipeline
from dailydig.download.matcher import passes_bitrate_floor, pick_best, verify

V = dict(cand_artists=["Slowdive"], video_id="v")


def test_matcher_rules():
    ok = verify("Souvlaki Space Dust", "Slowdive", 340, cand_title="Souvlaki Space Dust (Official Audio)",
                cand_duration_s=343, **V)
    assert ok.level == "accept"
    assert verify("Souvlaki Space Dust", "Slowdive", 340, cand_title="Souvlaki Space Dust (Live)",
                  cand_duration_s=341, **V).level == "reject"
    assert verify("Souvlaki Space Dust", "Slowdive", 340, cand_title="Souvlaki Space Dust (Sped Up)",
                  cand_duration_s=300, **V).level == "reject"
    far = verify("Souvlaki Space Dust", "Slowdive", 340, cand_title="Souvlaki Space Dust", cand_duration_s=400, **V)
    assert far.level == "reject" and "different recording" in far.reasons[0]
    near = verify("Souvlaki Space Dust", "Slowdive", 340, cand_title="Souvlaki Space Dust", cand_duration_s=349, **V)
    assert near.level == "accept"
    partial = verify("Space Dust", "Slowdive", 340, cand_title="Space Dust Remastered Edition", cand_duration_s=345, **V)
    assert partial.level == "verify"
    assert verify("Halo", "Beyonce", 200, cand_title="Halo", cand_artists=["Someone Else"],
                  cand_duration_s=200, video_id="x").level == "reject"
    assert verify("Halo", "Beyonce", None, cand_title="Halo", cand_artists=["Beyonce"], cand_duration_s=None,
                  video_id="x").level == "accept"
    best = pick_best("Halo", "Beyonce", 261, [
        {"video_id": "live", "title": "Halo (Live)", "artists": ["Beyonce"], "duration_s": 262},
        {"video_id": "studio", "title": "Halo", "artists": ["Beyonce"], "duration_s": 261}])
    assert best.video_id == "studio" and best.level == "accept"


def test_bitrate_floor():
    assert not passes_bitrate_floor(100_000, 300, lossless=False)
    assert passes_bitrate_floor(5_000_000, 300, lossless=False)
    assert passes_bitrate_floor(1_000_000, 300, lossless=False)
    assert not passes_bitrate_floor(1_000_000, 300, lossless=True)


def make_audio(path, seconds=200, codec="libopus"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink",
                    "-c:a", codec, "-b:a", "96k", path], check=True)


@pytest.fixture
def fake_dl(ctx, settings, monkeypatch):
    """Stub the yt-dlp subprocess: writes a 200 s noise file and counts calls."""
    settings.fetch_lyrics = settings.replaygain = False
    state = {"runs": 0}

    async def fake_run(self, url, workdir):
        state["runs"] += 1
        os.makedirs(workdir, exist_ok=True)
        out = os.path.join(workdir, "vid.opus")
        make_audio(out, 200)
        return out
    monkeypatch.setattr(backends.YtDlp, "run_ytdlp", fake_run)
    return state


async def seed_row(ctx, **over):
    pid = ctx.db.insert("INSERT INTO playlists(kind,name,config_json,schedule_cron) VALUES('t','Test PL','{}','* * * * *')")
    row = dict(title="Souvlaki Space Dust", artist="Slowdive", duration_ms=200_000,
               recording_mbid="0e1f5d2b-1c3e-4c0a-9d6e-111111111111", artist_mbid="a1000000-0000-0000-0000-000000000001")
    row.update(over)
    rid = ctx.db.insert(
        "INSERT INTO playlist_tracks(playlist_id,track_key,position,title,artist,artist_mbid,recording_mbid,duration_ms,"
        "status,added_at) VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
        (pid, row["recording_mbid"], 0, row["title"], row["artist"], row["artist_mbid"], row["recording_mbid"],
         row["duration_ms"], time.time()))
    return pid, rid


async def test_download_validate_tag_deliver(ctx, settings, fake_dl):
    async def search(q, limit=5):
        return [{"video_id": "live1", "title": "Souvlaki Space Dust (Live)", "artists": ["Slowdive"], "duration_s": 201},
                {"video_id": "vid1", "title": "Souvlaki Space Dust", "artists": ["Slowdive"], "duration_s": 200}]
    ctx.ytm.search_songs = search
    pid, rid = await seed_row(ctx)
    res = await pipeline.download_track(ctx, rid)
    assert res["status"] == "downloaded" and fake_dl["runs"] == 1
    row = ctx.db.one("SELECT * FROM playlist_tracks WHERE id=?", (rid,))
    assert row["video_id"] == "vid1" and row["file_path"].endswith(".opus")
    assert row["file_path"].startswith(settings.discovery_dir)
    import mutagen
    tags = mutagen.File(row["file_path"]).tags
    assert tags["musicbrainz_trackid"] == ["0e1f5d2b-1c3e-4c0a-9d6e-111111111111"]
    out = await deliver.deliver_playlist(ctx, pid)
    assert out["tracks"] == 1 and out["m3u8"]
    body = open(out["m3u8"], encoding="utf-8").read()
    assert "#EXTM3U" in body and "../_discovery/Slowdive/" in body and ".opus" in body
    assert ctx.db.one("SELECT status FROM playlist_tracks WHERE id=?", (rid,))["status"] == "delivered"


async def test_ambiguous_goes_to_review_then_manual_approve(ctx, settings, fake_dl):
    async def search(q, limit=5):
        return [{"video_id": "v9", "title": "Souvlaki Space Dust", "artists": ["Slowdive"], "duration_s": 213}]
    ctx.ytm.search_songs = search
    _, rid = await seed_row(ctx)
    res = await pipeline.download_track(ctx, rid)
    assert res["status"] == "review" and fake_dl["runs"] == 0
    assert pipeline.approve(ctx, rid)
    res = await pipeline.download_track(ctx, rid)
    assert res["status"] == "downloaded"


async def test_fake_silence_rejected(ctx, settings, fake_dl, monkeypatch):
    async def search(q, limit=5):
        return [{"video_id": "v1", "title": "Souvlaki Space Dust", "artists": ["Slowdive"], "duration_s": 200}]
    ctx.ytm.search_songs = search
    monkeypatch.setattr(files, "validate", lambda p, e: (False, "bitrate 3 kbit/s below anti-fake floor"))
    _, rid = await seed_row(ctx)
    res = await pipeline.download_track(ctx, rid)
    assert res["status"] == "failed"
    assert not any(f.endswith(".opus") for _, _, fs in os.walk(settings.discovery_dir) for f in fs)


async def test_promote_and_expire(ctx, settings, fake_dl):
    async def search(q, limit=5):
        return [{"video_id": "v1", "title": "Souvlaki Space Dust", "artists": ["Slowdive"], "duration_s": 200}]
    ctx.ytm.search_songs = search
    pid, rid = await seed_row(ctx)
    await pipeline.download_track(ctx, rid)
    new = cleanup.promote(ctx, rid)
    assert new and os.path.exists(new) and "_discovery" not in new
    assert ctx.db.one("SELECT status FROM playlist_tracks WHERE id=?", (rid,))["status"] == "promoted"
    p2 = os.path.join(settings.discovery_dir, "X", "Y", "z.opus")
    make_audio(p2, 5)
    ctx.db.insert("INSERT INTO playlist_tracks(playlist_id,track_key,title,artist,status,file_path,added_at) "
                  "VALUES(?,?,?,?,'retired',?,?)", (pid, "k", "z", "X", p2, time.time() - 90 * 86400))
    assert cleanup.expire_discovery(ctx)["expired_files"] == 1
    assert not os.path.exists(p2) and os.path.exists(new)


async def test_navidrome_sync(ctx, settings):
    settings.navidrome_url, settings.navidrome_user, settings.navidrome_pass = "http://nd.test", "u", "p"
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        ep = req.url.path.rsplit("/", 1)[-1]
        calls.append((ep, dict(req.url.params)))
        ok = {"subsonic-response": {"status": "ok"}}
        if ep == "search3":
            ok["subsonic-response"]["searchResult3"] = {"song": [{"id": "s1", "title": "Halo", "artist": "Beyonce"}]}
        if ep == "getPlaylists":
            ok["subsonic-response"]["playlists"] = {"playlist": [{"id": "p1", "name": "Daily Dig"}]}
        if ep == "createPlaylist":
            ok["subsonic-response"]["playlist"] = {"id": "p1"}
        return httpx.Response(200, json=ok)
    ctx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    nd = ctx.navidrome
    assert await nd.ping()
    assert (await nd.find_song("Beyonce", "Halo"))["id"] == "s1"
    assert await nd.find_song("Nobody", "Unrelated Thing") is None
    assert await nd.sync_playlist("Daily Dig", ["s1"]) == "p1"
    create = [c for c in calls if c[0] == "createPlaylist"][0][1]
    assert create["playlistId"] == "p1" and "name" not in create
    assert {"t", "s", "u", "v", "c"} <= set(create)
    assert "p" not in create


def test_layout_is_configurable_and_safe(tmp_path):
    root = str(tmp_path)
    f = {"album_artist": "AC/DC", "year": "1980", "album": "Back in Black", "track": 3, "title": "Hells Bells?", "ext": "opus"}
    p = layout.render("{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}", f, root)
    assert p == os.path.join(root, "AC_DC", "1980 - Back in Black", "03 - Hells Bells_.opus")
    assert layout.render("{album_artist} - {title}.{ext}", f, root) == os.path.join(root, "AC_DC - Hells Bells_.opus")
    evil = layout.render("{album}/{title}.{ext}", {**f, "album": "../../etc", "title": "x"}, root)
    assert os.path.commonpath([evil, root]) == root
    assert "é" not in layout.render("{title}.{ext}", {**f, "title": "Café"}, root, ascii_only=True)


async def test_ytdlp_backend_layout_lyrics_replaygain(ctx, settings, monkeypatch):
    settings.audio_format = "opus"
    settings.path_template = "{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}"

    async def album_info(browse_id, video_id=None):
        return {"album": "Souvlaki", "album_artist": "Slowdive", "year": "1993", "track": 4, "tracks_total": 10, "cover": None}
    ctx.ytm.album_info = album_info

    async def fake_run(self, url, workdir):
        os.makedirs(workdir, exist_ok=True)
        out = os.path.join(workdir, "vid1.opus")
        make_audio(out, 30)
        return out
    monkeypatch.setattr(backends.YtDlp, "run_ytdlp", fake_run)
    calls = []

    async def lrc(ctx_, artist, title, album, dur):
        calls.append((artist, title, album))
        return "[00:01.00]Hello"
    monkeypatch.setattr(backends.tagging, "fetch_lrc", lrc)

    req = backends.DLRequest(video_id="vid1", url="u", title="Souvlaki Space Dust", artist="Slowdive",
                             duration_s=30, album="Souvlaki", album_id="MPREb_x")
    path = await backends.get_backend(ctx).download(req)
    assert path == os.path.join(settings.discovery_dir, "Slowdive", "1993 - Souvlaki", "04 - Souvlaki Space Dust.opus")
    assert open(path[:-5] + ".lrc").read().startswith("[00:01.00]")
    import mutagen
    t = mutagen.File(path).tags
    assert t["album"] == ["Souvlaki"] and t["tracknumber"] == ["4/10"] and t["date"] == ["1993"]
    assert t["replaygain_track_gain"][0].endswith(" dB")
    assert not os.path.exists(os.path.join(settings.config_dir, "tmp", "vid1"))
    assert await backends.get_backend(ctx).download(req) == path


async def test_default_downloads_into_library_and_never_expires(ctx, settings, monkeypatch):
    settings.discovery_subdir, settings.expire_days = "", 0
    assert settings.discovery_dir == settings.music_dir and not settings.staging
    settings.path_template = "{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}"
    ctx.ytm.album_info = lambda *a, **k: _async({"album": "Souvlaki", "album_artist": "Slowdive", "year": "1993", "track": 4})

    async def fake_run(self, url, workdir):
        os.makedirs(workdir, exist_ok=True)
        out = os.path.join(workdir, "v.opus")
        make_audio(out, 20)
        return out
    monkeypatch.setattr(backends.YtDlp, "run_ytdlp", fake_run)
    settings.fetch_lyrics = False
    req = backends.DLRequest(video_id="v", url="u", title="Alison", artist="Slowdive", album="Souvlaki", album_id="x")
    path = await backends.get_backend(ctx).download(req)
    assert path == os.path.join(settings.music_dir, "Slowdive", "1993 - Souvlaki", "04 - Alison.opus")
    assert not os.path.exists(os.path.join(settings.config_dir, "tmp", "v"))
    pid = ctx.db.insert("INSERT INTO playlists(kind,name,config_json,schedule_cron) VALUES('z','z','{}','* * * * *')")
    ctx.db.insert("INSERT INTO playlist_tracks(playlist_id,track_key,title,artist,status,file_path,added_at) "
                  "VALUES(?,?,?,?,'retired',?,?)", (pid, "k", "Alison", "Slowdive", path, time.time() - 999 * 86400))
    assert cleanup.expire_discovery(ctx) == {"expired_files": 0} and os.path.exists(path)
    rid = ctx.db.one("SELECT id FROM playlist_tracks WHERE file_path=?", (path,))["id"]
    assert cleanup.promote(ctx, rid) is None and os.path.exists(path)


async def _async(v):
    return v
