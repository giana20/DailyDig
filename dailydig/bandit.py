"""Thompson-sampling allocation of playlist slots across candidate generators.

Each generator is a Beta(alpha, beta) arm. Rewards: completed +1, saved +2, ignored/skipped 0.
A hard epsilon floor keeps exploring so a lucky early arm can't starve the others.
"""
from __future__ import annotations

import random

from .db import DB, now

DECAY_PER_DAY = 0.98


def get_arms(db: DB, arms: list[str]) -> dict[str, tuple[float, float]]:
    rows = {r["arm"]: (r["alpha"], r["beta"]) for r in db.query("SELECT arm, alpha, beta FROM bandit")}
    return {a: rows.get(a, (1.0, 1.0)) for a in arms}


def update(db: DB, arm: str, reward: float) -> None:
    a, b = (1.0, 0.0) if reward > 0 else (0.0, 1.0)
    db.execute(
        "INSERT INTO bandit(arm,alpha,beta,updated_at) VALUES(?,?,?,?) ON CONFLICT(arm) DO UPDATE SET "
        "alpha=alpha+?, beta=beta+?, updated_at=?",
        (arm, 1.0 + reward * a, 1.0 + b, now(), reward * a, b, now()))


def decay(db: DB, days: float = 1.0) -> None:
    f = DECAY_PER_DAY ** days
    db.execute("UPDATE bandit SET alpha=1+(alpha-1)*?, beta=1+(beta-1)*?", (f, f))


def allocate(db: DB, arms: list[str], slots: int, epsilon: float = 0.2, rng: random.Random | None = None) -> dict[str, int]:
    """Return {arm: n_slots}. Sums to `slots`."""
    rng = rng or random.Random()
    posts = get_arms(db, arms)
    out = {a: 0 for a in arms}
    for _ in range(slots):
        if rng.random() < epsilon:
            pick = rng.choice(arms)
        else:
            pick = max(arms, key=lambda a: rng.betavariate(*posts[a]))
        out[pick] += 1
    return out


def precision(db: DB) -> list[dict]:
    return [{"arm": r["arm"], "alpha": round(r["alpha"], 2), "beta": round(r["beta"], 2),
             "mean": round(r["alpha"] / (r["alpha"] + r["beta"]), 3)} for r in db.query("SELECT * FROM bandit ORDER BY arm")]
