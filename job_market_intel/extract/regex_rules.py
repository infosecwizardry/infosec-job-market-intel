"""Regex + dictionary extraction. Free, deterministic, fast.

Covers what follows predictable patterns: certifications, years of experience,
degree level, security clearance, salary range, schedule signals, named tech skills.
The LLM pass handles responsibilities and ambiguous skills.
"""

from __future__ import annotations

import re

from ..models import ExtractedRequirements
from ..seeds import CERTIFICATIONS, TECH_SKILLS

# Compiled once at import.
_CERT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (canonical, re.compile("|".join(f"(?:{v})" for v in variants), re.IGNORECASE))
    for canonical, variants in CERTIFICATIONS.items()
]

_SKILL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (canonical, re.compile("|".join(f"(?:{v})" for v in variants), re.IGNORECASE))
    for canonical, variants in TECH_SKILLS.items()
]

# Matches "3+ years", "3-5 years", "3<EN-DASH>5 years", "minimum of 2 years", etc.
# The literal en-dash in the regex is intentional: job listings commonly use it.
_YOE_RE = re.compile(
    r"(?P<min>\d{1,2})\s*\+?\s*(?:to|-|–)?\s*(?P<max>\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",  # noqa: RUF001
    re.IGNORECASE,
)
_YOE_CONTEXT_TOKENS = (
    "experience",
    "work experience",
    "professional experience",
    "background",
    "in security",
    "in cybersecurity",
    "in it",
    "in information technology",
    "in a soc",
    "in help desk",
    "in technical support",
    "of relevant",
    "in related",
    # Recruiter / posting language — used only for the SENTENCE-LEVEL strict
    # pass. These are safe at sentence level because a sentence containing
    # e.g. "5 years" and "looking for" is almost always about experience.
    "looking for",
    "seeking",
    "minimum",
    "required",
    "preferred",
    "needs",
    "must have",
    "ideal candidate",
    "candidates with",
)

# Stricter subset for the BODY-SCAN fallback (60-char window). The full token
# list above is too loose for cross-sentence proximity — "Bachelor's degree
# required" co-occurring with "founded 25 years ago" within 60 chars would
# falsely match if we used "required" here.
_STRICT_YOE_CUES = (
    "experience",
    "background",
    "exp.",
    "minimum",
    "at least",
    "or more",
    "of relevant",
    "of professional",
    "of prior",
    "of hands-on",
    "of progressive",
    "of cybersecurity",
    "of security",
    "of it",
    "looking for someone with",
    "ideal candidate has",
    "candidates with",
)

_DEGREE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("bachelor", re.compile(r"\bbachelor(?:'s)?\b|\bb\.?s\.?\b|\bbachelors\b", re.IGNORECASE)),
    ("associate", re.compile(r"\bassociate(?:'s)?\b|\baa\b|\baas\b", re.IGNORECASE)),
    ("high_school", re.compile(r"\bhigh\s+school\b|\bged\b", re.IGNORECASE)),
    ("equivalent", re.compile(r"\b(?:equivalent|comparable)\s+(?:work\s+)?experience\b", re.IGNORECASE)),
]

_CLEARANCE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("ts_sci", re.compile(r"\bts\s*/\s*sci\b|\btop\s+secret\s*/\s*sci\b", re.IGNORECASE)),
    ("top_secret", re.compile(r"\btop\s+secret\b", re.IGNORECASE)),
    ("secret", re.compile(r"(?<!top\s)\bsecret\s+clearance\b|\bactive\s+secret\b", re.IGNORECASE)),
    ("public_trust", re.compile(r"\bpublic\s+trust\b", re.IGNORECASE)),
]

# Matches "$55,000 - $75,000", "$55,000 <EN-DASH> $75,000", "$55K-$75K", "$55,000 to $75,000".
_SALARY_RE = re.compile(
    r"\$\s*(?P<lo>\d{2,3}(?:,\d{3})?(?:k)?)\s*(?:-|to|–)\s*\$?\s*(?P<hi>\d{2,3}(?:,\d{3})?(?:k)?)",  # noqa: RUF001
    re.IGNORECASE,
)

