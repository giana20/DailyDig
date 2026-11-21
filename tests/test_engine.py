import json
import random
import time

import pytest

from dailydig import bandit, learning, playlists
from dailydig.models import Candidate
from dailydig.ranker import PersonalRanker, apply_final, merge, tier_a
from dailydig.taste import load_profile, parse_listen, store_events

NOW = int(time.time())
A1, A2 = "a1000000-0000-0000-0000-000000000001", "a2000000-0000-0000-0000-000000000002"


def seed_history(ctx):
    evs = []
    for i in range(30):
        evs.append({"user": "tester", "track_key": f"r{i%5}", "recording_mbid": None, "title": f"Song {i%5}",
                    "artist": "Slowdive" if i % 2 else "Ride", "artist_mbid": A1 if i % 2 else A2,
                    "played_at": NOW - i * 3600, "duration_ms": 200000, "source": "test"})
    store_events(ctx, evs)


def fake_lb(ctx):
    async def radio_artist(mbid, mode, pb, pe, per, n):
        return [{"recording_mbid": f"{mbid[:2]}{i:034d}"[:36], "similar_artist_mbid": f"b{i}", "total_listen_count": 1000 * (i + 1)}
                for i in range(6)]

    async def recording_metadata(mbids):
        return {m: {"title": f"Track {m[-2:]}", "artist": f"Artist {m[-2:]}", "duration_ms": 210000,
                    "artist_mbid": f"c{m[-2:]}", "album": "Alb"} for m in mbids}

    async def created_for(user=None):
        return [{"mbid": "pl1", "title": "Daily Jams", "kind": "daily-jams", "date": None}]

    async def playlist(mbid):
        return [{"recording_mbid": None, "title": "Jam Song", "artist": "Jam Artist", "album": None,
                 "duration_ms": 180000, "artist_mbid": None}]

    async def similar_users(user=None):
        return [{"user_name": "twin1", "similarity": 0.8}]

    async def stats_recordings(user, rng, count):
        return [{"track_name": "Twin Song", "artist_name": "Twin Artist", "recording_mbid": None, "artist_mbids": []}]

    for n, f in dict(radio_artist=radio_artist, recording_metadata=recording_metadata, created_for=created_for,
                     playlist=playlist, similar_users=similar_users, stats_recordings=stats_recordings).items():
        setattr(ctx.lb, n, f)


def test_assemble_one_per_artist_and_quotas():
    def mk(i, gen, artist):
        c = Candidate(title=f"t{i}", artist=artist, generator=gen, seeds={f"s{i % 2}"})
        from dailydig.ranker import Scored
        s = Scored(cand=c, generators={gen}, seeds=c.seeds)
        s.final = 100 - i
        return s
    scored = [mk(i, "lb_radio" if i < 6 else "deezer_2hop", f"A{i // 2}") for i in range(12)]
    picks = playlists.assemble(scored, 4, {"lb_radio": 2, "deezer_2hop": 2})
    assert len(picks) == 4
    artists = [p[0].cand.artist for p in picks]
    assert len(set(artists)) == 4                                   # one per artist
    assert sum(1 for _, arm in picks if arm == "deezer_2hop") == 2   # quota honoured


def test_merge_consensus_beats_single_source():
    a = Candidate(title="Halo", artist="Beyoncé", generator="lb_radio", raw_score=0.5, seeds={"x"})
    b = Candidate(title="Halo (Remastered)", artist="Beyonce", generator="deezer_2hop", raw_score=0.5, seeds={"y"})
    c = Candidate(title="Other", artist="Z", generator="lb_radio", raw_score=0.5, seeds={"x"})
    scored = merge([a, b, c])
    assert len(scored) == 2

    class P:  # minimal profile
        tags, artist_neg = {}, {}
        def plays_for(self, *a): return 0
    tier_a(scored, P())
    halo = next(s for s in scored if s.cand.title.startswith("Halo"))
    other = next(s for s in scored if s.cand.title == "Other")
    assert halo.heuristic > other.heuristic and len(halo.generators) == 2


