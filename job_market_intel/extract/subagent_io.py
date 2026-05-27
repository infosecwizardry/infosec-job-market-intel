"""Subagent-based LLM enrichment — chunking + merging helpers.

Why this exists: on Windows, spawning `claude.exe` as a subprocess pops up
console windows (one per spawn, even with CREATE_NO_WINDOW set, because
claude.exe's internal child processes allocate their own consoles). The
alternative is to dispatch the LLM work as subagents inside a Claude Code
session — those run in the parent agent's context, no subprocesses, no
windows.

This module DOES NOT dispatch the agents itself (the Agent tool only exists
inside a Claude Code session, not in plain Python). Instead it provides the
two endpoints a Claude Code skill calls:

  1. `prepare_chunks(snapshot, output_dir, chunk_size)` — splits the
     post-filter listings into N JSON chunk files plus a manifest.
     The skill then dispatches N parallel Agent calls, one per chunk.

  2. `merge_chunks(snapshot, results_dir, output_dir, today)` — reads the
     per-chunk result files the agents wrote and assembles the final
     snapshot (same merge rules as _merge in cli_llm.py).

CLI shim: `python -m job_market_intel subagent-prepare ...` and
`python -m job_market_intel subagent-merge ...` invoke these directly.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from ..models import ExtractedRequirements, Listing
from ..reporting import (
    append_trend_csv,
    render_markdown_report,
    stats_to_dict,
    write_listings_csv,
    write_snapshot_json,
)
from ..scoring import tabulate

# Per-listing description truncation. Set high since each chunk goes to its
# own agent with a fresh 200K context — no need to be stingy.
MAX_CHUNK_DESCRIPTION_CHARS = 7000

# What the prompt asks each agent to return per listing.
SUBAGENT_SCHEMA_DESCRIPTION = (
    "For EACH listing, return one JSON object inside a JSON array. "
    "The output must be ONE JSON array — no markdown fences, no preamble, no commentary. "
    "Each object: "
    "{"
    '"id": copy from input, '
    '"seniority_signal": one of "entry","mid","senior","leadership","unclear", '
    '"remote_arrangement": one of "remote","hybrid","onsite","unspecified", '
    '"level_clues": array of 1-3 short verbatim phrases that justified the seniority '
    '(empty array if "unclear"), '
    '"technical_skills": array of up to 8 short tool/skill names (e.g. "Splunk","Active Directory"), '
    '"responsibilities": array of 2-3 short phrases, '
    '"certifications": array of cert short-names mentioned/required. Use canonical short-names: '
    '"Security+","CISSP","CCNA","CEH","CISA","CISM","OSCP","CySA+","ITIL","AZ-104","AZ-500","SC-200",'
    '"AWS Certified Cloud Practitioner","AWS Certified Solutions Architect","Network+","A+","Linux+","CompTIA Cloud+",'
    '"MS-900","Splunk Core Certified User". Include BOTH required and preferred certs. '
    '"years_experience_min": integer or null (e.g. "3+ years" -> 3). Null if not stated. '
    '"years_experience_max": integer or null (upper bound of range). Null if open-ended. '
    '"degree": one of "high_school","associate","bachelor","master","phd","none","unspecified". '
    'Use "unspecified" when degree is not mentioned. '
    '"clearance": one of "secret","top_secret","ts_sci","public_trust","none". Use "none" if not mentioned'
    "}. "
    "Seniority rules: entry = 0-2 years with no senior language; senior = 5+ years OR explicit lead/principal/staff; "
    "leadership = manager/director/VP; mid = 2-4 years OR explicit Tier 2; unclear = genuinely unknown."
)


def _listing_to_chunk_item(listing: Listing | dict) -> dict:
    """Compact form sent to the subagent — id + title + truncated description."""
    if isinstance(listing, dict):
        return {
            "id": listing.get("listing_id", ""),
            "title": listing.get("title", ""),
            "description": (listing.get("description", "") or "")[:MAX_CHUNK_DESCRIPTION_CHARS],
        }
    return {
        "id": listing.listing_id,
        "title": listing.title,
        "description": (listing.description or "")[:MAX_CHUNK_DESCRIPTION_CHARS],
    }


def prepare_chunks(
    snapshot_path: Path,
    *,
    output_dir: Path,
    chunk_size: int = 50,
) -> dict:
    """Split a snapshot's listings into N chunk files for parallel agent dispatch.

    Writes:
      - <output_dir>/chunk-001.json, chunk-002.json, ... (each with N listings)
      - <output_dir>/manifest.json — paths + the snapshot path + the per-agent
        prompt the skill should give each subagent.

    Returns the manifest dict.
    """
    snapshot_path = Path(snapshot_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Wipe previous chunks so stale files don't get merged later.
    for f in output_dir.glob("chunk-*.json"):
        f.unlink()
    for f in output_dir.glob("chunk-*-result.json"):
        f.unlink()

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    listings = payload.get("listings", []) or []
    chunk_paths: list[str] = []
    for chunk_idx in range(0, len(listings), chunk_size):
        chunk = listings[chunk_idx : chunk_idx + chunk_size]
        items = [_listing_to_chunk_item(li) for li in chunk]
        chunk_num = chunk_idx // chunk_size + 1
        chunk_file = output_dir / f"chunk-{chunk_num:03d}.json"
        chunk_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        chunk_paths.append(str(chunk_file.resolve()))

    manifest = {
        "snapshot_path": str(snapshot_path.resolve()),
        "chunk_paths": chunk_paths,
        "chunk_size": chunk_size,
        "total_listings": len(listings),
        "prompt_template": (
            "You are processing a chunk of job-market listings for LLM enrichment. "
            "Read the JSON array of listings from the input file, extract structured "
            "requirements for each, and write the result JSON array to the output file.\n\n"
            f"{SUBAGENT_SCHEMA_DESCRIPTION}\n\n"
            "Input file: {input_path}\n"
            "Output file: {output_path}\n\n"
            "Use the Read tool to load the input, then process each listing in the array. "
            "Write the resulting JSON array (one object per input listing, keyed by id) "
            "to the output file using the Write tool. Do not add any prose or commentary "
            "outside the JSON array."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Merge: reads chunk-NNN-result.json files and applies LLM extractions to
# the snapshot the chunks came from. Same merge rules as cli_llm._merge.
# ---------------------------------------------------------------------------

_VALID_DEGREE_VALUES = {"high_school", "associate", "bachelor", "master", "phd", "none", "unspecified"}
_VALID_CLEARANCE_VALUES = {"secret", "top_secret", "ts_sci", "public_trust", "none"}


def _merge_into_extracted(base: ExtractedRequirements, payload: dict) -> ExtractedRequirements:
    """Same merge rules as cli_llm._merge — kept in sync deliberately.

    - certifications / technical_skills: UNION
    - responsibilities: REPLACE
    - seniority_signal / remote_arrangement: REPLACE if LLM returned a valid value
    - years_experience_min/_max: FILL only if base is None
    - degree / clearance: FILL only if base is None AND LLM value isn't a sentinel
    """
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

    certs = payload.get("certifications", [])
    if isinstance(certs, list):
        merged_c = list(out.certifications)
        for c in certs:
            c_str = str(c).strip()
            if c_str and c_str not in merged_c:
                merged_c.append(c_str)
        out.certifications = merged_c[:15]

    seniority = payload.get("seniority_signal")
    if seniority in {"entry", "mid", "senior", "leadership", "unclear"}:
        out.seniority_signal = seniority  # type: ignore[assignment]

    arrangement = payload.get("remote_arrangement")
    if arrangement in {"remote", "hybrid", "onsite", "unspecified"}:
        out.remote_arrangement = arrangement  # type: ignore[assignment]

    yoe_min_raw = payload.get("years_experience_min")
    if out.years_experience_min is None and isinstance(yoe_min_raw, int) and 0 <= yoe_min_raw <= 30:
        out.years_experience_min = yoe_min_raw
    yoe_max_raw = payload.get("years_experience_max")
    if out.years_experience_max is None and isinstance(yoe_max_raw, int) and 0 <= yoe_max_raw <= 40:
        out.years_experience_max = yoe_max_raw

    degree_raw = payload.get("degree")
    if (
        out.degree is None
        and isinstance(degree_raw, str)
        and degree_raw in _VALID_DEGREE_VALUES
        and degree_raw not in ("unspecified", "none")
    ):
        out.degree = degree_raw

    clearance_raw = payload.get("clearance")
    if (
        out.clearance is None
        and isinstance(clearance_raw, str)
        and clearance_raw in _VALID_CLEARANCE_VALUES
        and clearance_raw != "none"
    ):
        out.clearance = clearance_raw

    return out


def merge_chunks(
    snapshot_path: Path,
    *,
    results_dir: Path,
    output_dir: Path,
    today: str | None = None,
    allowed_seniority: list[str] | None = None,
) -> dict:
    """Apply per-chunk LLM results to the snapshot and write the final outputs.

    Reads every `chunk-NNN-result.json` in `results_dir`, indexes by listing_id,
    merges into the corresponding listing's `extracted` field. After merging,
    re-applies BOTH:

      (1) The compliance/GRC disambiguator + physical-security disambiguator,
          so role mis-classifications surfaced by the LLM's description view
          (e.g. "Information Security Analyst" that turned out to be GRC) get
          demoted to 'unclassified' and filtered.

      (2) The seniority classifier — including the YoE veto — using the
          freshly-extracted YoE the LLM filled in. Listings the LLM correctly
          tagged as mid/senior (or where extracted YoE >= 3) get dropped from
          the entry/unclear pool, instead of leaking through because the pre-
          subagent snapshot had them as 'entry'.

    `allowed_seniority` defaults to ["entry", "unclear"] to match the rest of
    the pipeline. Set to None or an empty list to keep all seniority buckets.

    Returns a summary dict with counts (total, merged, missed, written paths,
    drop counts per filter).
    """
    from ..seeds import (
        _yoe_to_bucket,
        classify_seniority_combined,
        is_compliance_role,
        is_physical_security_role,
    )
    from ..seeds import (
        classify_seniority as _classify_title,
    )

    snapshot_path = Path(snapshot_path)
    results_dir = Path(results_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    listings_data = payload.get("listings", []) or []

    # Read every chunk-NNN-result.json and build {listing_id: extraction}.
    results_by_id: dict[str, dict] = {}
    result_files = sorted(results_dir.glob("chunk-*-result.json"))
    for rf in result_files:
        try:
            parsed = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  WARN: could not read {rf.name}: {exc}")
            continue
        if not isinstance(parsed, list):
            print(f"  WARN: {rf.name} is not a JSON array; skipping")
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                results_by_id[entry_id] = entry

    # Reconstruct Listing objects so the existing reporting helpers stay happy.
    listings: list[Listing] = []
    merged_count = 0
    missed_ids: list[str] = []
    for record in listings_data:
        extracted_data = record.get("extracted") or {}
        base = ExtractedRequirements(**extracted_data) if extracted_data else ExtractedRequirements()
        listing_id = record.get("listing_id", "")
        agent_result = results_by_id.get(listing_id)
        if agent_result is not None:
            merged_extracted = _merge_into_extracted(base, agent_result)
            merged_count += 1
        else:
            merged_extracted = base
            missed_ids.append(listing_id)
        listings.append(
            Listing(
                listing_id=listing_id,
                title=record.get("title", ""),
                company=record.get("company", ""),
                location=record.get("location", ""),
                description=record.get("description", ""),
                role_bucket=record.get("role_bucket", "unclassified"),
                sources=list(record.get("sources", [])),
                source_urls=list(record.get("source_urls", [])),
                posted_at=record.get("posted_at"),
                fetched_at=record.get("fetched_at", ""),
                extracted=merged_extracted,
            )
        )

    # Re-apply the role disambiguators with the LLM-enriched data in view.
    # The LLM filled in YoE/certs/skills which sometimes reveals that a
    # SOC-titled listing is actually GRC work, or that a description is
    # really physical-security after the LLM's reading.
    compliance_relabeled = 0
    physical_relabeled = 0
    for li in listings:
        if li.role_bucket != "junior_soc":
            continue
        if is_physical_security_role(li.description):
            li.role_bucket = "unclassified"  # type: ignore[assignment]
            physical_relabeled += 1
        elif is_compliance_role(li.description):
            li.role_bucket = "unclassified"  # type: ignore[assignment]
            compliance_relabeled += 1

    # Re-run the seniority classifier with the LLM-enriched YoE in hand.
    # Same priority as reclassify.py Step 3:
    #   1. Senior/leadership title — authoritative.
    #   2. YoE veto: yoe_min >= 3 → never entry.
    #   3. LLM's seniority_signal (we just merged it in).
    #   4. Otherwise: classify_seniority_combined.
    for li in listings:
        if li.extracted is None:
            li.extracted = ExtractedRequirements()
        yoe_min = li.extracted.years_experience_min
        title_level = _classify_title(li.title)
        if title_level in ("senior", "leadership"):
            li.extracted.seniority_signal = title_level  # type: ignore[assignment]
            continue
        if yoe_min is not None and yoe_min >= 3:
            li.extracted.seniority_signal = _yoe_to_bucket(yoe_min)  # type: ignore[assignment]
            continue
        if title_level != "unclear":
            li.extracted.seniority_signal = title_level  # type: ignore[assignment]
        elif li.extracted.llm_used:
            # Trust LLM's call — already merged.
            pass
        else:
            li.extracted.seniority_signal = classify_seniority_combined(  # type: ignore[assignment]
                li.title, li.description, yoe_min
            )

    # Apply the seniority filter (default keeps entry + unclear). Mirrors the
    # CLI's --seniority flag default.
    if allowed_seniority is None:
        allowed_seniority = ["entry", "unclear"]
    allowed = set(allowed_seniority)
    pre_seniority = len(listings)
    if allowed:
        listings = [li for li in listings if li.extracted and li.extracted.seniority_signal in allowed]
    dropped_seniority = pre_seniority - len(listings)

    # Apply the role filter too — drop anything we just demoted to 'unclassified'.
    pre_role = len(listings)
    listings = [li for li in listings if li.role_bucket in {"junior_soc", "help_desk_it_admin"}]
    dropped_role = pre_role - len(listings)

    stats_by_bucket = tabulate(listings)
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    date_str = today or generated_at[:10]

    out_payload = {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "tool_version": __version__,
        "input": {
            **(payload.get("input", {}) or {}),
            "subagent_merged_from": str(snapshot_path),
            "subagent_chunks_processed": len(result_files),
        },
        "summary": {
            "total_listings_pre_dedup": payload.get("summary", {}).get("total_listings_pre_dedup", 0),
            "total_listings_post_dedup": len(listings),
            "per_source_pre_dedup": payload.get("summary", {}).get("per_source_pre_dedup", {}),
            "listings_with_llm_extraction": sum(1 for li in listings if li.extracted and li.extracted.llm_used),
            "listings_regex_only": sum(1 for li in listings if li.extracted and not li.extracted.llm_used),
            "duration_seconds": 0.0,
        },
        "stats_by_bucket": {bucket: stats_to_dict(stats) for bucket, stats in stats_by_bucket.items()},
        "listings": [asdict(li) for li in listings],
        "warnings": payload.get("warnings", []) or [],
    }

    snap_path = output_dir / f"snapshot-{date_str}.json"
    csv_path = output_dir / f"snapshot-{date_str}.csv"
    md_path = output_dir / f"report-{date_str}.md"
    trend_path = output_dir / "trend.csv"
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

    return {
        "total": len(listings),
        "merged": merged_count,
        "missed": len(missed_ids),
        "missed_ids": missed_ids[:10],  # cap for log noise
        "result_files": len(result_files),
        "compliance_relabeled": compliance_relabeled,
        "physical_relabeled": physical_relabeled,
        "dropped_seniority": dropped_seniority,
        "dropped_role": dropped_role,
        "snapshot_path": str(snap_path),
        "csv_path": str(csv_path),
        "md_path": str(md_path),
    }
