"""Estimate corpus-wide remote-arrangement coverage for a set of regex patterns.

Companion to `analyze_remote_signals.py`. Runs candidate remote/hybrid/onsite
patterns over every listing in an unfiltered snapshot and reports what fraction
the patterns can confidently classify — the number that decided whether the
regex approach was worth shipping (it landed at ~24.5% on the May 2026 corpus,
which the production extractor then beat slightly with the location-field and
JobSpy is_remote signals layered on).

The patterns here mirror what shipped in
`job_market_intel/extract/regex_rules.py`; keep them roughly in sync when the
production patterns change so this stays a useful regression check on coverage.

Usage:
    python scripts/estimate_coverage.py [SNAPSHOT_PATH]

SNAPSHOT_PATH defaults to the most recent reports/snapshot-*.unfiltered.json.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

P_REMOTE = re.compile(
    r"(?:"
    r"100\s*%\s*remote|fully\s+remote|remote[-\s]?first|remote[-\s]?friendly|"
    r"this\s+is\s+a\s+remote\s+(?:position|role|job)|remote\s+position|remote\s+role|"
    r"remote\s+(?:work|opportunity|employee|employees|worker|workers)|"
    r"work\s+location\s*[:\-]\s*remote|workplace\s+type\s*[:\-]\s*remote|"
    r"work\s+model\s*[:\-]\s*remote|work\s+arrangement\s*[:\-]\s*remote|"
    r"telework(?:\s+eligible)?|teleworking|"
    r"work\s+from\s+home|wfh\b|home[-\s]?based|home\s+office|"
    r"distributed\s+team|virtual\s+(?:position|role)|"
    r"li-?remote\b"
    r")",
    re.IGNORECASE,
)
P_HYBRID = re.compile(
    r"(?:"
    r"hybrid\s+(?:work|schedule|role|position|model|arrangement|environment|setup|basis|setting|in-?office)|"
    r"hybrid[-\s]?remote|li-?hybrid\b|"
    r"work\s+(?:model|location|arrangement|schedule)\s*[:\-]\s*hybrid|"
    r"workplace\s+type\s*[:\-]\s*hybrid|"
    r"\d\s*(?:-\s*\d\s*)?days?\s+(?:per|a|/)\s*week\s+(?:in|at|on-?site)|"
    r"\d\s*days?\s+(?:in|at)\s+(?:the\s+)?(?:office|on-?site)|"
    r"this\s+is\s+a\s+hybrid|hybrid\s+position|hybrid\s+role|"
    r"telework[/\s-]+hybrid|hybrid[/\s-]+telework"
    r")",
    re.IGNORECASE,
)
P_ONSITE = re.compile(
    r"(?:"
    r"work\s+location\s*[:\-]?\s*in\s+person|workplace\s+type\s*[:\-]?\s*on-?site|"
    r"this\s+is\s+an?\s+(?:on-?site|in-?person)\s+(?:position|role|job)|"
    r"on-?site\s+(?:position|role|job|only|required|presence|work)|fully\s+on-?site|100\s*%\s*on-?site|"
    r"must\s+(?:be\s+able\s+to\s+)?work\s+on-?site|"
    r"in-?office\s+(?:position|role|job|only|required|work)|"
    r"work\s+model\s*[:\-]\s*on-?site|work\s+arrangement\s*[:\-]\s*on-?site|"
    r"5\s*days?\s+(?:per|a)\s*week\s+(?:in|at)\s+(?:the\s+)?office|"
    r"primarily\s+in\s+the\s+office|in-?person\s+collaboration"
    r")",
    re.IGNORECASE,
)

LOC_REMOTE = re.compile(r"\b(?:remote|anywhere(?:\s+in\s+(?:the\s+)?us)?)\b", re.IGNORECASE)
LOC_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
LOC_ONSITE_KW = re.compile(r"\b(?:on-?site|in[-\s]?office|in\s+person)\b", re.IGNORECASE)


def _default_snapshot() -> Path | None:
    candidates = sorted((REPO_ROOT / "reports").glob("snapshot-*.unfiltered.json"))
    return candidates[-1] if candidates else None


def _classify(description: str, location: str) -> str:
    # Location field is the highest-priority signal.
    if LOC_HYBRID.search(location):
        return "hybrid"
    if LOC_REMOTE.search(location):
        return "remote"
    if LOC_ONSITE_KW.search(location):
        return "onsite"
    # Description patterns — hybrid wins ties, remote loses (highest FP rate).
    if P_HYBRID.search(description):
        return "hybrid"
    if P_ONSITE.search(description):
        return "onsite"
    if P_REMOTE.search(description):
        return "remote"
    return "unspecified"


def main(argv: list[str]) -> int:
    snapshot_path = Path(argv[1]) if len(argv) > 1 else _default_snapshot()
    if snapshot_path is None or not snapshot_path.exists():
        print("No snapshot found. Pass a path: python scripts/estimate_coverage.py <snapshot.json>")
        return 2

    listings = json.loads(snapshot_path.read_text(encoding="utf-8")).get("listings", [])
    if not listings:
        print(f"No listings in {snapshot_path.name}")
        return 1

    cats: Counter = Counter()
    for li in listings:
        cats[_classify(li.get("description") or "", li.get("location") or "")] += 1

    print(f"Corpus-wide classification ({snapshot_path.name}):")
    for label, count in cats.most_common():
        print(f"  {label:12s}  {count:5d}  ({100 * count / len(listings):.1f}%)")
    classified = sum(count for label, count in cats.items() if label != "unspecified")
    print(f"Total: {len(listings)}")
    print(f"Classified (any of 3): {classified} ({100 * classified / len(listings):.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
