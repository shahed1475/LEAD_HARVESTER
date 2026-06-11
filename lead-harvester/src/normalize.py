from __future__ import annotations
import re
from urllib.parse import urlparse, urlunparse


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    # Must have at least 7 digits to be a real phone number
    if len(digits) < 7:
        return None
    return digits


def normalize_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Skip non-HTTP links (tel:, mailto:, javascript:)
    if raw.startswith(("tel:", "mailto:", "javascript:")):
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        if not parsed.netloc:
            return None
        # Strip trailing slash from bare root path
        path = parsed.path if parsed.path not in ("", "/") else ""
        return urlunparse(parsed._replace(path=path, fragment=""))
    except Exception:
        return None


def normalize_name(raw: str | None) -> str | None:
    if not raw:
        return None
    # Collapse whitespace and strip
    return " ".join(raw.strip().split()) or None


def make_external_id(source: str, identifier: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", identifier.lower()).strip("-")
    return f"{source}:{slug}"


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        netloc = urlparse(url).netloc
        return netloc.lstrip("www.").lower() or None
    except Exception:
        return None
