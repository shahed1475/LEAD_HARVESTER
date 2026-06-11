"""
Healthgrades scraper — dental, primary-care, mental-health, weight-loss.

Strategy:
  1. GET /search?what={specialty}&where={city},{state}
  2. Extract provider card links from search results
  3. Visit each provider page → JSON-LD + HTML for phone, website, address
  4. Paginate via &page=N

Rate: 2 s base + 0.5–1.5 s jitter.
"""
from __future__ import annotations
import json
import logging
import urllib.parse

from bs4 import BeautifulSoup

from ..models import Job, Lead
from ..normalize import make_external_id, normalize_name, normalize_phone, normalize_url
from .base import BaseScraper

logger = logging.getLogger(__name__)

_SPECIALTY_MAP: dict[str, str] = {
    "dental": "dentist",
    "primary-care": "family+doctor",
    "mental-health": "psychiatrist",
    "weight-loss": "weight-loss",
}

_JSONLD_TYPES = {
    "Physician", "Dentist", "MedicalBusiness", "MedicalClinic",
    "LocalBusiness", "HealthAndBeautyBusiness",
}


class HealthgradesScraper(BaseScraper):
    source = "healthgrades"
    source_detail = "healthgrades_listing"
    base_delay = 2.0
    jitter = (0.5, 1.5)
    max_failures = 5

    async def _do_scrape(self, job: Job) -> list[Lead]:
        specialty = _SPECIALTY_MAP.get(job.vertical_slug)
        if not specialty:
            logger.info("Healthgrades: no specialty mapping for %s", job.vertical_slug)
            return []

        location = urllib.parse.quote(f"{job.city}, {job.state}")
        provider_urls: list[str] = []

        async with self._make_client() as client:
            # ── Collect provider page URLs ────────────────────────────────
            page = 1
            while len(provider_urls) < job.target_count * 2:
                search_url = (
                    f"https://www.healthgrades.com/usearch"
                    f"?what={specialty}&where={location}&page={page}"
                )
                resp = await self._safe_get(client, search_url)
                if resp is None:
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                links = self._extract_provider_links(soup)
                if not links:
                    break

                provider_urls.extend(l for l in links if l not in provider_urls)
                page += 1

                if len(links) < 5:
                    break

            # ── Visit each provider page ──────────────────────────────────
            leads: list[Lead] = []
            for url in provider_urls:
                if len(leads) >= job.target_count:
                    break

                resp = await self._safe_get(client, url)
                if resp is None:
                    continue

                soup = BeautifulSoup(resp.text, "lxml")

                if self._is_closed(soup):
                    continue

                lead = self._parse_provider_page(soup, url, job)
                if lead and not self._is_excluded(lead.website, job.exclude_domains):
                    leads.append(lead)

            return leads

    # ── Parsing ─────────────────────────────────────────────────────────

    def _extract_provider_links(self, soup: BeautifulSoup) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            # Provider pages: /physician-directory/ or /dentist-directory/ etc.
            if "/directory/" in href and href not in seen:
                full = (
                    href if href.startswith("http")
                    else f"https://www.healthgrades.com{href}"
                )
                seen.add(href)
                urls.append(full)
        return urls

    def _is_closed(self, soup: BeautifulSoup) -> bool:
        text = soup.get_text(" ", strip=True).lower()
        return "no longer practicing" in text or "retired" in text

    def _parse_provider_page(
        self, soup: BeautifulSoup, url: str, job: Job
    ) -> Lead | None:
        # Try JSON-LD first
        ld = self._extract_jsonld(soup)
        name = normalize_name(ld.get("name") if ld else None)

        # Fall back to HTML title
        if not name:
            h1 = soup.find("h1")
            if h1:
                name = normalize_name(h1.get_text(strip=True))
        if not name:
            return None

        phone = normalize_phone(ld.get("telephone") if ld else None)
        if not phone:
            # Try HTML — healthgrades shows phone in a <a href="tel:...">
            tel_link = soup.find("a", href=lambda h: h and h.startswith("tel:"))
            if tel_link:
                phone = normalize_phone(tel_link["href"].replace("tel:", ""))

        website = normalize_url(ld.get("url") if ld else None)
        if not website:
            # Look for an "Official Website" link
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "healthgrades.com" not in href and href.startswith("http"):
                    text = a.get_text(strip=True).lower()
                    if "website" in text or "official" in text:
                        website = normalize_url(href)
                        break

        addr = (ld.get("address") or {}) if ld else {}
        city = addr.get("addressLocality") or job.city
        state = addr.get("addressRegion") or job.state
        specialty = ld.get("medicalSpecialty") if ld else None

        slug = url.rstrip("/").rsplit("/", 1)[-1]
        external_id = make_external_id("healthgrades", slug)

        if not phone and not website:
            return None

        return Lead(
            practice_name=name[:255],
            phone=phone,
            website=website,
            specialty=specialty,
            vertical_slug=job.vertical_slug,
            city=city[:120] if city else None,
            state=state[:60] if state else None,
            country="US",
            source="lead_harvester",
            source_detail=self.source_detail,
            external_id=external_id,
            raw={"hg_url": url, "jsonld": ld},
        )

    def _extract_jsonld(self, soup: BeautifulSoup) -> dict | None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(script.string or "")
            except Exception:
                continue
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                if item.get("@type") in _JSONLD_TYPES:
                    return item
        return None
