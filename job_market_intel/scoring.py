"""Frequency tabulation over a deduped listing set, bucketed by role."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import Listing, RoleBucket


@dataclass(slots=True)
class BucketStats:
    role_bucket: RoleBucket
    sample_size: int = 0
    source_breakdown: dict[str, int] = field(default_factory=dict)
    certifications: list[tuple[str, int]] = field(default_factory=list)
    technical_skills: list[tuple[str, int]] = field(default_factory=list)
    responsibilities: list[tuple[str, int]] = field(default_factory=list)
    yoe_histogram: dict[str, int] = field(default_factory=dict)  # bucketed: "0", "1-2", "3-5", "6+"
    yoe_with_value: int = 0
    degree_breakdown: dict[str, int] = field(default_factory=dict)
    remote_arrangement: dict[str, int] = field(default_factory=dict)
    seniority_signal: dict[str, int] = field(default_factory=dict)
    clearance_required: int = 0


def tabulate(listings: list[Listing]) -> dict[RoleBucket, BucketStats]:
    """Group listings by role_bucket and return per-bucket frequency tables."""
    by_bucket: dict[RoleBucket, list[Listing]] = {}
    for listing in listings:
        by_bucket.setdefault(listing.role_bucket, []).append(listing)

    return {bucket: _tabulate_bucket(bucket, items) for bucket, items in by_bucket.items()}


def _tabulate_bucket(bucket: RoleBucket, listings: list[Listing]) -> BucketStats:
    stats = BucketStats(role_bucket=bucket, sample_size=len(listings))

    source_counter: Counter[str] = Counter()
    cert_counter: Counter[str] = Counter()
    skill_counter: Counter[str] = Counter()
    resp_counter: Counter[str] = Counter()
    yoe_buckets: Counter[str] = Counter()
    degree_counter: Counter[str] = Counter()
    arrangement_counter: Counter[str] = Counter()
    seniority_counter: Counter[str] = Counter()

    for listing in listings:
        for source in listing.sources:
            source_counter[source] += 1

        req = listing.extracted
        if req is None:
            continue

        for cert in req.certifications:
            cert_counter[cert] += 1
        for skill in req.technical_skills:
            skill_counter[skill] += 1
        for resp in req.responsibilities:
            # normalize lightly so near-duplicates aggregate
            normalized = resp.strip().rstrip(".").lower()
            if normalized:
                resp_counter[normalized] += 1
        if req.years_experience_min is not None:
            stats.yoe_with_value += 1
            yoe_buckets[_yoe_bucket(req.years_experience_min)] += 1
        if req.degree:
            degree_counter[req.degree] += 1
        arrangement_counter[req.remote_arrangement] += 1
        seniority_counter[req.seniority_signal] += 1
        if req.clearance:
            stats.clearance_required += 1

    stats.source_breakdown = dict(source_counter.most_common())
    stats.certifications = cert_counter.most_common(15)
    stats.technical_skills = skill_counter.most_common(15)
    stats.responsibilities = resp_counter.most_common(10)
    stats.yoe_histogram = dict(yoe_buckets)
    stats.degree_breakdown = dict(degree_counter)
    stats.remote_arrangement = dict(arrangement_counter)
    stats.seniority_signal = dict(seniority_counter)
    return stats


def _yoe_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value <= 2:
        return "1-2"
    if value <= 5:
        return "3-5"
    return "6+"