def test_bandit_prefers_winners_but_explores(ctx):
    for _ in range(40):
        bandit.update(ctx.db, "good", 1.0)
        bandit.update(ctx.db, "bad", 0.0)
    alloc = bandit.allocate(ctx.db, ["good", "bad"], 200, epsilon=0.2, rng=random.Random(1))
    assert alloc["good"] > 150 and alloc["bad"] > 5 and sum(alloc.values()) == 200


async def test_parse_listen():
    l = {"listened_at": 1700000000, "track_metadata": {"artist_name": "A", "track_name": "T", "additional_info": {
        "recording_mbid": "r", "artist_mbids": ["a"], "duration_ms": 1000}}}
    e = parse_listen("u", l)
    assert e["recording_mbid"] == "r" and e["artist_mbid"] == "a" and e["duration_ms"] == 1000


async def test_build_daily_dig_end_to_end(ctx):
    playlists.seed_playlists(ctx)
    seed_history(ctx)
    fake_lb(ctx)
    ctx.deezer.enabled = ctx.ytm.enabled = False          # only LB-backed generators in this test
    pid = ctx.db.one("SELECT id FROM playlists WHERE kind='daily_dig'")["id"]
    ctx.db.execute("UPDATE playlists SET config_json=? WHERE id=?",
                   (json.dumps({"size": 8, "generators": ["lb_radio", "lb_jams", "taste_twins"], "exploration": 0.2,
                                "cooldown_days": 14, "max_artist_plays": 40, "download": True}), pid))
    res = await playlists.build_playlist(ctx, pid, rng=random.Random(3))
    assert res["count"] == 8 and res["tier"] == "A"
    rows = playlists.current_tracks(ctx, "daily_dig")
    assert len(rows) == 8 and len({r["artist"] for r in rows}) == 8
    assert all(r["status"] == "pending" for r in rows)
    assert ctx.queue.counts().get("queued", 0) >= 8          # download jobs + deliver job
    # seen-ledger: rebuilding must not re-offer the same tracks within cooldown
    first = {r["title"] for r in rows}
    res2 = await playlists.build_playlist(ctx, pid, rng=random.Random(4))
    second = {r["title"] for r in playlists.current_tracks(ctx, "daily_dig")}
    assert not (first & second)


async def test_outcome_labels_and_training(ctx):
    pid = ctx.db.insert("INSERT INTO playlists(kind,name,config_json,schedule_cron) VALUES('x','x','{}','* * * * *')")
    t = time.time()
    for i in range(300):
        good = i % 2 == 0
        feats = {"cf": 0.9 if good else 0.2, "novelty": 0.5, "consensus": 0.5 if good else 0.0}
        ctx.db.insert(
            "INSERT INTO playlist_tracks(playlist_id,track_key,title,artist,generator,status,added_at,features_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, f"k{i}", f"T{i}", f"A{i}", "lb_radio", "downloaded", t - 20 * 86400,
             json.dumps({"f": feats, "g": ["lb_radio"]})))
        if good:
            ctx.db.execute(
                "INSERT INTO play_events(user,track_key,title,artist,played_at) VALUES('tester',?,?,?,?)",
                (f"k{i}", f"T{i}", f"A{i}", int(t - 5 * 86400)))
    out = learning.label_outcomes(ctx)
    assert out["labeled"] == 300 and out["positive"] == 150 and out["negative"] == 150
    assert bandit.get_arms(ctx.db, ["lb_radio"])["lb_radio"][0] > 100
    res = await learning.train_ranker(ctx)
    assert res["trained"] and res["auc_holdout"] > 0.9
    r = learning.load_ranker(ctx)
    from dailydig.ranker import Scored
    hi = Scored(cand=Candidate(title="a", artist="b", generator="lb_radio"), generators={"lb_radio"},
                features={"cf": 0.9, "novelty": 0.5, "consensus": 0.5})
    lo = Scored(cand=Candidate(title="c", artist="d", generator="lb_radio"), generators={"lb_radio"},
                features={"cf": 0.2, "novelty": 0.5, "consensus": 0.0})
    assert r.predict([hi, lo]) and hi.ml > lo.ml
