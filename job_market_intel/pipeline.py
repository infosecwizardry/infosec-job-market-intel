"""End-to-end orchestration: collect → dedup → extract → score → report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

from . import __version__
from .collectors import Collector, CollectorResult
from .dedup import dedup_listings
from .extract import regex_rules
from .extract.llm import ClaudeExtractor
from .models import Listing
from .reporting import (
    append_trend_csv,
    render_markdown_report,
    stats_to_dict,
    write_listings_csv,
    write_snapshot_json,
)
from .scoring import BucketStats, tabulate
from .seeds import (
    ROLE_SEEDS,
    all_seed_phrases,
    classify_seniority_combined,
    is_bureaucratic_metadata_only,
    is_compliance_role,
    is_physical_security_role,
)


@dataclass(slots=True)
class PipelineOptions:
    location: str = "United States"
    also_remote: bool = True
    # 0 = unlimited (pull everything each source will give). Non-zero = soft cap.
    results_per_source: int = 0
    # Cross-source freshness filter applied after collection + dedup. Listings
    # whose posted_at parses to >freshness_days old are dropped. Listings with
    # missing/unparseable posted_at are KEPT (Greenhouse often omits the field).
    freshness_days: int = 14
    # Drop listings whose description body is shorter than this. LinkedIn via
    # JobSpy often returns the title + company but no description body —
    # those listings can't be classified or analyzed downstream so they're
    # noise. Set to 0 to keep everything.
    min_description_chars: int = 50
    # Seniority filter — drops senior/leadership noise so the dataset reflects
    # what an entry-level candidate would actually apply to. Defaults to
    # entry + unclear (bare "SOC Analyst" with no level modifier is typically
    # entry-bracket at most companies).
    allowed_seniority: list[str] = field(default_factory=lambda: ["entry", "unclear"])
    # Drop listings the title classifier couldn't bucket. Most are off-topic
    # noise from JobSpy full-text matches (e.g. "Software Engineer" listings
    # that mention "SOC" in the description). Toggle on if you want to see them.
    include_unclassified: bool = False
    role_buckets: list[str] = field(default_factory=lambda: ["junior_soc", "help_desk_it_admin"])
    use_llm: bool = True
    cache_dir: Path = field(default_factory=lambda: Path("cache"))
    output_dir: Path = field(default_factory=lambda: Path("reports"))
    dry_run: bool = False
    today: str | None = None  # YYYY-MM-DD override for deterministic tests


class Pipeline:
    def __init__(
        self,
        *,
        collectors: list[Collector],
        llm_extractor: ClaudeExtractor | None,
        options: PipelineOptions,
    ) -> None:
        self.collectors = collectors
        self.llm_extractor = llm_extractor
        self.options = options

    def run(self) -> dict:
        started = perf_counter()
        generated_at = _now_iso()
        date_str = self.options.today or generated_at[:10]
        warnings: list[str] = []

        queries = self._queries_for_buckets()
        if not queries:
            raise ValueError("No queries — at least one role bucket must be enabled.")

        # Collect from each source. Sources that fail entirely log a warning but don't abort.
        raw_listings: list[Listing] = []
        per_source_counts: dict[str, int] = {}
        for collector in self.collectors:
            result = self._safe_collect(collector, queries=queries)
            warnings.extend(result.warnings)
            per_source_counts[collector.source_name] = len(result.listings)
            raw_listings.extend(result.listings)

        # Remote pass: re-run with "Remote" location to catch postings tagged remote-only.
        if self.options.also_remote:
            for collector in self.collectors:
                result = self._safe_collect(collector, queries=queries, location_override="Remote")
                warnings.extend(result.warnings)
                per_source_counts[collector.source_name] = per_source_counts.get(collector.source_name, 0) + len(
                    result.listings
                )
                raw_listings.extend(result.listings)

        deduped = dedup_listings(raw_listings)

        # Physical-security disambiguator: re-label SOC titles whose descriptions
        # are clearly about physical (not cyber) security as 'unclassified'.
        physical_security_relabeled = 0
        for listing in deduped:
            if listing.role_bucket == "junior_soc" and is_physical_security_role(listing.description):
                listing.role_bucket = "unclassified"  # type: ignore[assignment]
                physical_security_relabeled += 1

        # Compliance/GRC disambiguator: "Information Security Analyst" postings
        # often match the SOC keyword but describe GRC/audit work. Demote those.
        compliance_relabeled = 0
        for listing in deduped:
            if listing.role_bucket == "junior_soc" and is_compliance_role(listing.description):
                listing.role_bucket = "unclassified"  # type: ignore[assignment]
                compliance_relabeled += 1

        # Role-bucket filter — drop off-topic listings. include_unclassified
        # toggles whether titles the classifier couldn't bucket survive.
        allowed_buckets = set(self.options.role_buckets)
        if self.options.include_unclassified:
            allowed_buckets.add("unclassified")
        pre_bucket_count = len(deduped)
        deduped = [listing for listing in deduped if listing.role_bucket in allowed_buckets]
        dropped_off_topic = pre_bucket_count - len(deduped)

        # Drop listings with no usable description body. Some sources (notably
        # LinkedIn via JobSpy) return titles and companies but fail to fetch the
        # body; those listings can't be classified by description regex or LLM
        # and produce no useful requirements/skills data downstream.
        pre_nodesc = len(deduped)
        min_chars = self.options.min_description_chars
        if min_chars > 0:
            deduped = [li for li in deduped if len((li.description or "").strip()) >= min_chars]
        # Also drop bureaucratic-empty listings — civil-service / school-district
        # postings whose entire body is metadata fields with no actual job content.
        deduped = [li for li in deduped if not is_bureaucratic_metadata_only(li.description)]
        dropped_no_description = pre_nodesc - len(deduped)

        # Cross-source freshness filter. See _is_fresh() for the parse policy.
        pre_freshness_count = len(deduped)
        deduped = [listing for listing in deduped if _is_fresh(listing.posted_at, self.options.freshness_days)]
        dropped_stale = pre_freshness_count - len(deduped)

        # Extraction: regex first (always), then optional LLM enrichment.
        # If the LLM extractor exposes enrich_many we use it (concurrent batch);
        # otherwise fall back to a serial loop.
        llm_skipped = 0
        bases = [regex_rules.extract(li.description) for li in deduped]
        if self.options.use_llm and self.llm_extractor is not None:
            if hasattr(self.llm_extractor, "enrich_many"):
                enriched_list, llm_warnings = self.llm_extractor.enrich_many(deduped, bases)
                warnings.extend(llm_warnings)
                for listing, enriched in zip(deduped, enriched_list, strict=False):
                    listing.extracted = enriched
            else:
                for listing, base in zip(deduped, bases, strict=False):
                    enriched, llm_warnings = self.llm_extractor.enrich(listing, base)
                    listing.extracted = enriched
                    warnings.extend(llm_warnings)
        else:
            for listing, base in zip(deduped, bases, strict=False):
                listing.extracted = base
                llm_skipped += 1

        # Seniority classification. Title wins when explicit (Senior/Junior/etc).
        # Bare titles like "SOC Analyst" route through the description classifier
        # which uses senior/entry keywords + YoE bucket to assign a seniority,
        # falling back to 'unclear' only when no signal is found anywhere.
        for listing in deduped:
            yoe_min = listing.extracted.years_experience_min if listing.extracted else None
            level = classify_seniority_combined(listing.title, listing.description, yoe_min)
            if listing.extracted is not None:
                listing.extracted.seniority_signal = level  # type: ignore[assignment]

        # Seniority filter — drops listings outside the allowed bucket set.
        allowed_seniority = set(self.options.allowed_seniority)
        pre_seniority_count = len(deduped)
        deduped = [
            listing
            for listing in deduped
            if listing.extracted is not None and listing.extracted.seniority_signal in allowed_seniority
        ]
        dropped_seniority = pre_seniority_count - len(deduped)

        stats_by_bucket = tabulate(deduped)

        prior_stats_by_bucket = self._load_prior_stats(date_str)
        markdown = render_markdown_report(
            generated_at=generated_at,
            tool_version=__version__,
            stats_by_bucket=stats_by_bucket,
            prior_stats_by_bucket=prior_stats_by_bucket,
            warnings=sorted(set(warnings)),
        )

        snapshot = {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "tool_version": __version__,
            "input": {
                "location": self.options.location,
                "also_remote": self.options.also_remote,
                "results_per_source": self.options.results_per_source,
                "freshness_days": self.options.freshness_days,
                "allowed_seniority": self.options.allowed_seniority,
                "include_unclassified": self.options.include_unclassified,
                "role_buckets": self.options.role_buckets,
                "use_llm": self.options.use_llm and self.llm_extractor is not None,
                "queries": queries,
            },
            "summary": {
                "total_listings_pre_dedup": len(raw_listings),
                "total_listings_post_dedup": len(deduped),
                "dropped_off_topic": dropped_off_topic,
                "dropped_no_description": dropped_no_description,
                "dropped_stale": dropped_stale,
                "dropped_seniority": dropped_seniority,
                "physical_security_relabeled": physical_security_relabeled,
                "per_source_pre_dedup": per_source_counts,
                "listings_with_llm_extraction": sum(
                    1 for listing in deduped if listing.extracted and listing.extracted.llm_used
                ),
                "listings_regex_only": llm_skipped,
                "duration_seconds": round(perf_counter() - started, 3),
            },
            "stats_by_bucket": {bucket: stats_to_dict(stats) for bucket, stats in stats_by_bucket.items()},
            "listings": [asdict(listing) for listing in deduped],
            "warnings": sorted(set(warnings)),
        }

        if not self.options.dry_run:
            self._write_outputs(snapshot, deduped, stats_by_bucket, markdown, date_str=date_str)

        return snapshot

    def _queries_for_buckets(self) -> list[str]:
        enabled = set(self.options.role_buckets)
        if not enabled:
            return all_seed_phrases()
        queries: list[str] = []
        for bucket, phrases in ROLE_SEEDS.items():
            if bucket in enabled:
                queries.extend(phrases)
        # Stable dedup, preserve order.
        return list(dict.fromkeys(queries))

    def _safe_collect(
        self,
        collector: Collector,
        *,
        queries: list[str],
        location_override: str | None = None,
    ) -> CollectorResult:
        try:
            return collector.collect(
                queries=queries,
                location=location_override or self.options.location,
                results_per_query=self.options.results_per_source,
                freshness_days=self.options.freshness_days,
            )
        except Exception as exc:
            return CollectorResult(
                warnings=[f"{collector.source_name} crashed: {exc}"],
                source_name=collector.source_name,
            )

    def _write_outputs(
        self,
        snapshot: dict,
        listings: list[Listing],
        stats_by_bucket: dict[str, BucketStats],
        markdown: str,
        *,
        date_str: str,
    ) -> None:
        out_dir = self.options.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        write_snapshot_json(snapshot, output_path=out_dir / f"snapshot-{date_str}.json")
        write_listings_csv(listings, output_path=out_dir / f"snapshot-{date_str}.csv")
        (out_dir / f"report-{date_str}.md").write_text(markdown, encoding="utf-8")
        append_trend_csv(stats_by_bucket, date_str=date_str, output_path=out_dir / "trend.csv")

    def _load_prior_stats(self, current_date: str) -> dict[str, BucketStats] | None:
        out_dir = self.options.output_dir
        if not out_dir.exists():
            return None
        snapshots = sorted(out_dir.glob("snapshot-*.json"))
        candidates = [p for p in snapshots if p.stem != f"snapshot-{current_date}"]
        if not candidates:
            return None
        prior_path = candidates[-1]
        try:
            import json

            prior = json.loads(prior_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        stats_by_bucket: dict[str, BucketStats] = {}
        for bucket, payload in (prior.get("stats_by_bucket") or {}).items():
            stats_by_bucket[bucket] = _bucket_from_dict(bucket, payload)
        return stats_by_bucket


def _bucket_from_dict(bucket: str, payload: dict) -> BucketStats:
    return BucketStats(
        role_bucket=bucket,  # type: ignore[arg-type]
        sample_size=payload.get("sample_size", 0),
        source_breakdown=payload.get("source_breakdown", {}) or {},
        certifications=[tuple(item) for item in payload.get("certifications", []) or []],
        technical_skills=[tuple(item) for item in payload.get("technical_skills", []) or []],
        responsibilities=[tuple(item) for item in payload.get("responsibilities", []) or []],
        yoe_histogram=payload.get("yoe_histogram", {}) or {},
        yoe_with_value=payload.get("yoe_with_value", 0),
        degree_breakdown=payload.get("degree_breakdown", {}) or {},
        remote_arrangement=payload.get("remote_arrangement", {}) or {},
        seniority_signal=payload.get("seniority_signal", {}) or {},
        clearance_required=payload.get("clearance_required", 0),
    )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Tolerant date parsers — each source posts dates in a different format.
def _parse_posted_at(value: str) -> datetime | None:
    """Parse posted_at into a tz-aware UTC datetime. Returns None on failure."""
    s = (value or "").strip()
    if not s:
        return None
    # ISO 8601 (USAJobs, internal): "2026-05-25T18:30:00Z" or "...+00:00"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        pass
    # Date only: "2026-05-25" (Greenhouse, JobSpy)
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        pass
    return None


def _is_fresh(posted_at: str | None, max_age_days: int) -> bool:
    """Return True if the listing's posted_at is within max_age_days.

    Intentionally permissive: listings with missing or unparseable posted_at
    are KEPT (Greenhouse and some Lever postings omit the field; we'd rather
    show them than drop everything on a parser miss).
    """
    if not posted_at:
        return True
    dt = _parse_posted_at(posted_at)
    if dt is None:
        return True
    return (datetime.now(UTC) - dt) <= timedelta(days=max_age_days)