_SCHEDULE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("swing", re.compile(r"\bswing\s+shift\b", re.IGNORECASE)),
    ("graveyard", re.compile(r"\bgraveyard\b|\bovernight\s+shift\b", re.IGNORECASE)),
    ("24x7", re.compile(r"\b24\s*/\s*7\b|\b24x7\b|\baround[-\s]the[-\s]clock\b", re.IGNORECASE)),
    ("on_call", re.compile(r"\bon[-\s]call\b", re.IGNORECASE)),
    ("rotating", re.compile(r"\brotating\s+shift\b|\brotation\b", re.IGNORECASE)),
    ("weekend", re.compile(r"\bweekend\s+shift\b|\bweekends\s+required\b", re.IGNORECASE)),
]


def extract(text: str) -> ExtractedRequirements:
    """Run all regex passes against the listing description and return structured results."""
    result = ExtractedRequirements()
    if not text:
        return result

    result.certifications = _find_certifications(text)
    result.technical_skills = _find_skills(text)
    result.years_experience_min, result.years_experience_max = _find_yoe(text)
    result.degree = _find_first_label(text, _DEGREE_RULES)
    result.clearance = _find_first_label(text, _CLEARANCE_RULES)
    result.salary_min, result.salary_max = _find_salary(text)
    result.schedule_signals = _find_all_labels(text, _SCHEDULE_RULES)
    return result


def _find_certifications(text: str) -> list[str]:
    found: list[str] = []
    for canonical, pattern in _CERT_PATTERNS:
        if pattern.search(text):
            found.append(canonical)
    return found


def _find_skills(text: str) -> list[str]:
    found: list[str] = []
    for canonical, pattern in _SKILL_PATTERNS:
        if pattern.search(text):
            found.append(canonical)
    return found


def _find_yoe(text: str) -> tuple[int | None, int | None]:
    """Return (min, max) years of experience.

    First pass: split into sentences and score each sentence against a fixed
    context-token list — listings that explicitly use "experience" / "background"
    / "minimum" etc. inside a sentence with a number.

    Fallback: when the strict pass finds nothing, scan the whole text for any
    "N years" pattern with a 60-character context window — catches phrasings
    that span sentence boundaries or use cue words further from the number.
    """
    candidates: list[tuple[int, int | None]] = []
    for sentence in _split_sentences(text):
        if not any(token in sentence.lower() for token in _YOE_CONTEXT_TOKENS):
            continue
        for match in _YOE_RE.finditer(sentence):
            lo = int(match.group("min"))
            hi_raw = match.group("max")
            hi = int(hi_raw) if hi_raw else None
            # Filter unreasonable values (resume gaps, "10 years ago", page numbers).
            if lo > 30 or (hi is not None and hi > 40):
                continue
            candidates.append((lo, hi))

    if candidates:
        candidates.sort(key=lambda pair: (pair[0], pair[1] or pair[0]))
        return candidates[0]

    # Fallback: whole-text scan with a 60-char context window. Useful when the
    # YoE digit and the cue word ("experience" / "background" / "looking for")
    # live in different sentences or markdown blocks.
    if not text:
        return None, None
    body = text.lower()
    window_candidates: list[tuple[int, int | None]] = []
    for match in _YOE_RE.finditer(body):
        try:
            lo = int(match.group("min"))
        except (TypeError, ValueError):
            continue
        hi_raw = match.group("max")
        hi = int(hi_raw) if hi_raw and hi_raw.isdigit() else None
        if lo > 30 or (hi is not None and hi > 40):
            continue
        start = max(0, match.start() - 60)
        end = min(len(body), match.end() + 60)
        window = body[start:end]
        if any(cue in window for cue in _STRICT_YOE_CUES):
            window_candidates.append((lo, hi))

    if window_candidates:
        window_candidates.sort(key=lambda pair: (pair[0], pair[1] or pair[0]))
        return window_candidates[0]
    return None, None


def _find_first_label(text: str, rules: list[tuple[str, re.Pattern[str]]]) -> str | None:
    for label, pattern in rules:
        if pattern.search(text):
            return label
    return None


def _find_all_labels(text: str, rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    return [label for label, pattern in rules if pattern.search(text)]


def _find_salary(text: str) -> tuple[int | None, int | None]:
    match = _SALARY_RE.search(text)
    if not match:
        return None, None
    return _parse_salary(match.group("lo")), _parse_salary(match.group("hi"))


def _parse_salary(token: str) -> int | None:
    token = token.lower().replace(",", "").strip()
    if not token:
        return None
    if token.endswith("k"):
        try:
            return int(float(token[:-1]) * 1000)
        except ValueError:
            return None
    try:
        return int(token)
    except ValueError:
        return None


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?\n])\s+")


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
