"""SQLite job queue with leases, heartbeats and fencing (Aurral-style).

Workers claim with a single UPDATE, renew leases by heartbeat, and every write is
fenced on (id, worker_id, attempts) so a zombie worker can never settle a job that
has been re-claimed by someone else.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .db import DB, now

LEASE_S = 60


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    worker_id: str
    attempts: int
    max_attempts: int

    @property
    def fence(self) -> tuple[int, str, int]:
        return (self.id, self.worker_id, self.attempts)


class JobQueue:
    def __init__(self, db: DB):
        self.db = db

    def enqueue(self, kind: str, payload: dict | None = None, *, run_at: float | None = None,
                max_attempts: int = 3, dedupe_key: str | None = None) -> int | None:
        t = now()
        if dedupe_key:
            r = self.db.one(
                "SELECT id FROM jobs WHERE dedupe_key=? AND state IN ('queued','running')", (dedupe_key,))
            if r:
                return None
        return self.db.insert(
            "INSERT INTO jobs(kind,payload,state,run_at,max_attempts,created_at,updated_at,dedupe_key)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (kind, json.dumps(payload or {}), "queued", run_at or t, max_attempts, t, t, dedupe_key))

    def reset_expired(self) -> int:
        t = now()
        return self.db.execute(
            "UPDATE jobs SET state='queued', worker_id=NULL, lease_until=NULL, run_at=?, updated_at=?,"
            " error='The worker stopped renewing its claim' WHERE state='running' AND lease_until < ?",
            (t, t, t))

    def claim(self, worker_id: str, kinds: list[str] | None = None, lease_s: int = LEASE_S) -> Job | None:
        t = now()
        kind_sql = ""
        if kinds:
            kind_sql = " AND kind IN (%s)" % ",".join("?" * len(kinds))
        sql = ("UPDATE jobs SET state='running', worker_id=?, lease_until=?, attempts=attempts+1, updated_at=? "
               "WHERE id=(SELECT id FROM jobs WHERE state='queued' AND run_at<=?" + kind_sql +
               " ORDER BY run_at, id LIMIT 1) AND state='queued' "
               "RETURNING id, kind, payload, worker_id, attempts, max_attempts")
        with self.db.conn() as c:
            row = c.execute(sql, [worker_id, t + lease_s, t, t] + (kinds or [])).fetchone()
        if not row:
            return None
        return Job(row["id"], row["kind"], json.loads(row["payload"] or "{}"), row["worker_id"],
                   row["attempts"], row["max_attempts"])

    def heartbeat(self, job: Job, lease_s: int = LEASE_S) -> bool:
        n = self.db.execute(
            "UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND worker_id=? AND attempts=? AND state='running'",
            (now() + lease_s, now(), *job.fence))
        return n == 1

    def complete(self, job: Job, result: Any = None) -> bool:
        return self.db.execute(
            "UPDATE jobs SET state='done', result=?, lease_until=NULL, updated_at=?, error=NULL"
            " WHERE id=? AND worker_id=? AND attempts=? AND state='running'",
            (json.dumps(result), now(), *job.fence)) == 1

    def fail(self, job: Job, error: str, *, retry_in: float | None = None, final: bool = False) -> bool:
        t = now()
        if not final and job.attempts < job.max_attempts:
            delay = retry_in if retry_in is not None else min(300.0, 5 * 2 ** job.attempts)
            return self.db.execute(
                "UPDATE jobs SET state='queued', worker_id=NULL, lease_until=NULL, run_at=?, error=?, updated_at=?"
                " WHERE id=? AND worker_id=? AND attempts=? AND state='running'",
                (t + delay, error[:2000], t, *job.fence)) == 1
        return self.db.execute(
            "UPDATE jobs SET state='failed', lease_until=NULL, error=?, updated_at=?"
            " WHERE id=? AND worker_id=? AND attempts=? AND state='running'",
            (error[:2000], t, *job.fence)) == 1

    def reschedule(self, job: Job, delay: float) -> bool:
        """Put a running job back in the queue without burning an attempt."""
        t = now()
        return self.db.execute(
            "UPDATE jobs SET state='queued', worker_id=NULL, lease_until=NULL, run_at=?, attempts=MAX(attempts-1,0),"
            " updated_at=? WHERE id=? AND worker_id=? AND attempts=? AND state='running'",
            (t + delay, t, *job.fence)) == 1

    def counts(self) -> dict[str, int]:
        return {r["state"]: r["n"] for r in self.db.query("SELECT state, COUNT(*) n FROM jobs GROUP BY state")}

    def recent(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT id,kind,state,attempts,run_at,updated_at,error,payload FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,))]

    def prune(self, keep_days: int = 7) -> int:
        return self.db.execute(
            "DELETE FROM jobs WHERE state IN ('done','failed') AND updated_at < ?", (now() - keep_days * 86400,))

    # -- named cross-process mutexes -------------------------------------
    def acquire_lock(self, name: str, owner: str, ttl: float = 300) -> bool:
        t = now()
        with self.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO job_locks(name,owner,expires_at) VALUES(?,?,0)", (name, owner))
            n = c.execute(
                "UPDATE job_locks SET owner=?, expires_at=? WHERE name=? AND (expires_at<? OR owner=?)",
                (owner, t + ttl, name, t, owner)).rowcount
        return n == 1

    def release_lock(self, name: str, owner: str) -> None:
        self.db.execute("UPDATE job_locks SET expires_at=0 WHERE name=? AND owner=?", (name, owner))
