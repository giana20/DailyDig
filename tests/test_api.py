import time

import pytest
from fastapi.testclient import TestClient

from dailydig import api as api_mod
from dailydig.config import Settings, set_settings


@pytest.fixture
def client(tmp_path):
    s = Settings(config_dir=str(tmp_path / "c"), music_dir=str(tmp_path / "m"))
    s.lb_user, s.enable_scheduler, s.workers = "tester", False, 1
    set_settings(s)
    with TestClient(api_mod.app) as c:
        yield c, s


def test_ui_status_playlists(client):
    c, _ = client
    assert "dailydig" in c.get("/").text
    st = c.get("/api/status").json()
    assert st["configured"]["listenbrainz_user"] and {p["name"] for p in st["providers"]} >= {"listenbrainz", "deezer"}
    pls = c.get("/api/playlists").json()
    assert [p["kind"] for p in pls] == ["daily_dig", "discover_weekly", "release_radar", "on_repeat"]
    pid = pls[0]["id"]
    r = c.patch(f"/api/playlists/{pid}", json={"schedule_cron": "0 7 * * *", "config": {"size": 12}})
    assert r.json()["cron"] == "0 7 * * *" and r.json()["config"]["size"] == 12
    assert c.patch(f"/api/playlists/{pid}", json={"schedule_cron": "nonsense"}).status_code == 422
    assert c.patch(f"/api/playlists/{pid}", json={"config": {"evil": 1}}).status_code == 422
    assert c.patch(f"/api/playlists/{pid}", json={"config": {"size": 9999}}).status_code == 422


def test_ingest_scrobble_and_feedback(client):
    c, s = client
    body = {"listen_type": "single", "payload": [{"listened_at": int(time.time()), "track_metadata": {
        "artist_name": "Slowdive", "track_name": "Alison", "additional_info": {"duration_ms": 230000}}}]}
    assert c.get("/apis/listenbrainz/1/validate-token").json()["valid"]
    assert c.post("/apis/listenbrainz/1/submit-listens", json=body).json()["stored"] == 1
    assert c.post("/apis/listenbrainz/1/submit-listens", json=body).json()["stored"] == 0   # idempotent
    assert c.get("/api/taste").json()["plays"] == 1
    assert c.post("/api/feedback", json={"action": "block_artist", "artist": "Bad Band"}).json()["ok"]
    assert "bad band" in c.get("/api/taste").json()["blocked_artists"]
    assert c.post("/api/feedback", json={"action": "bogus"}).status_code == 422


def test_ingest_token_enforced(tmp_path):
    s = Settings(config_dir=str(tmp_path / "c"), music_dir=str(tmp_path / "m"))
    s.enable_scheduler, s.workers, s.ingest_token, s.admin_password = False, 1, "secret", "pw"
    set_settings(s)
    with TestClient(api_mod.app) as c:
        assert c.get("/api/status").status_code == 401                         # UI needs the admin password
        assert c.get("/api/status", auth=("me", "pw")).status_code == 200
        assert c.get("/api/health").status_code == 200
        v = c.get("/apis/listenbrainz/1/validate-token", headers={"Authorization": "Token nope"}).json()
        assert v["valid"] is False
        ok = c.get("/apis/listenbrainz/1/validate-token", headers={"Authorization": "Token secret"}).json()
        assert ok["valid"] is True


async def test_scheduler_tick_enqueues(ctx):
    from dailydig import playlists
    from dailydig.runtime import Runtime
    playlists.seed_playlists(ctx)
    ctx.db.execute("UPDATE playlists SET next_run=0 WHERE kind='daily_dig'")
    rt = Runtime(ctx)
    await rt.tick()
    await rt.tick()      # second tick must not double-enqueue
    kinds = [r["kind"] for r in ctx.db.query("SELECT kind FROM jobs")]
    assert kinds.count("build_playlist") == 1 and {"taste_sync", "train_ranker", "cleanup"} <= set(kinds)
