"""Closing the loop: label outcomes of past playlist tracks, feed the bandit, train the personal ranker."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from . import bandit
from .db import now
from .models import norm
from .ranker import MIN_LABELS, PersonalRanker

log = logging.getLogger("dailydig.learning")
IGNORE_AFTER_DAYS = 10


def label_outcomes(ctx) -> dict:
    """Played after being offered -> positive; starred -> strongest; blocked/ignored -> negative."""
    user = ctx.settings.lb_user or "me"
    rows = ctx.db.query(
        "SELECT id, track_key, title, artist, generator, added_at FROM playlist_tracks "
        "WHERE label IS NULL AND status IN ('downloaded','delivered')")
    if not rows:
        return {"labeled": 0}
    since = min(r["added_at"] for r in rows)
    plays: dict[str, int] = {}
    for p in ctx.db.query("SELECT track_key, title, artist, played_at FROM play_events WHERE user=? AND played_at>=?",
                          (user, int(since))):
        for k in (p["track_key"], f"n:{norm(p['artist'])}|{norm(p['title'])}"):
            plays[k] = max(plays.get(k, 0), p["played_at"])
    fb = {(r["target_id"], r["action"]) for r in ctx.db.query("SELECT target_id, action FROM feedback WHERE user=?", (user,))}
    n = {"positive": 0, "negative": 0}
    t = now()
    for r in rows:
        name_key = f"n:{norm(r['artist'])}|{norm(r['title'])}"
        keys = {r["track_key"], name_key}
        played = any(plays.get(k, 0) >= (r["added_at"] or 0) for k in keys)
        starred = any((k, "star") in fb or (k, "love") in fb for k in keys)
        bad = any((k, a) in fb for k in keys for a in ("dislike", "block", "hate")) or \
              (norm(r["artist"]), "block") in fb
        label, reward = None, 0.0
        if bad:
            label, reward = 0, 0.0
        elif starred:
            label, reward = 1, 2.0
        elif played:
            label, reward = 1, 1.0
        elif (r["added_at"] or t) < t - IGNORE_AFTER_DAYS * 86400:
            label, reward = 0, 0.0
        if label is None:
            continue
        ctx.db.execute("UPDATE playlist_tracks SET label=?, labeled_at=? WHERE id=?", (label, t, r["id"]))
        bandit.update(ctx.db, r["generator"] or "unknown", reward)
        n["positive" if label else "negative"] += 1
    return {"labeled": n["positive"] + n["negative"], **n}


def training_rows(ctx) -> list[tuple[dict, list[str], int]]:
    out = []
    for r in ctx.db.query("SELECT features_json, label FROM playlist_tracks WHERE label IS NOT NULL AND features_json IS NOT NULL"):
        try:
            j = json.loads(r["features_json"])
            out.append((j["f"], j["g"], int(r["label"])))
        except (ValueError, KeyError):
            continue
    return out


async def train_ranker(ctx, min_labels: int = MIN_LABELS) -> dict:
    path = os.path.join(ctx.settings.config_dir, "ranker.pkl")
    rows = training_rows(ctx)
    res = await asyncio.to_thread(PersonalRanker.train, path, rows, min_labels)
    ctx.db.kv_set("ranker_stats", {**res, "at": now()})
    return res


def load_ranker(ctx) -> PersonalRanker | None:
    r = PersonalRanker(os.path.join(ctx.settings.config_dir, "ranker.pkl"))
    return r if r.load() else None
