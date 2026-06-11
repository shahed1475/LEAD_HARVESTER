"""
Google Maps scraper — all verticals, Playwright headless Chromium.

Used as fallback / supplement for verticals with no dedicated directory
(med-spa, house-call) and to fill gaps from other scrapers.

Strategy:
  1. Navigate to google.com/maps/search/{query}
  2. Wait for the results feed
  3. Iterate through listing cards, clicking each to load details
  4. Extract: name, phone, website, address from the info panel
  5. Scroll to load more results

Anti-detection:
  - Disable automation flags
  - Random viewport / locale
  - Realistic UA via fake-useragent

Rate: 5 s base + 1–3 s jitter.  Circuit breaks after 3 errors.
"""
from __future__ import annotations
import asyncio
import logging
import re
import urllib.parse

from fake_useragent import UserAgent

from ..config import settings
from ..models import Job, Lead
from ..normalize import make_external_id, normalize_name, normalize_phone, normalize_url
from .base import BaseScraper

logger = logging.getLogger(__name__)

_ua = UserAgent()

_VERTICAL_QUERIES: dict[str, str] = {
    "dental": "dental clinic",
    "mental-health": "mental health therapist",
    "primary-care": "primary care physician",
    "med-spa": "medical spa aesthetics",
    "weight-loss": "weight loss clinic semaglutide",
    "house-call": "house call mobile doctor",
}

# Google Maps CSS selectors (stable as of 2025 — may need updates if Google changes UI)
_FEED_SELECTOR = 'div[role="feed"]'
_RESULT_SELECTOR = 'div.Nv2PK'          # business card in results list
_NAME_SELECTOR = "h1.DUwDvf"            # business name in detail panel
_PHONE_SELECTOR = '[data-tooltip="Copy phone number"]'
_WEBSITE_SELECTOR = 'a[data-item-id="authority"]'
_ADDRESS_SELECTOR = '[data-item-id="address"]'


class GoogleMapsScraper(BaseScraper):
    source = "google_maps"
    source_detail = "google_maps_listing"
    base_delay = 5.0
    jitter = (1.0, 3.0)
    max_failures = 3       # Google is aggressive — open circuit quickly

    async def _do_scrape(self, job: Job) -> list[Lead]:
        query = _VERTICAL_QUERIES.get(job.vertical_slug, job.vertical_slug)
        search_query = f"{query} near {job.city} {job.state}"
        encoded = urllib.parse.quote(search_query)
        maps_url = f"https://www.google.com/maps/search/{encoded}"

        leads: list[Lead] = []

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Playwright not installed — skipping Google Maps scraper")
            return []

        async with async_playwright() as p:
            launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            if settings.proxy_url:
                launch_args.append(f"--proxy-server={settings.proxy_url}")

            browser = await p.chromium.launch(headless=True, args=launch_args)
            context = await browser.new_context(
                user_agent=_ua.random,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/Chicago",
            )

            # Mask automation signals
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            page = await context.new_page()

            try:
                await page.goto(maps_url, wait_until="domcontentloaded", timeout=30_000)

                # Wait for results feed
                try:
                    await page.wait_for_selector(_FEED_SELECTOR, timeout=15_000)
                except Exception:
                    logger.warning("Google Maps: results feed did not load for %s", search_query)
                    return []

                # Scroll and collect listings
                collected = 0
                previous_count = 0
                stale_scrolls = 0

                while collected < job.target_count:
                    cards = page.locator(_RESULT_SELECTOR)
                    count = await cards.count()

                    for i in range(previous_count, count):
                        if collected >= job.target_count:
                            break
                        try:
                            card = cards.nth(i)
                            await card.scroll_into_view_if_needed()
                            await card.click()
                            await asyncio.sleep(1.5)  # wait for panel to load

                            lead = await self._extract_from_panel(page, job)
                            if lead and not self._is_excluded(lead.website, job.exclude_domains):
                                leads.append(lead)
                                collected += 1
                        except Exception as exc:
                            logger.debug("Google Maps card %d error: %s", i, exc)
                            continue

                    if count == previous_count:
                        stale_scrolls += 1
                        if stale_scrolls >= 3:
                            break
                    else:
                        stale_scrolls = 0

                    previous_count = count

                    # Scroll the feed panel to load more
                    try:
                        feed = page.locator(_FEED_SELECTOR)
                        await feed.evaluate("el => el.scrollBy(0, 800)")
                        await asyncio.sleep(2)
                    except Exception:
                        break

            finally:
                await browser.close()

        return leads

    async def _extract_from_panel(self, page, job: Job) -> Lead | None:
        """Extract business details from the open info panel."""
        try:
            # Name
            name_loc = page.locator(_NAME_SELECTOR)
            name = normalize_name(await name_loc.text_content(timeout=5_000))
        except Exception:
            return None
        if not name:
            return None

        # Phone
        phone = None
        try:
            phone_loc = page.locator(_PHONE_SELECTOR).first
            raw_phone = await phone_loc.text_content(timeout=3_000)
            phone = normalize_phone(raw_phone)
        except Exception:
            pass

        # Website
        website = None
        try:
            web_loc = page.locator(_WEBSITE_SELECTOR).first
            website = normalize_url(await web_loc.get_attribute("href", timeout=3_000))
        except Exception:
            pass

        # Address
        city = job.city
        state = job.state
        try:
            addr_loc = page.locator(_ADDRESS_SELECTOR).first
            addr_text = await addr_loc.text_content(timeout=3_000)
            if addr_text and "," in addr_text:
                parts = addr_text.split(",")
                if len(parts) >= 2:
                    city = parts[-2].strip() or city
                    state_zip = parts[-1].strip()
                    state = state_zip.split()[0] if state_zip else state
        except Exception:
            pass

        if not phone and not website:
            return None

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        external_id = make_external_id("gmaps", f"{slug}-{job.city.lower()}-{job.state.lower()}")

        return Lead(
            practice_name=name[:255],
            phone=phone,
            website=website,
            vertical_slug=job.vertical_slug,
            city=city[:120],
            state=state[:60],
            country="US",
            source="lead_harvester",
            source_detail=self.source_detail,
            external_id=external_id,
            raw={"query": f"{job.vertical_slug} {job.city} {job.state}"},
        )
