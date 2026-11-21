"""Ranking. Tier A = heuristic fusion (always on). Tier B = personal gradient-boosted model
trained on your own play/skip outcomes (optional; falls back to Tier A when absent)."""
from __future__ import annotations

import json
import logging
import math
import os
import pickle
from dataclasses import dataclass, field

from .generators import GENERATORS, PRIOR
from .models import Candidate, norm
from .taste import Profile

log = logging.getLogger("dailydig.ranker")

W = {"cf": 38.0, "consensus": 14.0, "genre": 10.0, "novelty": 14.0, "twin": 6.0, "fresh": 10.0}
P = {"popularity": 12.0, "skip": 20.0}
MODES = {"safer": {"novelty": 0.5, "popularity": 0.6}, "balanced": {"novelty": 1.0, "popularity": 1.0},
         "deeper": {"novelty": 1.5, "popularity": 1.5}}
MIN_LABELS = 200


@dataclass
class Scored:
    cand: Candidate
    generators: set[str] = field(default_factory=set)
    seeds: set[str] = field(default_factory=set)
    features: dict = field(default_factory=dict)
    heuristic: float = 0.0
    ml: float | None = None
    final: float = 0.0
    video_id: str | None = None

    @property
    def key(self) -> str:
        return self.cand.key

    @property
    def reason(self) -> str:
        extra = len(self.generators) - 1
        return self.cand.reason + (f" (+{extra} more source{'s' if extra > 1 else ''})" if extra > 0 else "")


def merge(cands: list[Candidate]) -> list[Scored]:
    """Group candidates describing the same track (by normalised artist+title) across generators."""
    groups: dict[str, list[Candidate]] = {}
    for c in cands:
        groups.setdefault(f"{norm(c.artist)}|{norm(c.title)}", []).append(c)
    out = []
    for members in groups.values():
        best = max(members, key=lambda c: (c.recording_mbid is not None, c.raw_score * PRIOR.get(c.generator, 0.5)))
        prim = Candidate(**{**best.__dict__})
        for m in members:
            prim.recording_mbid = prim.recording_mbid or m.recording_mbid
            prim.artist_mbid = prim.artist_mbid or m.artist_mbid
            prim.duration_ms = prim.duration_ms or m.duration_ms
            prim.deezer_id = prim.deezer_id or m.deezer_id
            prim.album = prim.album or m.album
            prim.popularity = prim.popularity if prim.popularity is not None else m.popularity
        s = Scored(cand=prim, generators={m.generator for m in members},
                   seeds=set().union(*[m.seeds for m in members]))
        prim.tags = set().union(*[m.tags for m in members])
        prim.freshness = max(m.freshness for m in members)
        s.video_id = next((m.extra.get("video_id") for m in members if m.extra.get("video_id")), None)
        s.features = {"cf": max(m.raw_score * PRIOR.get(m.generator, 0.5) for m in members)}
        out.append(s)
    return out


def tier_a(scored: list[Scored], profile: Profile, mode: str = "balanced") -> None:
    mm = MODES.get(mode, MODES["balanced"])
    ptags = {t for t, _ in sorted(profile.tags.items(), key=lambda x: -x[1])[:25]}
    for s in scored:
        c = s.cand
        plays = profile.plays_for(c.artist, c.artist_mbid)
        gen_bonus = (len(s.generators) - 1) * 1.5
        f = s.features
        f["consensus"] = min(1.0, (len(s.seeds) + gen_bonus) / 4.0)
        f["genre"] = min(1.0, len(c.tags & ptags) / 2.0) if c.tags and ptags else 0.0
        f["novelty"] = 1.0 / (1.0 + math.log1p(plays))
        f["pop"] = max(0.0, ((c.popularity or 0.0) - 60.0) / 40.0) if c.popularity is not None else 0.0
        f["twin"] = 1.0 if "taste_twins" in s.generators else 0.0
        f["fresh"] = c.freshness
        f["skip"] = profile.artist_neg.get(norm(c.artist), 0.0)
        f["log_plays"] = math.log1p(plays)
        f["duration_min"] = (c.duration_ms or 0) / 60000.0
        s.heuristic = (W["cf"] * f["cf"] + W["consensus"] * f["consensus"] + W["genre"] * f["genre"] +
                       W["novelty"] * mm["novelty"] * f["novelty"] + W["twin"] * f["twin"] + W["fresh"] * f["fresh"] -
                       P["popularity"] * mm["popularity"] * f["pop"] - P["skip"] * f["skip"])
        s.final = s.heuristic


# ---- Tier B -------------------------------------------------------------------------
FEATURES = ["cf", "consensus", "genre", "novelty", "pop", "twin", "fresh", "skip", "log_plays", "duration_min"]
GEN_NAMES = sorted(GENERATORS)


def vectorize(features: dict, generators) -> list[float]:
    return [float(features.get(k, 0.0)) for k in FEATURES] + [1.0 if g in generators else 0.0 for g in GEN_NAMES]


class PersonalRanker:
    def __init__(self, path: str):
        self.path = path
        self.model = None
        self.meta: dict = {}

    def load(self) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, "rb") as fh:
                blob = pickle.load(fh)  # noqa: S301 - our own file in our own config dir
            if blob.get("features") != FEATURES + GEN_NAMES:
                return False
            self.model, self.meta = blob["model"], blob.get("meta", {})
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("could not load ranker: %s", e)
            return False

    def predict(self, scored: list[Scored]) -> bool:
        if self.model is None or not scored:
            return False
        X = [vectorize(s.features, s.generators) for s in scored]
        for s, p in zip(scored, self.model.predict_proba(X)[:, 1]):
            s.ml = float(p)
        return True

    @classmethod
    def train(cls, path: str, rows: list[tuple[dict, list[str], int]], min_labels: int = MIN_LABELS) -> dict:
        """rows: (features, generators, label). Returns stats; writes the model on success."""
        pos = sum(1 for r in rows if r[2] == 1)
        if len(rows) < min_labels or pos < 10 or len(rows) - pos < 10:
            return {"trained": False, "reason": f"need >= {min_labels} labels with both classes (have {len(rows)}, {pos} positive)"}
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            from sklearn.metrics import roc_auc_score
            from sklearn.model_selection import train_test_split
        except ImportError:
            return {"trained": False, "reason": "scikit-learn not installed"}
        X = [vectorize(f, g) for f, g, _ in rows]
        y = [r[2] for r in rows]
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=7, stratify=y)
        model = HistGradientBoostingClassifier(max_depth=3, max_iter=80, learning_rate=0.1, random_state=7)
        model.fit(Xtr, ytr)
        auc = float(roc_auc_score(yte, model.predict_proba(Xte)[:, 1]))
        model.fit(X, y)
        meta = {"rows": len(rows), "positive": pos, "auc_holdout": round(auc, 3)}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as fh:
            pickle.dump({"model": model, "features": FEATURES + GEN_NAMES, "meta": meta}, fh)
        os.replace(path + ".tmp", path)
        return {"trained": True, **meta}


def apply_final(scored: list[Scored], blend: float = 0.5) -> None:
    for s in scored:
        s.final = s.heuristic if s.ml is None else (1 - blend) * s.heuristic + blend * 100.0 * s.ml


def features_json(s: Scored) -> str:
    return json.dumps({"f": {k: round(float(v), 4) for k, v in s.features.items()}, "g": sorted(s.generators)})
