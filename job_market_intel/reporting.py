"""Write JSON snapshot + CSV + markdown report to Community/reports/job-market-intel/."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from .models import Listing
from .scoring import BucketStats

_BUCKET_TITLES = {
    "junior_soc": "Junior SOC Analyst",
    "help_desk_it_admin": "Help Desk / IT Admin",
    "unclassified": "Unclassified",
}


def write_snapshot_json(report: dict, *, output_path: Path) -> None:
    """Atomically write a snapshot JSON file.

    Writes to {path}.tmp, then os.replace() onto the final path. Prevents
    readers (e.g. the dashboard) from seeing a half-written file while a
    scrape is mid-flush.
    """
    import os

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(tmp_path, output_path)


def write_listings_csv(listings: list[Listing], *, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "listing_id",
        "role_bucket",
        "title",
        "company",
        "location",
        "sources",
        "source_urls",
        "posted_at",
        "fetched_at",
        "certifications",
        "yoe_min",
        "yoe_max",
        "degree",
        "clearance",
        "salary_min",
        "salary_max",
        "schedule_signals",
        "technical_skills",
        "seniority_signal",
        "remote_arrangement",
        "llm_used",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for listing in listings:
            req = listing.extracted
            writer.writerow(
                {
                    "listing_id": listing.listing_id,
                    "role_bucket": listing.role_bucket,
                    "title": listing.title,
                    "company": listing.company,
                    "location": listing.location,
                    "sources": "|".join(listing.sources),
                    "source_urls": "|".join(listing.source_urls),
                    "posted_at": listing.posted_at or "",
                    "fetched_at": listing.fetched_at,
                    "certifications": "|".join(req.certifications) if req else "",
                    "yoe_min": (req.years_experience_min if req else "") if req else "",
                    "yoe_max": (req.years_experience_max if req else "") if req else "",
                    "degree": (req.degree or "") if req else "",
                    "clearance": (req.clearance or "") if req else "",
                    "salary_min": (req.salary_min if req else "") or "",
                    "salary_max": (req.salary_max if req else "") or "",
                    "schedule_signals": "|".join(req.schedule_signals) if req else "",
                    "technical_skills": "|".join(req.technical_skills) if req else "",
                    "seniority_signal": (req.seniority_signal if req else "") if req else "",
                    "remote_arrangement": (req.remote_arrangement if req else "") if req else "",
                    "llm_used": (req.llm_used if req else False),
                }
            )


def append_trend_csv(stats_by_bucket: dict, *, date_str: str, output_path: Path) -> None:
    """Append long-form (date, role_bucket, requirement_type, requirement, count, total)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not output_path.exists()
    with output_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(["date", "role_bucket", "requirement_type", "requirement", "count", "total"])
        for bucket, stats in stats_by_bucket.items():
            total = stats.sample_size or 1
            for cert, count in stats.certifications:
                writer.writerow([date_str, bucket, "cert", cert, count, total])
            for skill, count in stats.technical_skills:
                writer.writerow([date_str, bucket, "skill", skill, count, total])
            for yoe_bucket, count in stats.yoe_histogram.items():
                writer.writerow([date_str, bucket, "yoe_bucket", yoe_bucket, count, stats.yoe_with_value or total])
            for degree, count in stats.degree_breakdown.items():
                writer.writerow([date_str, bucket, "degree", degree, count, total])
            for arrangement, count in stats.remote_arrangement.items():
                writer.writerow([date_str, bucket, "remote", arrangement, count, total])


