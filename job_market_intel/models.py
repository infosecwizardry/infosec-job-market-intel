from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RoleBucket = Literal["junior_soc", "help_desk_it_admin", "unclassified"]
RemoteArrangement = Literal["remote", "hybrid", "onsite", "unspecified"]
Seniority = Literal["entry", "mid", "senior", "unclear"]


@dataclass(slots=True)
class Listing:
    """A single (deduped) job listing."""

    listing_id: str  # SHA256 of normalize(company)|normalize(title)|normalize(location)
    title: str
    company: str
    location: str
    description: str
    role_bucket: RoleBucket
    sources: list[str] = field(default_factory=list)  # e.g. ["indeed", "linkedin"]
    source_urls: list[str] = field(default_factory=list)
    posted_at: str | None = None  # ISO8601 or None
    fetched_at: str = ""
    extracted: ExtractedRequirements | None = None


@dataclass(slots=True)
class ExtractedRequirements:
    """Structured requirements derived from a listing's description."""

    certifications: list[str] = field(default_factory=list)
    years_experience_min: int | None = None
    years_experience_max: int | None = None
    degree: str | None = None  # "bachelor" / "associate" / "high_school" / "equivalent" / None
    clearance: str | None = None  # "secret" / "top_secret" / "ts_sci" / "public_trust" / None
    salary_min: int | None = None
    salary_max: int | None = None
    schedule_signals: list[str] = field(default_factory=list)  # e.g. ["swing", "on_call"]
    technical_skills: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    seniority_signal: Seniority = "unclear"
    remote_arrangement: RemoteArrangement = "unspecified"
    llm_used: bool = False


@dataclass(slots=True)
class Snapshot:
    """The full output of a single tool run."""

    schema_version: str
    generated_at: str
    tool_version: str
    input_params: dict
    summary: dict
    listings: list[Listing] = field(default_factory=list)
