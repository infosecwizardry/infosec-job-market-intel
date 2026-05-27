"""Aggregate LLM-cited evidence phrases into candidate regex patterns.

After a batched LLM run classifies the previously-unclear listings, each
result includes `level_clues` — verbatim phrases from the description that
justified the seniority call. This module:

1. Normalizes those phrases (lowercase, strip punctuation, collapse whitespace).
2. Counts frequency per seniority bucket.
3. Filters to phrases that appear at least `min_freq` times.
4. Renders a markdown report a human can review before promoting any phrase
   into `_DESC_ENTRY_PATTERNS` / `_DESC_SENIOR_PATTERNS` in seeds.py.

The system is intentionally NOT self-modifying — each round's discovered
patterns are surfaced as a review document, not auto-applied. The user
decides what to bake in.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime

# Phrases shorter than this (after normalization) are too generic to surface.
_MIN_PHRASE_LEN = 6
# Words shared across nearly every infosec listing — drop them so we don't
# clutter the report with low-signal noise.
_STOPWORD_PHRASES = {
    "experience",
    "knowledge",
    "skills",
    "ability",
    "responsibilities",
    "qualifications",
    "the role",
    "this role",
    "the team",
}

_PUNCT_RE = re.compile(r"[^\w\s+/.-]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_phrase(phrase: str) -> str:
    """Lowercase, strip leading/trailing punctuation, collapse whitespace.

    Keep alphanumerics, whitespace, and a handful of in-word symbols ('+',
    '/', '.', '-') so we don't mangle tokens like "C++", "Tier-3", "A+",
    "5+", "L1/L2".
    """
    if not phrase:
        return ""
    lowered = phrase.lower().strip()
    cleaned = _PUNCT_RE.sub(" ", lowered)
    collapsed = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return collapsed


def mine_patterns(
    clues_by_seniority: dict[str, list[str]],
    *,
    min_freq: int = 3,
) -> dict[str, list[tuple[str, int, str]]]:
    """Turn raw clue lists into ranked candidate-pattern lists per bucket.

    Args:
        clues_by_seniority: keys like "entry"/"mid"/"senior"/"leadership",
            values are flat lists of verbatim phrases the LLM cited.
        min_freq: minimum number of times a normalized phrase must appear
            within a bucket to be surfaced.

    Returns:
        dict keyed by seniority. Each value is a list of tuples sorted by
        frequency descending, then phrase ascending:
            (normalized_phrase, frequency, example_original_phrase)
        The original example helps a human spot whether the normalization
        ate something important (case, punctuation).
    """
    out: dict[str, list[tuple[str, int, str]]] = {}
    for bucket, phrases in clues_by_seniority.items():
        counter: Counter[str] = Counter()
        examples: dict[str, str] = {}
        for raw in phrases:
            norm = _normalize_phrase(raw)
            if len(norm) < _MIN_PHRASE_LEN:
                continue
            if norm in _STOPWORD_PHRASES:
                continue
            counter[norm] += 1
            # Keep the FIRST original casing seen — useful for matching back
            # to a real description if the user wants to verify.
            if norm not in examples:
                examples[norm] = raw.strip()
        ranked = sorted(
            ((p, c, examples[p]) for p, c in counter.items() if c >= min_freq),
            key=lambda t: (-t[1], t[0]),
        )
        out[bucket] = ranked
    return out


def render_markdown(
    buckets: dict[str, list[tuple[str, int, str]]],
    *,
    generated_at: str | None = None,
    source_count: int | None = None,
) -> str:
    """Render a human-readable review report.

    Args:
        buckets: output of `mine_patterns`.
        generated_at: ISO-8601 timestamp string. Defaults to now (UTC).
        source_count: optional total listings the clues came from — shown
            in the header so the reader can gauge representativeness.
    """
    ts = generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines: list[str] = []
    lines.append("# Discovered seniority patterns")
    lines.append("")
    lines.append(f"_Generated: {ts}_")
    if source_count is not None:
        lines.append(f"_Source: {source_count} LLM-classified listings_")
    lines.append("")
    lines.append(
        "Each section lists short phrases the LLM cited as evidence for a "
        "seniority bucket, normalized and aggregated by frequency. Phrases "
        "appearing at least the minimum-frequency threshold are surfaced "
        "below as candidates for `_DESC_ENTRY_PATTERNS` / "
        "`_DESC_SENIOR_PATTERNS` in `seeds.py`. **Review before promoting** — "
        "the goal is zero false positives in the regex pipeline."
    )
    lines.append("")

    # Stable section ordering matching the seniority hierarchy.
    section_order = ["entry", "mid", "senior", "leadership"]
    seen = set(section_order)
    extras = [b for b in buckets if b not in seen]
    for bucket in [*section_order, *extras]:
        ranked = buckets.get(bucket, [])
        lines.append(f"## {bucket}")
        lines.append("")
        if not ranked:
            lines.append("_No phrases met the frequency threshold._")
            lines.append("")
            continue
        lines.append("| Frequency | Normalized phrase | Example (verbatim) |")
        lines.append("|---:|---|---|")
        for normalized, freq, example in ranked:
            # Escape pipe chars in cell contents so the table renders.
            n_safe = normalized.replace("|", "\\|")
            e_safe = example.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {freq} | `{n_safe}` | {e_safe} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
