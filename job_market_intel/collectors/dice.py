"""Dice.com collector — tech-specialist job board, no API key required.

Dice has no public API, but its search results are server-rendered HTML and
each job-detail page embeds a schema.org JobPosting JSON-LD block
(`<script id="jobDetailStructuredData">`) with the full description, company,
location, and posting date. We paginate the search, collect detail URLs, then
fetch each detail page and parse the structured data — no CSS-selector
scraping of visual markup, so cosmetic redesigns don't break us.

Throttled to ~33 requests/minute (Cloudflare tolerance observed ~35/min/IP).
Live-verified 2026-06-11: "help desk" => 1,414 US results, "SOC analyst" =>
165, plain HTTP with a browser User-Agent, no challenge page.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from html import unescape

import httpx

from ..dedup import listing_hash
from ..models import Listing
from ..seeds import classify_role, is_us_location
from . import CollectorResult

SEARCH_URL = "https://www.dice.com/jobs"

# Detail links on the search page. The GUID-style path segment is stable
# across Dice's frontend rewrites (observed in both legacy and current UIs).
_DETAIL_URL_RE = re.compile(r'href="(https://www\.dice\.com/job-detail/[a-f0-9-]{36})"')

# The JobPosting JSON-LD block on detail pages. Attribute order can vary, so
# match on the id= attribute rather than position.
_STRUCTURED_DATA_RE = re.compile(
    r'<script[^>]*id="jobDetailStructuredData"[^>]*>(.*?)</script>',
    re.DOTALL,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Defensive page cap per query: 50 pages x 20 results = 1,000 listings —
# beyond any single query's realistic US yield (largest observed: 1,414
# across ALL help-desk phrasings combined).
MAX_PAGES_PER_QUERY = 50
PAGE_SIZE = 20

# Browser-like UA — Dice serves the server-rendered page to anything that
# looks like a browser; the default httpx UA gets a JS-shell response.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class DiceCollector:
    source_name = "dice"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        throttle_seconds: float = 1.8,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`throttle_seconds=1.8` keeps us at ~33 req/min, under Dice's
        observed ~35/min/IP Cloudflare threshold. `transport` is for tests
        (httpx.MockTransport)."""
        self.timeout = timeout
        self.throttle_seconds = throttle_seconds
        self.transport = transport

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int = 14,
    ) -> CollectorResult:
        out = CollectorResult(source_name=self.source_name)
        fetched_at = _now_iso()
        soft_cap = results_per_query if 0 < results_per_query < 100_000 else 100_000

        # Detail pages already fetched this run — queries overlap heavily
        # ("help desk" vs "IT support") and each detail fetch costs a request.
        seen_urls: set[str] = set()

        # transport=None means "use the default transport" — no conditional needed.
        with httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            for query in queries:
                collected_for_query = 0
                for page in range(1, MAX_PAGES_PER_QUERY + 1):
                    if collected_for_query >= soft_cap:
                        break
                    params: dict[str, str | int] = {
                        "q": query,
                        "countryCode": "US",
                        "page": page,
                        "pageSize": PAGE_SIZE,
                    }
                    # The pipeline's second pass uses location="Remote" —
                    # Dice models that as a workplace-type filter, not a geo.
                    if location and location.lower() == "remote":
                        params["workplaceTypes"] = "Remote"
                    elif location:
                        params["location"] = location

                    try:
                        response = client.get(SEARCH_URL, params=params)
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        out.warnings.append(f"dice search page {page} failed for '{query}': {exc}")
                        break

                    detail_urls = list(dict.fromkeys(_DETAIL_URL_RE.findall(response.text)))
                    if not detail_urls:
                        break  # ran past the last page

                    for url in detail_urls:
                        if collected_for_query >= soft_cap:
                            break
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)

                        self._throttle()
                        listing = self._fetch_detail(client, url, fetched_at=fetched_at, warnings=out.warnings)
                        if listing is not None:
                            out.listings.append(listing)
                            collected_for_query += 1

                    if len(detail_urls) < PAGE_SIZE:
                        break  # short page == last page
                    self._throttle()

        return out

    def _throttle(self) -> None:
        if self.throttle_seconds > 0:
            time.sleep(self.throttle_seconds)

    def _fetch_detail(
        self,
        client: httpx.Client,
        url: str,
        *,
        fetched_at: str,
        warnings: list[str],
    ) -> Listing | None:
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            warnings.append(f"dice detail fetch failed for {url}: {exc}")
            return None

        match = _STRUCTURED_DATA_RE.search(response.text)
        if not match:
            warnings.append(f"dice detail page had no structured data: {url}")
            return None

        try:
            posting = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            warnings.append(f"dice structured data unparseable for {url}: {exc}")
            return None

        return _posting_to_listing(posting, url=url, fetched_at=fetched_at)


def _posting_to_listing(posting: dict, *, url: str, fetched_at: str) -> Listing | None:
    title = (posting.get("title") or "").strip()
    org = posting.get("hiringOrganization") or {}
    company = (org.get("name") or "").strip() if isinstance(org, dict) else ""
    if not title or not company:
        return None

    location = _format_location(posting.get("jobLocation"))
    # countryCode=US constrains the search, but belt-and-suspenders like the
    # JobSpy collector — Dice occasionally lists foreign roles for US firms.
    if location and not is_us_location(location):
        return None

    description = _strip_html(unescape(posting.get("description") or ""))
    posted_at = (posting.get("datePosted") or "").strip() or None

    return Listing(
        listing_id=listing_hash(company, title, location),
        title=title,
        company=company,
        location=location,
        description=description,
        role_bucket=classify_role(title),
        sources=["dice"],
        source_urls=[url],
        posted_at=posted_at,
        fetched_at=fetched_at,
    )


def _format_location(job_location) -> str:
    """schema.org jobLocation: a Place dict or a list of them."""
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if not isinstance(job_location, dict):
        return ""
    address = job_location.get("address") or {}
    if not isinstance(address, dict):
        return ""
    parts = [
        (address.get("addressLocality") or "").strip(),
        (address.get("addressRegion") or "").strip(),
        (address.get("addressCountry") or "US").strip(),
    ]
    return ", ".join(p for p in parts if p)


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html or "")).strip()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
