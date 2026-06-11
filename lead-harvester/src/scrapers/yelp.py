"""
Yelp scraper — covers all 6 verticals.

Strategy:
  1. Search /search?find_desc={category}&find_loc={city},{state}&start={offset}
  2. Extract /biz/{slug} links from HTML (or JSON-LD ItemList)
  3. Visit each business page → extract JSON-LD LocalBusiness for phone/website/address
  4. Paginate until target_count reached

Rate: 3 s base + 0.5-2 s jitter.  Circuit breaks after 5 consecutive errors.
"""
from __future__ import annotations
import json
import logging
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from ..models import Job, Lead
from ..normalize import make_external_id, normalize_name, normalize_phone, normalize_url
from .base import BaseScraper

logger = logging.getLogger(__name__)

_CATEGORY_MAP: dict[str, str] = {
    "dental": "dentists",
    "mental-health": "mentalhealth",
    "primary-care": "physicians",
    "med-spa": "medspa",
    "weight-loss": "weightloss",
    "house-call": "homehealth",
}

# JSON-LD @type values Yelp uses for healthcare businesses
_BUSINESS_TYPES = {
    "LocalBusiness", "MedicalBusiness", "MedicalClinic", "Dentist",
    "Physician", "HealthAndBeautyBusiness", "MedicalOrganization",
    "ProfessionalService",
}


class YelpScraper(BaseScraper):
    source = "yelp"
    source_detail = "yelp_business_listing"
    base_delay = 3.0
    jitter = (0.5, 2.0)
    max_failures = 5

    async def _do_scrape(self, job: Job) -> list[Lead]:
        category = _CATEGORY_MAP.get(job.vertical_slug, job.vertical_slug)
        location = urllib.parse.quote(f"{job.city}, {job.state}")
        biz_urls: list[str] = []

        async with self._make_client() as client:
            # ── Step 1: collect business page URLs from search results ──────
            offset = 0
            while len(biz_urls) < job.target_count * 2:  # over-fetch; filter later
                search_url = (
                    f"https://www.yelp.com/search"
                    f"?find_desc={category}&find_loc={location}&start={offset}"
                )
                resp = await self._safe_get(client, search_url)
                if resp is None:
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                page_urls = self._extract_biz_urls(soup)
                if not page_urls:
                    break  # no more results

                biz_urls.extend(u for u in page_urls if u not in biz_urls)
                offset += 10

                # Yelp shows 10 per page; fewer than 5 means last page
                if len(page_urls) < 5:
                    break

            # ── Step 2: visit each business page ────────────────────────────
            leads: list[Lead] = []
            for url in biz_urls:
                if len(leads) >= job.target_count:
                    break

                resp = await self._safe_get(client, url)
                if resp is None:
                    continue

                soup = BeautifulSoup(resp.text, "lxml")

                # Skip permanently-closed businesses
                if self._is_permanently_closed(soup):
                    continue

                lead = self._parse_business_page(soup, url, job)
                if lead and not self._is_excluded(lead.website, job.exclude_domains):
                    leads.append(lead)

            return leads

    # ── Parsing helpers ────────────────────────────────────────────────────

    def _extract_biz_urls(self, soup: BeautifulSoup) -> list[str]:
        """Extract unique /biz/{slug} hrefs from a search results page."""
        seen: set[str] = set()
        urls: list[str] = []
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if href.startswith("/biz/") and "search" not in href:
                clean = href.split("?")[0].split("#")[0]
                full = f"https://www.yelp.com{clean}"
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
        return urls

    def _is_permanently_closed(self, soup: BeautifulSoup) -> bool:
        text = soup.get_text(" ", strip=True).lower()
        return "permanently closed" in text or "business is closed" in text

    def _parse_business_page(
        self, soup: BeautifulSoup, url: str, job: Job
    ) -> Lead | None:
        """Extract lead data from a Yelp business page via JSON-LD."""
        ld = self._extract_jsonld(soup)
        if not ld:
            return None

        name = normalize_name(ld.get("name"))
        if not name:
            return None

        phone = normalize_phone(ld.get("telephone"))
        website = normalize_url(ld.get("url"))

        addr: dict[str, Any] = ld.get("address") or {}
        city = addr.get("addressLocality") or job.city
        state = addr.get("addressRegion") or job.state

        slug = url.rstrip("/").rsplit("/biz/", 1)[-1]
        external_id = make_external_id("yelp", slug)

        # Must have at least phone or website to be useful
        if not phone and not website:
            return None

        return Lead(
            practice_name=name[:255],
            phone=phone,
            website=website,
            vertical_slug=job.vertical_slug,
            city=city[:120] if city else None,
            state=state[:60] if state else None,
            country="US",
            source="lead_harvester",
            source_detail=self.source_detail,
            external_id=external_id,
            raw={"yelp_url": url, "schema_type": ld.get("@type"), "jsonld": ld},
        )

    def _extract_jsonld(self, soup: BeautifulSoup) -> dict | None:
        """Find the first JSON-LD block that represents a healthcare business."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(script.string or "")
            except Exception:
                continue
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                if item.get("@type") in _BUSINESS_TYPES:
                    return item
        return None
