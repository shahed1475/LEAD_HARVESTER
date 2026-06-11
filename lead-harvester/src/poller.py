"""
Main polling loop:
  1. GET /jobs/next          — receive (vertical, state, city) target
  2. scrape()                — collect leads from all sources for that target
  3. POST /jobs/{id}/ingest  — push batches of ≤200 leads, is_final=True on last
  4. On exception            — POST /jobs/{id}/failed so job doesn't stall
  5. Sleep on 204            — no work right now, back off and retry
"""
from __future__ import annotations
import asyncio
import json
import logging
import sys
from itertools import islice
from typing import Iterator

from .client import ClarioScopeClient, HarvesterAuthError
from .config import settings
from .models import Lead
from .scrapers.registry import get_scrapers_for_vertical

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunked(iterable: list, size: int) -> Iterator[list]:
    it = iter(iterable)
    while chunk := list(islice(it, size)):
        yield chunk


def _dedupe(leads: list[Lead]) -> list[Lead]:
    """Remove within-batch duplicates by external_id (keeps first occurrence)."""
    seen: set[str] = set()
    out: list[Lead] = []
    for lead in leads:
        key = lead.external_id or id(lead)
        if key not in seen:
            seen.add(str(key))
            out.append(lead)
    return out


# ── Core scrape orchestrator ───────────────────────────────────────────────────

async def _scrape_job(job) -> list[Lead]:
    scraper_classes = get_scrapers_for_vertical(job.vertical_slug)
    if not scraper_classes:
        logger.warning("No scrapers registered for vertical '%s'", job.vertical_slug)
        return []

    all_leads: list[Lead] = []

    for scraper_cls in scraper_classes:
        if len(all_leads) >= job.target_count:
            break
        try:
            scraper = scraper_cls()
            leads = await scraper.scrape(job)
            logger.info(
                "%s returned %d leads for %s/%s/%s",
                scraper_cls.__name__, len(leads),
                job.vertical_slug, job.state, job.city,
            )
            all_leads.extend(leads)
        except Exception as exc:
            logger.warning("Scraper %s raised: %s", scraper_cls.__name__, exc)

    return _dedupe(all_leads)[: job.target_count]


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run() -> None:
    poll_interval = settings.harvester_poll_interval_ms / 1000
    logger.info("Lead harvester starting — polling %s every %.0fs",
                settings.clarioscope_url, poll_interval)

    async with ClarioScopeClient() as client:
        while True:
            # ── Poll for work ───────────────────────────────────────────────
            try:
                job = await client.get_next_job()
            except HarvesterAuthError as exc:
                logger.critical(str(exc))
                return  # Hard stop — bad token, no point retrying
            except Exception as exc:
                logger.error("Poll request failed: %s — sleeping %.0fs", exc, poll_interval)
                await asyncio.sleep(poll_interval)
                continue

            if job is None:
                logger.debug("204 — no jobs queued, sleeping %.0fs", poll_interval)
                await asyncio.sleep(poll_interval)
                continue

            logger.info(
                "Job %d: vertical=%s state=%s city=%s target=%d",
                job.id, job.vertical_slug, job.state, job.city, job.target_count,
            )

            # ── Scrape ──────────────────────────────────────────────────────
            try:
                leads = await _scrape_job(job)
            except Exception as exc:
                logger.error("Scraping job %d crashed: %s", job.id, exc)
                await client.post_failed(job.id, str(exc))
                continue

            if not leads:
                await client.post_failed(job.id, "No leads returned by any scraper")
                continue

            logger.info("Job %d: %d total leads scraped", job.id, len(leads))

            # ── Push batches ────────────────────────────────────────────────
            try:
                batches = list(_chunked(leads, settings.batch_size))
                for i, batch in enumerate(batches):
                    is_final = (i == len(batches) - 1)
                    result = await client.post_ingest(job.id, batch, is_final=is_final)
                    logger.info(
                        "Job %d batch %d/%d — created=%d updated=%d "
                        "duplicates=%d failed=%d",
                        job.id, i + 1, len(batches),
                        result.created, result.updated,
                        result.duplicates, result.failed,
                    )
                    for err in result.errors:
                        logger.warning(
                            "Job %d ingest error [index %d]: %s",
                            job.id, err.index, err.reason,
                        )
            except Exception as exc:
                logger.error("Ingest failed for job %d: %s", job.id, exc)
                await client.post_failed(job.id, f"ingest error: {exc}")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
