"""
OpenCare scraper — dental only.

URL pattern: https://www.opencare.com/dentists/{city-lower-hyphen}-{state-lower}/
Each listing card has: practice name, phone, website, address.

Rate: 2 s base + 0.5–1 s jitter.
"""
from __future__ import annotations
import json
import logging
import re

from bs4 import BeautifulSoup

from ..models import Job, Lead
from ..normalize import make_external_id, normalize_name, normalize_phone, normalize_url
from .base import BaseScraper

logger = logging.getLogger(__name__)


def _location_slug(city: str, state: str) -> str:
    city_slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    state_slug = state.lower().strip()[:2]
    return f"{city_slug}-{state_slug}"


class OpencareScraper(BaseScraper):
    source = "opencare"
    source_detail = "opencare_dental_listing"
    base_delay = 2.0
    jitter = (0.5, 1.0)
    max_failures = 5

    async def _do_scrape(self, job: Job) -> list[Lead]:
        slug = _location_slug(job.city, job.state)
        leads: list[Lead] = []

        async with self._make_client() as client:
            page = 1
            while len(leads) < job.target_count:
                url = (
                    f"https://www.opencare.com/dentists/{slug}/"
                    if page == 1
                    else f"https://www.opencare.com/dentists/{slug}/?page={page}"
                )
                resp = await self._safe_get(client, url)
                if resp is None:
                    break

                soup = BeautifulSoup(resp.text, "lxml")

                # Try JSON-LD ItemList first (structured data)
                ld_leads = self._extract_from_jsonld(soup, job)
                if ld_leads:
                    for lead in ld_leads:
                        if not self._is_excluded(lead.website, job.exclude_domains):
                            leads.append(lead)
                    if len(ld_leads) < 5:
                        break
                else:
                    # Fallback: parse HTML listing cards
                    cards = self._find_listing_cards(soup)
                    if not cards:
                        break
                    for card in cards:
                        lead = self._parse_card(card, job)
                        if lead and not self._is_excluded(lead.website, job.exclude_domains):
                            leads.append(lead)

                if not self._has_next_page(soup):
                    break
                page += 1

        return leads

    # ── JSON-LD path ───────────────────────────────────────────────────

    def _extract_from_jsonld(self, soup: BeautifulSoup, job: Job) -> list[Lead]:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "ItemList":
                    return [
                        self._jsonld_item_to_lead(el.get("item", el), job)
                        for el in item.get("itemListElement", [])
                        if el
                    ]
                if item.get("@type") in ("LocalBusiness", "Dentist", "MedicalBusiness"):
                    lead = self._jsonld_item_to_lead(item, job)
                    return [lead] if lead else []
        return []

    def _jsonld_item_to_lead(self, item: dict, job: Job) -> Lead | None:
        name = normalize_name(item.get("name"))
        if not name:
            return None

        phone = normalize_phone(item.get("telephone"))
        website = normalize_url(item.get("url"))

        addr = item.get("address") or {}
        city = addr.get("addressLocality") or job.city
        state = addr.get("addressRegion") or job.state

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        external_id = make_external_id("opencare", f"{slug}-{job.city.lower()}")

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
            raw={"jsonld": item},
        )

    # ── HTML path ─────────────────────────────────────────────────────

    def _find_listing_cards(self, soup: BeautifulSoup) -> list:
        return (
            soup.find_all("div", class_=re.compile(r"listing|practice|office|card", re.I))
            or soup.find_all("article")
        )

    def _parse_card(self, card, job: Job) -> Lead | None:
        name_tag = card.find(["h2", "h3", "h4"]) or card.find(
            class_=re.compile(r"name|title", re.I)
        )
        name = normalize_name(name_tag.get_text(strip=True)) if name_tag else None
        if not name:
            return None

        phone = None
        tel = card.find("a", href=re.compile(r"^tel:"))
        if tel:
            phone = normalize_phone(tel["href"].replace("tel:", ""))

        website = None
        for a in card.find_all("a", href=True):
            href = a["href"]
            if "opencare.com" not in href and href.startswith("http"):
                website = normalize_url(href)
                break

        if not phone and not website:
            return None

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        external_id = make_external_id("opencare", f"{slug}-{job.city.lower()}")

        return Lead(
            practice_name=name[:255],
            phone=phone,
            website=website,
            vertical_slug=job.vertical_slug,
            city=job.city[:120],
            state=job.state[:60],
            country="US",
            source="lead_harvester",
            source_detail=self.source_detail,
            external_id=external_id,
            raw={"card_text": card.get_text(" ", strip=True)[:500]},
        )

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        return bool(
            soup.find("a", rel="next")
            or soup.find("a", string=re.compile(r"next|›|»", re.I))
        )
