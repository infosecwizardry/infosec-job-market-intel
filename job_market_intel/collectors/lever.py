"""Lever public board collector — free, key-free JSON per company.

Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape

import httpx

from ..dedup import listing_hash
from ..models import Listing
from ..seeds import classify_role, is_us_location
from . import CollectorResult

LEVER_URL = "https://api.lever.co/v0/postings/{slug}"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class LeverCollector:
    source_name = "lever"

    def __init__(self, *, company_slugs: list[str], timeout: float = 30.0) -> None:
        self.company_slugs = company_slugs
        self.timeout = timeout

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int = 14,
    ) -> CollectorResult:
        # Filter by bucket-classification keywords (not search query phrases).
        # See greenhouse.py for the rationale. Lever's public API has no date
        # filter; freshness_days is handled by the cross-source post-filter.
        _ = freshness_days
        out = CollectorResult(source_name=self.source_name)
        fetched_at = _now_iso()

        with httpx.Client(timeout=self.timeout) as client:
            for slug in self.company_slugs:
                url = LEVER_URL.format(slug=slug)
                try:
                    response = client.get(url, params={"mode": "json"})
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    out.warnings.append(f"lever/{slug} failed: {exc}")
                    continue

                payload = response.json()
                if not isinstance(payload, list):
                    continue

                for job in payload:
                    title = (job.get("text") or "").strip()
                    if not title:
                        continue
                    if classify_role(title) == "unclassified":
                        continue

                    location_str = (job.get("categories", {}) or {}).get("location", "")
                    if not is_us_location(location_str):
                        continue

                    description_html = job.get("descriptionPlain") or job.get("description") or ""
                    description = _strip_html(unescape(description_html))
                    company_name = (job.get("categories", {}) or {}).get("team", "") or slug
                    url_value = job.get("hostedUrl") or job.get("applyUrl") or ""

                    created_at = job.get("createdAt")
                    posted_at = None
                    if isinstance(created_at, int | float) and created_at > 0:
                        try:
                            posted_at = (
                                datetime.fromtimestamp(created_at / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
                            )
                        except (OSError, OverflowError, ValueError):
                            posted_at = None

                    out.listings.append(
                        Listing(
                            listing_id=listing_hash(company_name, title, location_str),
                            title=title,
                            company=company_name,
                            location=location_str,
                            description=description,
                            role_bucket=classify_role(title),
                            sources=["lever"],
                            source_urls=[url_value] if url_value else [],
                            posted_at=posted_at,
                            fetched_at=fetched_at,
                        )
                    )

        return out


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html or "")).strip()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