def render_markdown_report(
    *,
    generated_at: str,
    tool_version: str,
    stats_by_bucket: dict[str, BucketStats],
    prior_stats_by_bucket: dict[str, BucketStats] | None,
    warnings: list[str],
) -> str:
    """Produce a human-readable markdown summary for the snapshot."""
    lines: list[str] = []
    lines.append("# Job Market Intel — Weekly Snapshot")
    lines.append("")
    lines.append(f"_Generated {generated_at} · tool v{tool_version}_")
    lines.append("")

    bucket_order = ["junior_soc", "help_desk_it_admin", "unclassified"]
    ordered = [b for b in bucket_order if b in stats_by_bucket]

    for bucket in ordered:
        stats = stats_by_bucket[bucket]
        prior = (prior_stats_by_bucket or {}).get(bucket) if prior_stats_by_bucket else None
        lines.extend(_render_bucket(bucket, stats, prior))

    if warnings:
        lines.append("---")
        lines.append("## Run warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines)


def _render_bucket(bucket: str, stats: BucketStats, prior: BucketStats | None) -> list[str]:
    title = _BUCKET_TITLES.get(bucket, bucket)
    lines = [f"## {title}", ""]
    if stats.sample_size == 0:
        lines.append("_No listings._")
        lines.append("")
        return lines

    lines.append(f"- **Sample size:** {stats.sample_size} listings")
    if stats.source_breakdown:
        breakdown = ", ".join(f"{src}={count}" for src, count in stats.source_breakdown.items())
        lines.append(f"- **By source:** {breakdown}")
    if stats.clearance_required:
        lines.append(f"- **Clearance required:** {_pct(stats.clearance_required, stats.sample_size)} of postings")
    lines.append("")

    lines.append("### Top certifications")
    lines.append("")
    if stats.certifications:
        for cert, count in stats.certifications[:10]:
            delta = _delta_label(cert, count, prior.certifications if prior else None)
            lines.append(f"- **{cert}** — {count} ({_pct(count, stats.sample_size)}){delta}")
    else:
        lines.append("_No certifications detected._")
    lines.append("")

    lines.append("### Years of experience")
    lines.append("")
    if stats.yoe_histogram:
        for bucket_key in ("0", "1-2", "3-5", "6+"):
            count = stats.yoe_histogram.get(bucket_key, 0)
            bar = "█" * max(1, count) if count else ""
            lines.append(f"- `{bucket_key:>3} yrs` {bar} {count}")
        lines.append(f"- _{stats.yoe_with_value} of {stats.sample_size} listings explicitly stated a minimum._")
    else:
        lines.append("_No explicit years of experience detected._")
    lines.append("")

    lines.append("### Degree requirement")
    lines.append("")
    if stats.degree_breakdown:
        for degree, count in sorted(stats.degree_breakdown.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{degree}** — {count} ({_pct(count, stats.sample_size)})")
    else:
        lines.append("_No explicit degree requirement detected._")
    lines.append("")

    lines.append("### Remote vs hybrid vs on-site")
    lines.append("")
    if stats.remote_arrangement:
        for arrangement in ("remote", "hybrid", "onsite", "unspecified"):
            count = stats.remote_arrangement.get(arrangement, 0)
            if count:
                lines.append(f"- **{arrangement}** — {count} ({_pct(count, stats.sample_size)})")
    else:
        lines.append("_No remote signal extracted._")
    lines.append("")

    lines.append("### Top technical skills")
    lines.append("")
    if stats.technical_skills:
        for skill, count in stats.technical_skills[:10]:
            lines.append(f"- **{skill}** — {count} ({_pct(count, stats.sample_size)})")
    else:
        lines.append("_No technical skills detected._")
    lines.append("")

    lines.append("### Top responsibilities (LLM-extracted)")
    lines.append("")
    if stats.responsibilities:
        for resp, count in stats.responsibilities[:8]:
            lines.append(f"- {resp} ({count})")
    else:
        lines.append("_No LLM-extracted responsibilities (LLM may have been disabled)._")
    lines.append("")

    return lines


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "0%"
    return f"{round(100 * part / whole)}%"


def _delta_label(key: str, count: int, prior: list[tuple[str, int]] | None) -> str:
    if not prior:
        return ""
    prior_lookup = dict(prior)
    if key not in prior_lookup:
        return " · _new this week_"
    delta = count - prior_lookup[key]
    if delta > 0:
        return f" · ▲{delta}"
    if delta < 0:
        return f" · ▼{abs(delta)}"
    return " · ="


def stats_to_dict(stats: BucketStats) -> dict:
    return asdict(stats)
