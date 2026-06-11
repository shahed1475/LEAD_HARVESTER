"""
Psychology Today scraper — mental-health only.

Strategy:
  URL: https://www.psychologytoday.com/us/therapists/{state_lower}/{city_slug}
  Each page shows ~20 therapist profile cards.
  Paginate via ?page=N.
  Each card has: name, credentials, phone (sometimes), website link, specialties, location.

Rate: 2 s base + 0.5–1 s jitter.
"""
from __future__ import annotations
import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

from ..models import Job, Lead
from ..normalize import make_external_id, normalize_name, normalize_phone, normalize_url
from .base import BaseScraper

logger = logging.getLogger(__name__)

_STATE_ABBR_TO_SLUG: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina",
    "SD": "south-dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district-of-columbia",
}


def _state_slug(state: str) -> str:
    state = state.strip()
    if len(state) == 2:
        return _STATE_ABBR_TO_SLUG.get(state.upper(), state.lower())
    return state.lower().replace(" ", "-")


def _city_slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")


class PsychologyTodayScraper(BaseScraper):
    source = "psychology_today"
    source_detail = "psychology_today_profile"
    base_delay = 2.0
    jitter = (0.5, 1.0)
    max_failures = 5

    async def _do_scrape(self, job: Job) -> list[Lead]:
        state = _state_slug(job.state)
        city = _city_slug(job.city)
        base_url = f"https://www.psychologytoday.com/us/therapists/{state}/{city}"

        leads: list[Lead] = []

        async with self._make_client() as client:
            page = 1
            while len(leads) < job.target_count:
                url = f"{base_url}?page={page}" if page > 1 else base_url
                resp = await self._safe_get(client, url)
                if resp is None:
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                cards = self._find_profile_cards(soup)
                if not cards:
                    break

                for card in cards:
                    lead = self._parse_card(card, job)
                    if lead and not self._is_excluded(lead.website, job.exclude_domains):
                        leads.append(lead)
                    if len(leads) >= job.target_count:
                        break

                # Check if there's a next page
                if not self._has_next_page(soup):
                    break
                page += 1

        return leads

    # ── Parsing helpers ────────────────────────────────────────────────

    def _find_profile_cards(self, soup: BeautifulSoup) -> list:
        # PT uses <div class="profile-results-list"> with <article> children
        # Try multiple selectors for resilience
        cards = soup.find_all("article", class_=re.compile(r"profile|listing", re.I))
        if not cards:
            # Fallback: any <div> with a profile link
            cards = soup.find_all("div", attrs={"data-provider-id": True})
        return cards

    def _parse_card(self, card, job: Job) -> Lead | None:
        # Name — in an <a> or <h2> / <h3>
        name_tag = (
            card.find("a", class_=re.compile(r"profile.name|name", re.I))
            or card.find(["h2", "h3"])
        )
        name = normalize_name(name_tag.get_text(strip=True)) if name_tag else None
        if not name:
            return None

        # Profile URL (for external_id)
        profile_url = None
        if name_tag and name_tag.name == "a":
            href = name_tag.get("href", "")
            if href.startswith("/"):
                profile_url = f"https://www.psychologytoday.com{href}"
            elif href.startswith("http"):
                profile_url = href

        # Phone — <a href="tel:...">
        phone = None
        tel = card.find("a", href=re.compile(r"^tel:"))
        if tel:
            phone = normalize_phone(tel["href"].replace("tel:", ""))

        # Website
        website = None
        for a in card.find_all("a", href=True):
            href = a["href"]
            if "psychologytoday.com" not in href and href.startswith("http"):
                text = a.get_text(strip=True).lower()
                if any(k in text for k in ("website", "visit", "www")):
                    website = normalize_url(href)
                    break

        # Specialties / tags
        tags = card.find_all(class_=re.compile(r"tag|issue|specialty", re.I))
        specialty = ", ".join(t.get_text(strip=True) for t in tags[:5]) or None

        # Location
        loc_tag = card.find(class_=re.compile(r"location|city|address", re.I))
        city = job.city
        state = job.state
        if loc_tag:
            loc_text = loc_tag.get_text(strip=True)
            if "," in loc_text:
                parts = loc_text.split(",")
                city = parts[0].strip() or city
                state = parts[1].strip() or state

        # Must have some contact channel
        if not phone and not website:
            return None

        slug = (
            profile_url.rstrip("/").rsplit("/", 1)[-1]
            if profile_url
            else re.sub(r"[^a-z0-9]+", "-", name.lower())
        )
        external_id = make_external_id("ptoday", slug)

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
            raw={"profile_url": profile_url, "card_text": card.get_text(" ", strip=True)[:500]},
        )

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        next_link = soup.find("a", rel="next") or soup.find(
            "a", string=re.compile(r"next|›|»", re.I)
        )
        return next_link is not None
