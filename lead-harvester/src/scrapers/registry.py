"""
Maps each vertical_slug → ordered list of scraper classes to try.
Scrapers run in order until target_count is reached.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseScraper


def get_scrapers_for_vertical(vertical_slug: str) -> list[type["BaseScraper"]]:
    # Import here to avoid circular imports and to keep startup fast
    from .yelp import YelpScraper
    from .healthgrades import HealthgradesScraper
    from .psychology_today import PsychologyTodayScraper
    from .opencare import OpencareScraper
    from .google_maps import GoogleMapsScraper

    REGISTRY: dict[str, list[type[BaseScraper]]] = {
        "dental": [
            HealthgradesScraper,
            YelpScraper,
            OpencareScraper,
        ],
        "mental-health": [
            PsychologyTodayScraper,
            HealthgradesScraper,
            YelpScraper,
        ],
        "primary-care": [
            HealthgradesScraper,
            YelpScraper,
        ],
        "med-spa": [
            YelpScraper,
            GoogleMapsScraper,
        ],
        "weight-loss": [
            HealthgradesScraper,
            YelpScraper,
        ],
        "house-call": [
            YelpScraper,
            GoogleMapsScraper,
        ],
    }
    return REGISTRY.get(vertical_slug, [YelpScraper])
