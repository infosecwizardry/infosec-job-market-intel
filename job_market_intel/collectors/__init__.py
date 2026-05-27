"""Per-source job listing collectors.

Each collector exposes a single method:

    collect(*, queries, location, results_per_query, freshness_days) -> CollectorResult

Semantics:
    results_per_query: int  — 0 means unlimited (pull every match the source returns).
    freshness_days:    int  — only return listings posted within the last N days.
                              Sources with a native date filter use it; others rely
                              on the pipeline's cross-source post-filter.

Implementations should never raise on per-query failure; record warnings instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Listing


@dataclass(slots=True)
class CollectorResult:
    listings: list[Listing] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_name: str = ""


class Collector(Protocol):
    source_name: str

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int,
    ) -> CollectorResult: ...
