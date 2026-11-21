"""yubal web-API client.

yubal's REST API is not publicly documented and has changed between versions (a plain POST /jobs
returns 405 on current releases). yubal is a FastAPI app, so we read its own /openapi.json and use
whatever jobs endpoint it actually exposes, instead of guessing paths.
"""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from ..providers.base import ProviderError

DONE = {"completed", "complete", "success", "succeeded", "done", "finished"}
FAILED = {"failed", "error", "errored", "cancelled", "canceled"}
URL_KEYS = ("url", "source_url", "link", "playlist_url")


class QueueFull(ProviderError):
    pass


class YubalError(ProviderError):
    pass


class Yubal:
    def __init__(self, ctx):
        self.ctx = ctx
        self._spec: dict | None = None
        self._create: tuple[str, dict] | None = None   # (path, request-body schema)
        self._list_path: str | None = None

    @property
    def base(self) -> str:
        return self.ctx.settings.yubal_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base)

    async def health(self) -> dict:
        if not self.configured:
            return {"ok": False, "error": "DD_YUBAL_URL not set"}
        for p in ("/health", "/api/health", "/api/v1/health"):
            try:
                r = await self.ctx.http.get(f"{self.base}{p}", timeout=8)
                if r.status_code == 200:
                    return {"ok": True, "status": 200}
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
                return {"ok": False, "error": err}
        return {"ok": False, "error": "no health endpoint answered 200"}

    # -- discovery -------------------------------------------------------------------------------
    async def spec(self) -> dict:
        if self._spec is None:
            for p in ("/openapi.json", "/api/openapi.json"):
                try:
                    r = await self.ctx.http.get(f"{self.base}{p}", timeout=15)
                    if r.status_code == 200:
                        self._spec = r.json()
                        break
                except (httpx.HTTPError, ValueError):
                    continue
            if self._spec is None:
                raise YubalError("could not read yubal's /openapi.json; use DD_DOWNLOADER=yubal_cli or ytdlp instead")
        return self._spec

    @staticmethod
    def _resolve(spec: dict, schema: dict) -> dict:
        ref = (schema or {}).get("$ref")
        if ref and ref.startswith("#/"):
            node = spec
            for part in ref[2:].split("/"):
                node = node.get(part, {})
            return node
        return schema or {}

    async def discover(self) -> tuple[str, dict]:
        if self._create:
            return self._create
        spec = await self.spec()
        paths = spec.get("paths") or {}
        posts = [(p, ops["post"]) for p, ops in paths.items()
                 if "post" in ops and re.search(r"/(jobs?|downloads?|sync)/?$", p)]
        if not posts:
            raise YubalError("yubal exposes no job-creation endpoint I recognise. Paths seen: "
                             + ", ".join(sorted(paths)[:25]))
        posts.sort(key=lambda x: (0 if x[0].rstrip("/").endswith("jobs") else 1, len(x[0])))
        path, op = posts[0]
        body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
        self._create = (path, self._resolve(spec, body))
        gets = [p for p, ops in paths.items() if "get" in ops and p.rstrip("/") == path.rstrip("/")]
        self._list_path = gets[0] if gets else None
        return self._create

    # -- jobs ------------------------------------------------------------------------------------------
    async def create_job(self, url: str, max_items: int = 1) -> str:
        path, schema = await self.discover()
        props = schema.get("properties") or {}
        body: dict = {}
        key = next((k for k in URL_KEYS if k in props), None)
        if key is None:
            raise YubalError(f"yubal job schema has no url field (properties: {sorted(props)})")
        body[key] = url
        if "max_items" in props:
            body["max_items"] = max_items
        missing = [k for k in schema.get("required") or [] if k not in body]
        if missing:
            raise YubalError(f"yubal job requires fields I do not know how to fill: {missing}")
        r = await self.ctx.http.post(f"{self.base}{path}", json=body, timeout=30)
        if r.status_code == 409:
            raise QueueFull("yubal queue is full")
        if r.status_code not in (200, 201, 202):
            raise YubalError(f"yubal POST {path} -> HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        return str(j.get("id") or j.get("job_id") or "")

    async def jobs(self) -> list[dict]:
        await self.discover()
        path = self._list_path
        if not path:
            raise YubalError("yubal has no job-list endpoint; cannot poll")
        r = await self.ctx.http.get(f"{self.base}{path}", timeout=30)
        if r.status_code != 200:
            raise YubalError(f"yubal GET {path} -> HTTP {r.status_code}")
        j = r.json()
        return j if isinstance(j, list) else (j.get("jobs") or j.get("items") or [])

    @staticmethod
    def _status(job: dict) -> str:
        return str(job.get("status") or job.get("state") or "").lower()

    async def wait(self, job_id: str, timeout: float = 1800, poll: float = 2.0) -> dict:
        deadline, seen = time.monotonic() + timeout, False
        while time.monotonic() < deadline:
            job = next((j for j in await self.jobs() if str(j.get("id")) == job_id), None)
            if job:
                seen = True
                st = self._status(job)
                if st in DONE:
                    return job
                if st in FAILED:
                    raise YubalError(f"yubal job {job_id} {st}: {job.get('error') or job.get('message') or ''}")
            elif seen:   # vanished after being seen: assume finished; caller verifies the file exists
                return {"id": job_id, "status": "completed", "inferred": True}
            await asyncio.sleep(poll)
        raise YubalError(f"yubal job {job_id} timed out after {timeout:.0f}s")
