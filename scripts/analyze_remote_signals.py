"""Mine remote / hybrid / onsite phrasings from an unfiltered snapshot.

This is the analysis tool that produced the remote-arrangement regex patterns
now living in `job_market_intel/extract/regex_rules.py`. Keep it around so the
next signal-mining round (salary bands, clearance phrasings, shift patterns,
etc.) has a starting template, and so the provenance of the shipped patterns
is reproducible.

It samples up to ~800 listings (stratified by company), tallies the surface
forms around remote/hybrid/onsite keywords, surfaces structured "Work Model:"
fields and N-days-per-week phrasings, and flags the product-term false
positives ("remote access", "remote desktop") that the production negative
lookahead has to guard against.

Usage:
    python scripts/analyze_remote_signals.py [SNAPSHOT_PATH]

SNAPSHOT_PATH defaults to the most recent reports/snapshot-*.unfiltered.json.
"""

from __future__ import annotations

import random
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SIZE = 800
WINDOW = 40

KEYWORDS = [
    r"remote",
    r"hybrid",
    r"on-?\s?site",
    r"in-?\s?office",
    r"telework",
    r"work\s+from\s+home",
    r"wfh",
    r"work\s+model",
    r"work\s+location",
    r"work\s+arrangement",
    r"workplace\s+type",
    r"location\s+type",
    r"fully\s+remote",
    r"100%?\s+remote",
    r"home\s+office",
    r"distributed\s+team",
    r"remote-?first",
    r"remote-?friendly",
    r"days?\s+(?:per|a)\s+week\s+in",
    r"days?\s+in\s+(?:the\s+)?office",
    r"in\s+the\s+office",
    r"in\s+person",
]
KW_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)

LOC_REMOTE = re.compile(r"\bremote\b|\banywhere\b|\bus(?:a)?[-\s]*remote\b", re.IGNORECASE)
LOC_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
LOC_ONSITE = re.compile(r"\bon[-\s]?site\b|\bin[-\s]?office\b|\bin person\b", re.IGNORECASE)

STRUCT_RE = re.compile(
    r"(work\s+model|work\s+location|work\s+arrangement|workplace\s+type|location\s+type|work\s+style)"
    r"\s*[:\-]\s*([A-Za-z][A-Za-z\s/\-]{0,40})",
    re.IGNORECASE,
)
DAYS_RE = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?days?\s+(?:per|a|/)\s*week\s+(?:in|at|on-?site|in-office)", re.IGNORECASE)
DAYS_RE2 = re.compile(r"(\d+)\s*days?\s+in\s+(?:the\s+)?office", re.IGNORECASE)

FP_PATTERNS = [
    r"remote\s+(?:access|management|monitoring|desktop|user|users|workforce|control|device|devices|"
    r"location|sites?|server|servers|system|systems|connection|wipe|attack|attacker|"
    r"code execution|exploit|exploitation)",
    r"remote(?:ly)?\s+(?:execut|access|manag|monitor|control)",
    r"in[- ]office\s+(?:printer|equipment|hardware)",
]


def _load_listings(snapshot_path: Path) -> list[dict]:
    import json

    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    return data.get("listings", [])


def _default_snapshot() -> Path | None:
    candidates = sorted((REPO_ROOT / "reports").glob("snapshot-*.unfiltered.json"))
    return candidates[-1] if candidates else None


def _stratified_sample(listings: list[dict]) -> list[dict]:
    """Up to 3 listings per company, shuffled, capped at SAMPLE_SIZE."""
    rng = random.Random(42)  # nosec B311 — deterministic sampling for reproducible analysis, not crypto
    by_company: dict[str, list[dict]] = {}
    for li in listings:
        by_company.setdefault(li.get("company", "?"), []).append(li)
    companies = list(by_company.keys())
    rng.shuffle(companies)
    sample: list[dict] = []
    for company in companies:
        sample.extend(by_company[company][:3])
        if len(sample) >= SAMPLE_SIZE:
            break
    return sample[:SAMPLE_SIZE]


def _normalize_phrase(fragment: str, kw_pattern: str) -> str | None:
    match = re.search(kw_pattern, fragment, re.IGNORECASE)
    if not match:
        return None
    start = max(0, match.start() - 25)
    end = min(len(fragment), match.end() + 25)
    sub = re.sub(r"[^a-z0-9%\s\-]", " ", fragment[start:end].lower())
    return re.sub(r"\s+", " ", sub).strip()


