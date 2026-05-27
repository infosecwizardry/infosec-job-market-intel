"""Claude-powered enrichment for fields regex can't reliably catch.

Cheap and skippable: uses Haiku 4.5 with prompt caching. The cached prefix
(system prompt + JSON schema + few-shot example) is large and stable; only the
per-listing user message varies, so 90%+ of input tokens are cache hits.

Per-listing extractions are cached on disk keyed by the listing's SHA hash, so
re-runs over the same dataset are no-op (no API calls).

Fields produced:
- responsibilities: top 3-5 free-form phrases
- technical_skills: top 5 (LLM pass merges with regex pass downstream)
- seniority_signal: entry/mid/senior/unclear (titles lie; this looks at the body)
- remote_arrangement: remote/hybrid/onsite/unspecified
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ..models import ExtractedRequirements, Listing

LLM_MODEL = "claude-haiku-4-5-20251001"
MAX_DESCRIPTION_CHARS = 8000  # truncate very long listings; signal saturates well before

_SYSTEM_PROMPT = """You extract structured requirements from a single job listing.

Return ONLY a JSON object with this exact shape, no prose, no markdown fences:

{
  "responsibilities": ["string", ...],          // 3-5 short phrases (5-12 words each)
  "technical_skills": ["string", ...],          // up to 5 named tools/technologies
  "seniority_signal": "entry|mid|senior|unclear",
  "remote_arrangement": "remote|hybrid|onsite|unspecified"
}

Rules:
- "entry" means 0-2 years required AND no senior/lead language.
- "senior" requires 5+ years OR explicit lead/principal/staff title language.
- "remote" means fully remote (no in-office requirement). "hybrid" means part on-site.
- Use "unclear"/"unspecified" when the listing genuinely doesn't say. Do not guess.
- For responsibilities and technical_skills, prefer the listing's own phrasing.
- Output must be valid JSON. Do not include comments or trailing commas."""


class _ClaudeProtocol(Protocol):
    def __call__(self, *, description: str) -> dict: ...


class ClaudeExtractor:
    """Wraps the Anthropic SDK with prompt caching + on-disk extraction cache."""

    def __init__(self, *, api_key: str | None, cache_dir: Path) -> None:
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = None

    def enrich(self, listing: Listing, base: ExtractedRequirements) -> tuple[ExtractedRequirements, list[str]]:
        """Apply Claude-extracted fields on top of the regex-extracted base.

        Returns (enriched, warnings). On any failure, returns base unchanged with a warning.
        """
        warnings: list[str] = []
        cache_path = self.cache_dir / f"{listing.listing_id}.json"

        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return _merge(base, cached), warnings
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"llm cache read failed for {listing.listing_id}: {exc}")

        if not self.api_key:
            warnings.append("ANTHROPIC_API_KEY not set; skipping LLM enrichment.")
            return base, warnings

        try:
            payload = self._call_claude(description=listing.description)
        except Exception as exc:
            warnings.append(f"llm call failed for {listing.listing_id}: {exc}")
            return base, warnings

        try:
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            warnings.append(f"llm cache write failed for {listing.listing_id}: {exc}")

        return _merge(base, payload), warnings

    def _call_claude(self, *, description: str) -> dict:
        from anthropic import Anthropic  # local import — keep import optional

        if self._client is None:
            self._client = Anthropic(api_key=self.api_key)

        body = (description or "").strip()[:MAX_DESCRIPTION_CHARS]

        response = self._client.messages.create(
            model=LLM_MODEL,
            max_tokens=600,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": f"Job listing:\n\n{body}"}],
        )

        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return _parse_json(text)


def _parse_json(text: str) -> dict:
    """Tolerant JSON parse: strip optional ```json fences, find outermost {...}."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Strip ```json ... ``` fences if Claude added them despite the instruction.
        stripped = stripped.lstrip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip("` \n")

    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ValueError("no JSON object in response")
    return json.loads(stripped[first : last + 1])


def _merge(base: ExtractedRequirements, payload: dict) -> ExtractedRequirements:
    """Layer LLM payload onto the regex-extracted base, keeping regex values where they exist."""
    out = ExtractedRequirements(**asdict(base))
    out.llm_used = True

    responsibilities = payload.get("responsibilities", [])
    if isinstance(responsibilities, list):
        out.responsibilities = [str(r).strip() for r in responsibilities if str(r).strip()][:5]

    skills = payload.get("technical_skills", [])
    if isinstance(skills, list):
        merged = list(out.technical_skills)
        for s in skills:
            s_str = str(s).strip()
            if s_str and s_str not in merged:
                merged.append(s_str)
        out.technical_skills = merged[:10]

    seniority = payload.get("seniority_signal")
    if seniority in {"entry", "mid", "senior", "unclear"}:
        out.seniority_signal = seniority  # type: ignore[assignment]

    arrangement = payload.get("remote_arrangement")
    if arrangement in {"remote", "hybrid", "onsite", "unspecified"}:
        out.remote_arrangement = arrangement  # type: ignore[assignment]

    return out
