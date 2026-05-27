"""Re-filter an existing snapshot without re-scraping.

Useful when you've already paid the time/CPU cost of a big JobSpy scrape and
want to tune the filter parameters (seniority levels, freshness window, role
buckets) against the same source data.

Invoked via the CLI: `job-market-intel --reclassify reports/snapshot-X.json ...`.
Writes a new snapshot to <output-dir>/snapshot-<today>.json overwriting if
exists, plus the matching CSV + markdown + appended trend.csv row.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .models import ExtractedRequirements, Listing
from .pipeline import _is_fresh
from .reporting import append_trend_csv, render_markdown_report, stats_to_dict, write_listings_csv, write_snapshot_json
from .scoring import tabulate
from .seeds import classify_role as _classify_role_from_title
from .seeds import classify_seniority as _classify_title_seniority
from .seeds import (
    classify_seniority_combined,
    is_bureaucratic_metadata_only,
    is_compliance_role,
    is_physical_security_role,
)


def reclassify_snapshot(
    *,
    input_path: Path,
    output_dir: Path,
    freshness_days: int,
    allowed_seniority: list[str],
    include_unclassified: bool,
    role_buckets: list[str],
    today: str | None = None,
    llm_extractor=None,
    min_description_chars: int = 50,
    mine_unclear: bool = False,
    enrich_filtered: bool = False,
) -> int:
    """Apply the current filter set to an existing snapshot.

    If `llm_extractor` is provided, it runs LLM enrichment on listings that
    don't already have llm_used=True. Otherwise reclassify is title-only.

    If `mine_unclear=True` AND the extractor exposes `enrich_unclear_batch`,
    we use the batched path (one CLI call per ~25 listings) and additionally
    emit `reports/discovered-patterns-<date>.md` aggregating the LLM-cited
    evidence phrases for human review.

    If `enrich_filtered=True` AND the extractor exposes `enrich_full_batch`,
    we run the full extraction prompt on EVERY listing that survives the
    filters — pulls certifications, YoE, degree, and clearance the regex
    extractor missed. This is broader than the title-unclear targeting and
    closes the "regex didn't catch this cert" gap end-to-end.

    Returns CLI exit code.
    """
    if not input_path.exists():
        print(f"ERROR: snapshot file not found: {input_path}")
        return 2

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not parse snapshot file: {exc}")
        return 2

    raw_listings_data = payload.get("listings", []) or []
    print(f"Loaded {len(raw_listings_data)} listings from {input_path.name}")

    listings: list[Listing] = []
    for record in raw_listings_data:
        extracted_data = record.get("extracted") or {}
        listings.append(
            Listing(
                listing_id=record.get("listing_id", ""),
                title=record.get("title", ""),
                company=record.get("company", ""),
                location=record.get("location", ""),
                description=record.get("description", ""),
                role_bucket=record.get("role_bucket", "unclassified"),
                sources=list(record.get("sources", [])),
                source_urls=list(record.get("source_urls", [])),
                posted_at=record.get("posted_at"),
                fetched_at=record.get("fetched_at", ""),
                extracted=ExtractedRequirements(**extracted_data) if extracted_data else None,
            )
        )

    # Step 0.4: re-run title-based role classification. The snapshot file's
    # cached role_bucket reflects the keyword list at scrape time; we want
    # to pick up any changes to _JUNIOR_SOC_KEYWORDS / _HELP_DESK_KEYWORDS /
    # the non-IT-department prefix filter since.
    rebucket_changes = 0
    for li in listings:
        new_bucket = _classify_role_from_title(li.title)
        if new_bucket != li.role_bucket:
            rebucket_changes += 1
        li.role_bucket = new_bucket  # type: ignore[assignment]

    # Step 0.5: physical-security disambiguator — re-label SOC titles whose
    # descriptions describe physical (not cyber) security as 'unclassified'.
    physical_security_relabeled = 0
    for li in listings:
        if li.role_bucket == "junior_soc" and is_physical_security_role(li.description):
            li.role_bucket = "unclassified"  # type: ignore[assignment]
            physical_security_relabeled += 1

    # Step 0.6: compliance/GRC disambiguator — "Information Security Analyst"
    # postings often match the SOC keyword list but describe GRC/audit work,
    # not SIEM monitoring. Demote those to 'unclassified' so they don't
    # pollute the junior_soc bucket.
    compliance_relabeled = 0
    for li in listings:
        if li.role_bucket == "junior_soc" and is_compliance_role(li.description):
            li.role_bucket = "unclassified"  # type: ignore[assignment]
            compliance_relabeled += 1

    # Step 1: role-bucket filter
    allowed_buckets = set(role_buckets)
    if include_unclassified:
        allowed_buckets.add("unclassified")
    pre = len(listings)
    listings = [li for li in listings if li.role_bucket in allowed_buckets]
    dropped_off_topic = pre - len(listings)

    # Step 1.5: drop listings with no usable description body OR with
    # bureaucratic-only descriptions (civil-service postings that publish
    # nothing but metadata fields).
    pre = len(listings)
    if min_description_chars > 0:
        listings = [li for li in listings if len((li.description or "").strip()) >= min_description_chars]
    listings = [li for li in listings if not is_bureaucratic_metadata_only(li.description)]
    dropped_no_description = pre - len(listings)

    # Step 2: freshness filter
    pre = len(listings)
    listings = [li for li in listings if _is_fresh(li.posted_at, freshness_days)]
    dropped_stale = pre - len(listings)

    # Step 2.5: optional LLM enrichment — ONLY for listings that would STILL
    # be 'unclear' after every regex pass (title classifier + description
    # classifier + YoE corroboration). Skipped automatically when the full
    # pass above already ran (every listing has llm_used=True at that point).
    # Skip if:
    #   - a previous LLM run already labeled this listing (llm_used flag), OR
    #   - title alone classifies (Senior/Junior/Director/L1/etc.), OR
    #   - the description-regex fallback (classify_seniority_combined) returns
    #     something other than "unclear" — that's the same logic Step 3 below
    #     applies, so re-running it here gives us the FINAL would-be seniority.
    clues_by_seniority: dict[str, list[str]] = {}
    if llm_extractor is not None:
        to_enrich_idx: list[int] = []
        bases: list[ExtractedRequirements] = []
        skipped_title = 0
        skipped_desc = 0
        skipped_prior_llm = 0
        for i, li in enumerate(listings):
            if li.extracted and li.extracted.llm_used:
                skipped_prior_llm += 1
                continue  # already enriched in a prior run
            if _classify_title_seniority(li.title) != "unclear":
                skipped_title += 1
                continue  # title alone is enough
            yoe_min = li.extracted.years_experience_min if li.extracted else None
            would_be = classify_seniority_combined(li.title, li.description, yoe_min)
            if would_be != "unclear":
                skipped_desc += 1
                continue  # description regex already nails it — no LLM needed
            to_enrich_idx.append(i)
            bases.append(li.extracted if li.extracted is not None else ExtractedRequirements())

        print(
            f"  LLM-target selection: skipped {skipped_prior_llm} prior-LLM, "
            f"{skipped_title} clear-title, {skipped_desc} clear-via-desc-regex; "
            f"{len(to_enrich_idx)} truly-unclear remain"
        )

        if to_enrich_idx:
            print(
                f"Enriching {len(to_enrich_idx)} listings via LLM (concurrency={getattr(llm_extractor, 'concurrency', 1)})..."
            )
            targets = [listings[i] for i in to_enrich_idx]

            done_count = [0]

            def _progress(done: int, total: int) -> None:
                done_count[0] = done
                if done == 1 or done % 50 == 0 or done == total:
                    print(f"  enriched {done}/{total}")

            # Prefer the batched path when mine_unclear is on and the extractor
            # exposes enrich_unclear_batch — ~25x fewer subprocess spawns AND it
            # returns the LLM-cited evidence phrases we mine into regex candidates.
            warns: list[str]
            if mine_unclear and hasattr(llm_extractor, "enrich_unclear_batch"):
                print("  (batched mode: one CLI call per ~25 listings, mining clues)")
                enriched_list, clues_by_seniority, warns = llm_extractor.enrich_unclear_batch(
                    targets, bases, progress_cb=_progress
                )
            elif hasattr(llm_extractor, "enrich_many"):
                enriched_list, warns = llm_extractor.enrich_many(targets, bases, progress_cb=_progress)
            else:
                enriched_list, warns = [], []
                for li, base in zip(targets, bases, strict=False):
                    e, w = llm_extractor.enrich(li, base)
                    enriched_list.append(e)
                    warns.extend(w)

            for idx, enriched in zip(to_enrich_idx, enriched_list, strict=False):
                listings[idx].extracted = enriched
            for w in warns[:5]:
                print(f"  WARN: {w}")
            if len(warns) > 5:
                print(f"  ({len(warns) - 5} more warnings suppressed)")

    # Step 3: re-classify seniority. Priority:
    #   1. Senior/leadership title — always authoritative.
    #   2. YoE veto: yoe_min >= 3 → never entry, regardless of title or LLM.
    #      A "Junior Specialist" asking for 5 years is not entry-level.
    #   3. LLM's seniority_signal (if LLM ran) — saw the full description.
    #   4. Otherwise: classify_seniority_combined handles title/desc/yoe logic.
    from .seeds import _yoe_to_bucket
    from .seeds import classify_seniority as _classify_title

    for li in listings:
        yoe_min = li.extracted.years_experience_min if li.extracted else None

        if li.extracted is None:
            level = classify_seniority_combined(li.title, li.description, yoe_min)
            li.extracted = ExtractedRequirements(seniority_signal=level)  # type: ignore[arg-type]
            continue

        title_level = _classify_title(li.title)

        # Senior/leadership titles always win.
        if title_level in ("senior", "leadership"):
            li.extracted.seniority_signal = title_level  # type: ignore[assignment]
            continue

        # YoE veto applies before either title or LLM is trusted.
        if yoe_min is not None and yoe_min >= 3:
            li.extracted.seniority_signal = _yoe_to_bucket(yoe_min)  # type: ignore[assignment]
            continue

        if title_level != "unclear":
            li.extracted.seniority_signal = title_level  # type: ignore[assignment]
        elif li.extracted.llm_used:
            # Trust LLM's answer for unclear-title listings (no YoE veto fired).
            pass
        else:
            li.extracted.seniority_signal = classify_seniority_combined(  # type: ignore[assignment]
                li.title, li.description, yoe_min
            )

    # Step 4: seniority filter
    allowed = set(allowed_seniority)
    pre = len(listings)
    listings = [li for li in listings if li.extracted and li.extracted.seniority_signal in allowed]
    dropped_seniority = pre - len(listings)

    print(
        f"After filters: {len(listings)} listings "
        f"(re-bucketed {rebucket_changes} via fresh title classifier, "
        f"dropped {dropped_off_topic} off-topic, {dropped_no_description} no-desc, "
        f"{dropped_stale} stale, {dropped_seniority} wrong seniority, "
        f"relabeled {physical_security_relabeled} physical-security SOCs, "
        f"relabeled {compliance_relabeled} compliance/GRC SOCs)"
    )

    # Step 5 (analyze): optional FULL LLM enrichment pass. Runs ONLY on the
    # final survivors of every filter above (~400 listings instead of ~1200),
    # so we don't spend LLM cycles on listings that get dropped anyway. The
    # merge logic in _merge() is additive: LLM-found certs supplement the
    # regex matches; LLM-extracted YoE / degree / clearance fill only when
    # regex left them blank. Pure widening — never removes information.
    if enrich_filtered and llm_extractor is not None and hasattr(llm_extractor, "enrich_full_batch"):
        full_targets = list(listings)
        full_bases = [li.extracted if li.extracted is not None else ExtractedRequirements() for li in full_targets]
        print(
            f"Full LLM analysis pass: {len(full_targets)} surviving listings "
            f"(concurrency={getattr(llm_extractor, 'concurrency', 1)})..."
        )

        full_done = [0]

        def _full_progress(done: int, total: int) -> None:
            full_done[0] = done
            if done == 1 or done % 5 == 0 or done == total:
                print(f"  enrichment batch {done}/{total}")

        full_enriched, full_warns = llm_extractor.enrich_full_batch(
            full_targets, full_bases, progress_cb=_full_progress
        )
        for i, enriched in enumerate(full_enriched):
            listings[i].extracted = enriched
        for w in full_warns[:5]:
            print(f"  WARN: {w}")
        if len(full_warns) > 5:
            print(f"  ({len(full_warns) - 5} more warnings suppressed)")

    # Tabulate + write new snapshot
    stats_by_bucket = tabulate(listings)
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    date_str = today or generated_at[:10]

    out_payload = {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "tool_version": __version__,
        "input": {
            **(payload.get("input", {}) or {}),
            "reclassified_from": str(input_path),
            "freshness_days": freshness_days,
            "allowed_seniority": allowed_seniority,
            "include_unclassified": include_unclassified,
            "role_buckets": role_buckets,
        },
        "summary": {
            "total_listings_pre_dedup": payload.get("summary", {}).get("total_listings_pre_dedup", 0),
            "total_listings_post_dedup": len(listings),
            "dropped_off_topic": dropped_off_topic,
            "dropped_no_description": dropped_no_description,
            "dropped_stale": dropped_stale,
            "dropped_seniority": dropped_seniority,
            "per_source_pre_dedup": payload.get("summary", {}).get("per_source_pre_dedup", {}),
            "listings_with_llm_extraction": sum(1 for li in listings if li.extracted and li.extracted.llm_used),
            "listings_regex_only": sum(1 for li in listings if li.extracted and not li.extracted.llm_used),
            "duration_seconds": 0.0,
        },
        "stats_by_bucket": {bucket: stats_to_dict(stats) for bucket, stats in stats_by_bucket.items()},
        "listings": [asdict(li) for li in listings],
        "warnings": payload.get("warnings", []) or [],
    }

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    snap_path = out_dir / f"snapshot-{date_str}.json"
    csv_path = out_dir / f"snapshot-{date_str}.csv"
    md_path = out_dir / f"report-{date_str}.md"
    trend_path = out_dir / "trend.csv"

    write_snapshot_json(out_payload, output_path=snap_path)
    write_listings_csv(listings, output_path=csv_path)
    md = render_markdown_report(
        generated_at=generated_at,
        tool_version=__version__,
        stats_by_bucket=stats_by_bucket,
        prior_stats_by_bucket=None,
        warnings=[],
    )
    md_path.write_text(md, encoding="utf-8")
    append_trend_csv(stats_by_bucket, date_str=date_str, output_path=trend_path)

    extra_paths = []
    # When mine_unclear is on and the LLM returned at least one clue, write a
    # discovered-patterns markdown report alongside the normal outputs.
    if mine_unclear and any(clues_by_seniority.values()):
        from .pattern_mining import mine_patterns, render_markdown

        buckets = mine_patterns(clues_by_seniority, min_freq=3)
        total_clues = sum(len(v) for v in clues_by_seniority.values())
        patterns_md = render_markdown(
            buckets,
            generated_at=generated_at,
            source_count=total_clues,
        )
        patterns_path = out_dir / f"discovered-patterns-{date_str}.md"
        patterns_path.write_text(patterns_md, encoding="utf-8")
        extra_paths.append(patterns_path.name)

    wrote_names = ", ".join([snap_path.name, csv_path.name, md_path.name, *extra_paths])
    print(f"Wrote {wrote_names}")
    return 0