def _show(title: str, counter: Counter, limit: int = 30) -> None:
    print(f"\n=== {title} (top {limit} normalized phrases) ===")
    for phrase, count in counter.most_common(limit):
        print(f"{count:4d}  {phrase}")


def main(argv: list[str]) -> int:
    snapshot_path = Path(argv[1]) if len(argv) > 1 else _default_snapshot()
    if snapshot_path is None or not snapshot_path.exists():
        print("No snapshot found. Pass a path: python scripts/analyze_remote_signals.py <snapshot.json>")
        return 2

    listings = _load_listings(snapshot_path)
    print(f"Loaded {len(listings)} listings from {snapshot_path.name}")

    sample = _stratified_sample(listings)
    print(f"Sample size: {len(sample)} from {len({li.get('company') for li in sample})} companies")

    contexts: list[tuple[str, str]] = []
    location_field_counter: Counter = Counter()
    for li in sample:
        desc = li.get("description") or ""
        location_field_counter[(li.get("location") or "").strip()] += 1
        for match in KW_RE.finditer(desc):
            start = max(0, match.start() - WINDOW)
            end = min(len(desc), match.end() + WINDOW)
            fragment = re.sub(r"\s+", " ", desc[start:end].replace("\n", " ").replace("\r", " ")).strip()
            contexts.append((match.group(0).lower(), fragment))
    print(f"Total keyword hits in descriptions: {len(contexts)}")

    print("\n=== TOP LOCATION FIELD VALUES ===")
    for location, count in location_field_counter.most_common(40):
        print(f"{count:4d}  {location!r}")

    loc_counts: Counter = Counter()
    for location, count in location_field_counter.items():
        if LOC_REMOTE.search(location):
            loc_counts["remote"] += count
        elif LOC_HYBRID.search(location):
            loc_counts["hybrid"] += count
        elif LOC_ONSITE.search(location):
            loc_counts["onsite"] += count
        else:
            loc_counts["unclassified_by_location"] += count
    print("\n=== LOCATION-FIELD CLASSIFICATION (sample) ===")
    for label, count in loc_counts.most_common():
        print(f"{label}: {count}")

    norm_remote: Counter = Counter()
    norm_hybrid: Counter = Counter()
    norm_onsite: Counter = Counter()
    for kw, fragment in contexts:
        low = kw.lower()
        if "hybrid" in low:
            phrase = _normalize_phrase(fragment, r"hybrid")
            target = norm_hybrid
        elif "remote" in low or "telework" in low or "wfh" in low:
            phrase = _normalize_phrase(fragment, r"remote|telework|wfh")
            target = norm_remote
        elif "work from home" in low or "home office" in low:
            phrase = _normalize_phrase(fragment, r"work from home|home office")
            target = norm_remote
        elif ("on" in low and ("site" in low or "office" in low)) or "in person" in low or "in the office" in low:
            phrase = _normalize_phrase(fragment, r"on-?\s?site|in-?\s?office|in the office|in person")
            target = norm_onsite
        else:
            phrase = None
            target = norm_remote
        if phrase:
            target[phrase] += 1

    _show("REMOTE", norm_remote, 35)
    _show("HYBRID", norm_hybrid, 35)
    _show("ONSITE", norm_onsite, 35)

    print("\n=== STRUCTURED PATTERNS (Work Model / Work Location / etc.) ===")
    struct_hits: Counter = Counter()
    for li in sample:
        for match in STRUCT_RE.finditer(li.get("description") or ""):
            struct_hits[(match.group(1).lower().strip(), match.group(2).strip()[:40])] += 1
    for (key, value), count in struct_hits.most_common(30):
        print(f"{count:3d}  {key} -> {value}")

    print("\n=== DAYS-PER-WEEK / DAYS-IN-OFFICE PATTERNS ===")
    days_counter: Counter = Counter()
    for li in sample:
        desc = li.get("description") or ""
        for match in DAYS_RE.finditer(desc):
            days_counter[match.group(0).lower()] += 1
        for match in DAYS_RE2.finditer(desc):
            days_counter[match.group(0).lower()] += 1
    for phrase, count in days_counter.most_common(20):
        print(f"{count:3d}  {phrase}")

    print("\n=== POTENTIAL FALSE POSITIVES (remote not about work arrangement) ===")
    fp_counter: Counter = Counter()
    for li in sample:
        desc = li.get("description") or ""
        for pattern in FP_PATTERNS:
            for match in re.finditer(pattern, desc, re.IGNORECASE):
                fp_counter[match.group(0).lower()] += 1
    for phrase, count in fp_counter.most_common(25):
        print(f"{count:3d}  {phrase}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
