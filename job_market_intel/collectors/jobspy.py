"""JobSpy wrapper — covers Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs.

JobSpy returns a pandas DataFrame. We translate rows into our Listing dataclass.
Per-site failures (rate limit, DOM change) are captured as warnings, not raised.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from ..dedup import listing_hash
from ..models import Listing
from ..seeds import classify_role, is_us_location
from . import CollectorResult

# Sites jobspy supports. Pass a subset to limit scrapes.
JOBSPY_SITES = ("indeed", "linkedin", "glassdoor", "zip_recruiter", "google")


class JobSpyCollector:
    source_name = "jobspy"

    def __init__(self, *, sites: Iterable[str] = JOBSPY_SITES, country_indeed: str = "USA") -> None:
        self.sites = tuple(sites)
        self.country_indeed = country_indeed

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int = 14,
    ) -> CollectorResult:
        try:
            from jobspy import scrape_jobs  # type: ignore[import-not-found]
        except ImportError:
            return CollectorResult(
                warnings=["python-jobspy not installed; skipping jobspy collector."],
                source_name=self.source_name,
            )

        out = CollectorResult(source_name=self.source_name)
        fetched_at = _now_iso()

        # 0 (or any sentinel-y value) means "pull as many as the backend will give".
        # JobSpy backends each have an internal cap (typically ~1000) so 10_000 is
        # effectively unlimited.
        wanted = results_per_query if 0 < results_per_query < 100_000 else 10_000
        hours_old = max(1, freshness_days) * 24

        for query in queries:
            for site in self.sites:
                try:
                    df = scrape_jobs(
                        site_name=[site],
                        search_term=query,
                        location=location,
                        results_wanted=wanted,
                        country_indeed=self.country_indeed,
                        # JobSpy filters at the source so we only fetch fresh posts.
                        hours_old=hours_old,
                        verbose=0,
                    )
                except Exception as exc:
                    out.warnings.append(f"jobspy/{site} failed for query '{query}': {exc}")
                    continue

                if df is None or df.empty:
                    continue

                for _, row in df.iterrows():
                    listing = _row_to_listing(row, site=site, fetched_at=fetched_at)
                    if listing is not None:
                        out.listings.append(listing)

        return out


def _row_to_listing(row, *, site: str, fetched_at: str) -> Listing | None:
    title = _safe_str(row.get("title"))
    company = _safe_str(row.get("company"))
    if not title or not company:
        return None

    location = _safe_str(row.get("location"))
    # Belt-and-suspenders US-only filter. Even though we pass country_indeed="USA"
    # and location="United States" to scrape_jobs, JobSpy occasionally returns
    # foreign roles for the same company.
    if not is_us_location(location):
        return None

    description = _safe_str(row.get("description"))
    job_url = _safe_str(row.get("job_url"))
    posted_at = _safe_iso(row.get("date_posted"))

    return Listing(
        listing_id=listing_hash(company, title, location),
        title=title,
        company=company,
        location=location,
        description=description,
        role_bucket=classify_role(title),
        sources=[site],
        source_urls=[job_url] if job_url else [],
        posted_at=posted_at,
        fetched_at=fetched_at,
    )


def _safe_str(value) -> str:
    if value is None:
        return ""
    try:
        # pandas NaN check without depending on math.isnan import (NaN != NaN).
        if isinstance(value, float) and value != value:
            return ""
        return str(value).strip()
    except Exception:
        return ""


def _safe_iso(value) -> str | None:
    s = _safe_str(value)
    if not s:
        return None
    # jobspy returns dates like "2026-05-18" or pd.Timestamp; keep best-effort
    return s


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
