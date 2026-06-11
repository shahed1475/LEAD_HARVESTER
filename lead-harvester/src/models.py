from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class VerticalSlug(str, Enum):
    PRIMARY_CARE = "primary-care"
    MENTAL_HEALTH = "mental-health"
    DENTAL = "dental"
    MED_SPA = "med-spa"
    WEIGHT_LOSS = "weight-loss"
    HOUSE_CALL = "house-call"


class Job(BaseModel):
    id: int
    vertical_slug: str
    state: str
    city: str
    target_count: int = 100
    exclude_domains: list[str] = Field(default_factory=list)
    notes: str | None = None


class Lead(BaseModel):
    # Required
    practice_name: str = Field(..., max_length=255)

    # At least one contact channel must be present
    email: str | None = None
    phone: str | None = None

    # Strongly recommended
    name: str | None = Field(None, max_length=255)
    website: str | None = None
    specialty: str | None = None
    vertical_slug: str | None = None
    city: str | None = Field(None, max_length=120)
    state: str | None = Field(None, max_length=60)
    country: str = Field("US", max_length=60)

    # Optional extras
    linkedin_url: str | None = Field(None, max_length=512)
    founded_year: int | None = None
    notes: str | None = Field(None, max_length=2000)

    # Always set by scrapers
    source: str = "lead_harvester"
    source_detail: str | None = None
    external_id: str | None = None
    raw: dict[str, Any] | None = None

    def has_contact(self) -> bool:
        return bool(self.email or self.phone)


class IngestError(BaseModel):
    index: int
    reason: str


class IngestResponse(BaseModel):
    job_id: int
    received: int
    created: int = 0
    updated: int = 0
    duplicates: int = 0
    failed: int = 0
    errors: list[IngestError] = Field(default_factory=list)
