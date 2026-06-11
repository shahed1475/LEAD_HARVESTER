"""
BaseScraper — shared rate-limiter, circuit-breaker, httpx client factory.
All concrete scrapers inherit from this and implement _do_scrape().
"""
from __future__ import annotations
import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx
from fake_useragent import UserAgent

from ..config import settings
from ..models import Job, Lead

logger = logging.getLogger(__name__)

_ua = UserAgent()


class CircuitBreaker:
    def __init__(self, max_failures: int = 5, reset_after: float = 300.0) -> None:
        self.max_failures = max_failures
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at and time.monotonic() - self._opened_at > self.reset_after:
            self._failures = 0
            self._opened_at = None
        return self._failures >= self.max_failures

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.max_failures:
            self._opened_at = time.monotonic()
            logger.warning(
                "%s circuit opened after %d failures — pausing for %.0fs",
                self.__class__.__name__, self._failures, self.reset_after,
            )


class BaseScraper(ABC):
    source: str = ""
    source_detail: str = ""
    base_delay: float = 2.0
    jitter: tuple[float, float] = (0.5, 2.0)
    max_failures: int = 5
    circuit_reset: float = 300.0

    def __init__(self) -> None:
        self._last_request: float = 0.0
        self._cb = CircuitBreaker(self.max_failures, self.circuit_reset)

    # ── Public entry point ────────────────────────────────────────────────

    async def scrape(self, job: Job) -> list[Lead]:
        if self._cb.is_open:
            logger.warning("%s circuit is open — skipping", self.__class__.__name__)
            return []
        try:
            leads = await self._do_scrape(job)
            self._cb.record_success()
            return leads
        except Exception as exc:
            self._cb.record_failure()
            raise exc

    # ── Abstract ─────────────────────────────────────────────────────────

    @abstractmethod
    async def _do_scrape(self, job: Job) -> list[Lead]:
        ...

    # ── Helpers available to all scrapers ─────────────────────────────────

    async def _rate_limit(self) -> None:
        """Enforce base_delay + jitter since last request (wall-clock)."""
        now = time.monotonic()
        elapsed = now - self._last_request
        delay = self.base_delay + random.uniform(*self.jitter)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    def _make_client(self) -> httpx.AsyncClient:
        """Return a configured httpx client with proxy support and realistic headers."""
        headers = {
            "User-Agent": _ua.random,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        kwargs: dict = dict(
            headers=headers,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
        )
        if settings.proxy_url:
            kwargs["proxy"] = settings.proxy_url

        return httpx.AsyncClient(**kwargs)

    async def _safe_get(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response | None:
        """GET with circuit-breaker error recording. Returns None on failure."""
        try:
            await self._rate_limit()
            resp = await client.get(url)
            if resp.status_code == 200:
                self._cb.record_success()
                return resp
            if resp.status_code in (403, 429):
                logger.warning("%s got %d from %s", self.source, resp.status_code, url)
                self._cb.record_failure()
            return None
        except Exception as exc:
            logger.debug("%s request error for %s: %s", self.source, url, exc)
            self._cb.record_failure()
            return None

    @staticmethod
    def _is_excluded(website: str | None, exclude_domains: list[str]) -> bool:
        if not website or not exclude_domains:
            return False
        try:
            domain = urlparse(website).netloc.lstrip("www.").lower()
            return domain in exclude_domains
        except Exception:
            return False
