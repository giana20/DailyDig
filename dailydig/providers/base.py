"""Provider base: shared HTTP, rate limiting, retries and failure-ratio health."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

import httpx

from ..ratelimit import RateLimiter

log = logging.getLogger("dailydig.providers")
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    pass


class Provider:
    name = "base"
    min_interval = 0.0           # seconds between requests, shared across processes
    base_url = ""

    def __init__(self, ctx):
        self.ctx = ctx
        self.enabled = True
        self.limiter = RateLimiter(ctx.db, self.name, self.min_interval) if self.min_interval else None
        self._results: deque[bool] = deque(maxlen=40)
        self.last_error: str | None = None

    # -- health (Aurral SourceHealth idea) --------------------------------
    def _record(self, ok: bool, err: str | None = None) -> None:
        self._results.append(ok)
        if not ok:
            self.last_error = err

    @property
    def failure_ratio(self) -> float:
        return 0.0 if not self._results else self._results.count(False) / len(self._results)

    def health(self) -> dict:
        r = self.failure_ratio
        state = "healthy" if r < 0.25 else "degraded" if r < 0.6 else "down"
        return {"name": self.name, "enabled": self.enabled, "state": state if self.enabled else "disabled",
                "failure_ratio": round(r, 2), "samples": len(self._results), "last_error": self.last_error}

    def fanout(self, n: int) -> int:
        """Shrink fan-out when the source is sick."""
        r = self.failure_ratio
        return n if r < 0.25 else max(1, n // 2) if r < 0.6 else max(1, n // 4)

    # -- http ---------------------------------------------------------------
    def headers(self) -> dict[str, str]:
        return {}

    async def get_json(self, path_or_url: str, params: dict | None = None, *, retries: int = 2,
                       timeout: float = 20.0) -> Any:
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        params = {k: v for k, v in (params or {}).items() if v is not None}
        err = "unknown"
        for attempt in range(retries + 1):
            if self.limiter:
                await self.limiter.wait()
            try:
                r = await self.ctx.http.get(url, params=params, headers=self.headers(), timeout=timeout)
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
                self._record(False, err)
                await asyncio.sleep(min(8, 0.5 * 2 ** attempt))
                continue
            self._after_response(r)
            if r.status_code == 404:
                self._record(True)
                return None
            if r.status_code in RETRY_STATUS:
                err = f"HTTP {r.status_code}"
                self._record(False, err)
                wait = _retry_after(r) or 0.5 * 2 ** attempt
                if self.limiter and r.status_code == 429:
                    self.limiter.pause(wait)
                await asyncio.sleep(min(wait, 15))
                continue
            if r.status_code >= 400:
                self._record(False, f"HTTP {r.status_code}")
                raise ProviderError(f"{self.name}: HTTP {r.status_code} for {url}")
            self._record(True)
            try:
                return r.json()
            except ValueError as e:
                raise ProviderError(f"{self.name}: bad JSON from {url}") from e
        raise ProviderError(f"{self.name}: gave up on {url} ({err})")

    def _after_response(self, r: httpx.Response) -> None:  # hook
        pass


def _retry_after(r: httpx.Response) -> float | None:
    for h in ("retry-after", "x-ratelimit-reset-in"):
        v = r.headers.get(h)
        if v:
            try:
                return float(v)
            except ValueError:
                pass
    return None
