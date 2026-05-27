"""Greenhouse public board collector.

Greenhouse exposes a free, key-free JSON endpoint per company:
    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

We post-filter by keyword (lowercased substring) since the API has no full-text search.
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

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class GreenhouseCollector:
    source_name = "greenhouse"

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
        # We use bucket-classification keywords rather than the literal search
        # query strings for filtering. Greenhouse's API has no full-text search,
        # so we paginate all jobs and post-filter — broader keywords mean we
        # catch titles like "Security Operations Engineer" (matches "security
        # operations") that wouldn't match a 4-word search phrase like
        # "security operations analyst".
        # The Greenhouse public API has no date filter; freshness_days is
        # handled by the cross-source post-filter in pipeline.py.
        _ = freshness_days  # documented as ignored here
        out = CollectorResult(source_name=self.source_name)
        fetched_at = _now_iso()

        with httpx.Client(timeout=self.timeout) as client:
            for slug in self.company_slugs:
                url = GREENHOUSE_URL.format(slug=slug)
                try:
                    response = client.get(url, params={"content": "true"})
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    out.warnings.append(f"greenhouse/{slug} failed: {exc}")
                    continue

                payload = response.json()
                for job in payload.get("jobs", []) or []:
                    title = (job.get("title") or "").strip()
                    if not title:
                        continue
                    if classify_role(title) == "unclassified":
                        continue

                    location_str = (job.get("location") or {}).get("name", "")
                    if not is_us_location(location_str):
                        continue

                    content_html = job.get("content") or ""
                    description = _strip_html(unescape(content_html))
                    company_name = job.get("company_name") or job.get("departments", [{}])[0].get("name", "") or slug
                    url_value = job.get("absolute_url") or ""

                    out.listings.append(
                        Listing(
                            listing_id=listing_hash(company_name, title, location_str),
                            title=title,
                            company=company_name,
                            location=location_str,
                            description=description,
                            role_bucket=classify_role(title),
                            sources=["greenhouse"],
                            source_urls=[url_value] if url_value else [],
                            posted_at=job.get("updated_at"),
                            fetched_at=fetched_at,
                        )
                    )

        return out


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html or "")).strip()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
