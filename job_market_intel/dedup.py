from __future__ import annotations

import hashlib

from .models import Listing
from .seeds import normalize_phrase


def listing_hash(company: str, title: str, location: str) -> str:
    key = f"{normalize_phrase(company)}|{normalize_phrase(title)}|{normalize_phrase(location)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def dedup_listings(listings: list[Listing]) -> list[Listing]:
    """Collapse listings sharing the same (company, title, location) hash.

    The merged record keeps the longest description and unions source labels + URLs.
    """
    by_hash: dict[str, Listing] = {}
    for listing in listings:
        existing = by_hash.get(listing.listing_id)
        if existing is None:
            by_hash[listing.listing_id] = listing
            continue

        for src in listing.sources:
            if src not in existing.sources:
                existing.sources.append(src)
        for url in listing.source_urls:
            if url and url not in existing.source_urls:
                existing.source_urls.append(url)

        if len(listing.description or "") > len(existing.description or ""):
            existing.description = listing.description

        if listing.posted_at and (not existing.posted_at or listing.posted_at < existing.posted_at):
            existing.posted_at = listing.posted_at

    return list(by_hash.values())
