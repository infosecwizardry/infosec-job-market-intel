"""USAJobs.gov collector — free official REST API.

Auth is via two headers: User-Agent (your registered email) and Authorization-Key.
Sign up: https://developer.usajobs.gov/

We treat keyword queries as straight `Keyword` searches; geo is `LocationName`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ..dedup import listing_hash
from ..models import Listing
from ..seeds import classify_role
from . import CollectorResult

USAJOBS_HOST = "data.usajobs.gov"
USAJOBS_URL = f"https://{USAJOBS_HOST}/api/Search"

# Hard cap on pages per query — defensive against runaway pagination if the
# API misreports SearchResultCountAll. 20 pages * 500/page = 10k jobs per
# query — far beyond what any one search realistically returns.
MAX_PAGES_PER_QUERY = 20
MAX_RESULTS_PER_PAGE = 500


class USAJobsCollector:
    source_name = "usajobs"

    def __init__(self, *, email: str, api_key: str, timeout: float = 30.0) -> None:
        self.email = email
        self.api_key = api_key
        self.timeout = timeout

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int = 14,
    ) -> CollectorResult:
        """Paginate through ALL active US federal listings for each query.

        `results_per_query` becomes a soft cap on total returned per query;
        pass 0 (or anything >= 10000) to mean 'no cap'.
        `freshness_days` is passed to the API as `DatePosted` (1-60 valid).
        """
        out = CollectorResult(source_name=self.source_name)
        fetched_at = _now_iso()
        today_iso = datetime.now(UTC).date().isoformat()

        headers = {
            "Host": USAJOBS_HOST,
            "User-Agent": self.email,
            "Authorization-Key": self.api_key,
        }
        # Soft cap interpretation: <= 0 means unlimited.
        soft_cap = results_per_query if 0 < results_per_query < 100_000 else 100_000
        # USAJobs `DatePosted` accepts 1-60; clamp to that range.
        date_posted = max(1, min(int(freshness_days), 60))

        with httpx.Client(headers=headers, timeout=self.timeout) as client:
            for query in queries:
                collected_for_query = 0
                for page in range(1, MAX_PAGES_PER_QUERY + 1):
                    if collected_for_query >= soft_cap:
                        break
                    params = {
                        "Keyword": query,
                        "ResultsPerPage": MAX_RESULTS_PER_PAGE,
                        "Page": page,
                        "DatePosted": date_posted,
                        "LocationName": location if location and location.lower() != "remote" else "",
                        "RemoteIndicator": "True" if location and location.lower() == "remote" else "",
                    }
                    params = {k: v for k, v in params.items() if v != ""}
                    try:
                        response = client.get(USAJOBS_URL, params=params)
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        out.warnings.append(f"usajobs page {page} failed for '{query}': {exc}")
                        break

                    payload = response.json()
                    search_result = payload.get("SearchResult", {})
                    items = search_result.get("SearchResultItems", [])
                    if not items:
                        break  # ran past the end

                    added_this_page = 0
                    for item in items:
                        if collected_for_query >= soft_cap:
                            break
                        listing = _item_to_listing(item, fetched_at=fetched_at, today_iso=today_iso)
                        if listing is not None:
                            out.listings.append(listing)
                            collected_for_query += 1
                            added_this_page += 1

                    total_pages = int(search_result.get("SearchResultCountAll", 0) or 0) // MAX_RESULTS_PER_PAGE + 1
                    # Stop when we've consumed all pages the API reports, or this page came back short.
                    if page >= total_pages or len(items) < MAX_RESULTS_PER_PAGE:
                        break

        return out


def _item_to_listing(item: dict, *, fetched_at: str, today_iso: str) -> Listing | None:
    descriptor = item.get("MatchedObjectDescriptor", {})
    title = (descriptor.get("PositionTitle") or "").strip()
    company = (descriptor.get("OrganizationName") or descriptor.get("DepartmentName") or "USAJobs").strip()
    if not title:
        return None

    # Active-only filter: drop listings whose ApplicationCloseDate is already past.
    close_date = descriptor.get("ApplicationCloseDate", "")
    if close_date and close_date[:10] < today_iso:
        return None

    locations = descriptor.get("PositionLocationDisplay", "")
    description = _build_description(descriptor)
    url = descriptor.get("PositionURI") or ""
    posted_at = descriptor.get("PublicationStartDate")

    return Listing(
        listing_id=listing_hash(company, title, locations),
        title=title,
        company=company,
        location=locations,
        description=description,
        role_bucket=classify_role(title),
        sources=["usajobs"],
        source_urls=[url] if url else [],
        posted_at=posted_at,
        fetched_at=fetched_at,
    )


def _build_description(descriptor: dict) -> str:
    parts: list[str] = []
    qualifications = descriptor.get("QualificationSummary")
    if qualifications:
        parts.append(qualifications)
    user_area = descriptor.get("UserArea", {}).get("Details", {})
    for key in ("JobSummary", "MajorDuties", "Education", "Requirements"):
        value = user_area.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v)
        elif value:
            parts.append(str(value))
    return "\n\n".join(parts)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
