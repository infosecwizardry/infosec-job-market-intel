"""The Muse collector — free public REST API.

Docs: https://www.themuse.com/developers/api/v2
Rate limits: 3,600 req/hr with a free registered key, 500 req/hr without.
Either is ample — the whole "Computer and IT" category is ~1,700 jobs
(~87 pages at 20/page, verified live 2026-06-11).

The Muse's category taxonomy has NO cybersecurity or help-desk category
(?category=Cybersecurity returns total=0), so we pull the general
"Computer and IT" pool and let the pipeline's title classifier bucket it.
Listings are international; we keep US locations plus "Flexible / Remote".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape

import httpx

from ..dedup import listing_hash
from ..models import Listing
from ..seeds import classify_role
from . import CollectorResult

API_URL = "https://www.themuse.com/api/public/jobs"
CATEGORY = "Computer and IT"

# Defensive page cap — the category is ~87 pages today; 200 leaves headroom
# without risking a runaway loop if the API's page_count misreports.
MAX_PAGES = 200

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# The Muse marks fully-remote postings with this special location name.
_REMOTE_LOCATION = "flexible / remote"

# POSITIVE US evidence required. The shared `seeds.is_us_location` helper
# defaults ambiguous strings to "include" — right for JobSpy (US-constrained
# searches) but wrong here: The Muse is an international board and renders
# locations as "City, Country" with no country code, so "Kazanlak, Bulgaria"
# reads as ambiguous and would leak through (caught in the live smoke test).
# Muse's US locations are uniformly "City, ST" — match the state code.
_US_STATE_SUFFIX_RE = re.compile(
    r",\s*(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|"
    r"N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\s*$"
)


def _is_positively_us(name: str) -> bool:
    if "united states" in name.lower():
        return True
    return bool(_US_STATE_SUFFIX_RE.search(name))


class TheMuseCollector:
    source_name = "themuse"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`api_key=None` still works (500 req/hr anonymous tier).
        `transport` is for tests (httpx.MockTransport)."""
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int = 14,
    ) -> CollectorResult:
        """Pull the full Computer-and-IT category once; `queries` are not
        passed to the API (it has no keyword search) — the pipeline's title
        classifier does the role filtering downstream.

        `results_per_query` acts as a soft cap on TOTAL listings returned.
        `freshness_days` is applied client-side on publication_date.
        """
        out = CollectorResult(source_name=self.source_name)
        fetched_at = _now_iso()
        soft_cap = results_per_query if 0 < results_per_query < 100_000 else 100_000
        cutoff = _freshness_cutoff(freshness_days)

        remote_only = bool(location) and location.lower() == "remote"

        # transport=None means "use the default transport" — no conditional needed.
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            page = 1
            page_count = 1  # discovered from the first response
            while page <= min(page_count, MAX_PAGES) and len(out.listings) < soft_cap:
                params: dict[str, str | int] = {"category": CATEGORY, "page": page}
                if self.api_key:
                    params["api_key"] = self.api_key
                try:
                    response = client.get(API_URL, params=params)
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    out.warnings.append(f"themuse page {page} failed: {exc}")
                    break

                page_count = int(payload.get("page_count") or 1)
                results = payload.get("results") or []
                if not results:
                    break

                for job in results:
                    if len(out.listings) >= soft_cap:
                        break
                    listing = _job_to_listing(
                        job,
                        fetched_at=fetched_at,
                        cutoff=cutoff,
                        remote_only=remote_only,
                    )
                    if listing is not None:
                        out.listings.append(listing)

                page += 1

        return out


def _job_to_listing(job: dict, *, fetched_at: str, cutoff: str | None, remote_only: bool) -> Listing | None:
    title = (job.get("name") or "").strip()
    company = ((job.get("company") or {}).get("name") or "").strip()
    if not title or not company:
        return None

    posted_at = (job.get("publication_date") or "").strip() or None
    if cutoff and posted_at and posted_at[:10] < cutoff:
        return None  # stale — saves the pipeline a post-filter pass

    location = _pick_us_location(job.get("locations") or [], remote_only=remote_only)
    if location is None:
        return None  # non-US listing (The Muse is international)

    description = _strip_html(unescape(job.get("contents") or ""))
    # The API exposes its own seniority taxonomy — append it so the
    # description-based seniority classifier sees it as listing metadata.
    levels = [(lv.get("name") or "").strip() for lv in (job.get("levels") or []) if isinstance(lv, dict)]
    levels = [lv for lv in levels if lv]
    if levels:
        description = f"{description}\n\nLevel: {', '.join(levels)}"

    url = ((job.get("refs") or {}).get("landing_page") or "").strip()

    return Listing(
        listing_id=listing_hash(company, title, location),
        title=title,
        company=company,
        location=location,
        description=description,
        role_bucket=classify_role(title),
        sources=["themuse"],
        source_urls=[url] if url else [],
        posted_at=posted_at,
        fetched_at=fetched_at,
    )


def _pick_us_location(locations: list, *, remote_only: bool) -> str | None:
    """Return a US location string, 'Remote' for Flexible/Remote postings,
    or None when the posting is entirely non-US."""
    names = [(loc.get("name") or "").strip() for loc in locations if isinstance(loc, dict)]
    names = [n for n in names if n]

    is_remote = any(n.lower() == _REMOTE_LOCATION for n in names)
    us_names = [n for n in names if n.lower() != _REMOTE_LOCATION and _is_positively_us(n)]

    if remote_only:
        return "Remote" if is_remote else None
    if us_names:
        return us_names[0]
    if is_remote:
        return "Remote"
    return None


def _freshness_cutoff(freshness_days: int) -> str | None:
    if freshness_days <= 0:
        return None
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(days=freshness_days)).date().isoformat()


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html or "")).strip()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
