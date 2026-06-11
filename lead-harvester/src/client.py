from __future__ import annotations
import logging
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .config import settings
from .models import Job, Lead, IngestResponse

logger = logging.getLogger(__name__)


class HarvesterAuthError(Exception):
    """Raised when the server returns 401 — stop polling, don't retry."""


class ClarioScopeClient:
    """Async HTTP client for all ClarioScope ingest API calls."""

    def __init__(self) -> None:
        self._base = settings.clarioscope_url.rstrip("/")
        self._headers = {
            "X-Harvester-Token": settings.harvester_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ClarioScopeClient":
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()

    # ── Public API ─────────────────────────────────────────────────────────

    async def get_next_job(self) -> Job | None:
        """
        Returns the next queued job, or None when the server sends 204 (no work).
        Raises HarvesterAuthError on 401 so the poller can stop gracefully.
        """
        resp = await self._client.get(f"{self._base}/api/lead-harvester/jobs/next")

        if resp.status_code == 204:
            return None
        if resp.status_code == 401:
            raise HarvesterAuthError("Invalid or missing HARVESTER_TOKEN — check .env and Laravel config")
        resp.raise_for_status()

        data = resp.json()
        return Job(**data["job"])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def post_ingest(
        self,
        job_id: int,
        leads: list[Lead],
        is_final: bool = False,
    ) -> IngestResponse:
        """POST a batch of leads. Retries up to 3 times on 5xx / network errors."""
        payload = {
            "leads": [lead.model_dump(exclude_none=True) for lead in leads],
            "is_final": is_final,
        }
        resp = await self._client.post(
            f"{self._base}/api/lead-harvester/jobs/{job_id}/ingest",
            json=payload,
        )
        resp.raise_for_status()
        return IngestResponse(**resp.json())

    async def post_failed(self, job_id: int, error: str) -> None:
        """Report a whole-job failure so it doesn't sit in_progress forever."""
        try:
            await self._client.post(
                f"{self._base}/api/lead-harvester/jobs/{job_id}/failed",
                json={"error": error[:1000]},
            )
        except Exception as exc:
            logger.error("Could not report job %d failure to API: %s", job_id, exc)
