from __future__ import annotations

import argparse
from pathlib import Path

# Load .env BEFORE importing .auth, so the module-level env reads in auth.py
# see the values from the .env file. Silent if python-dotenv is missing.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:
    pass

from .auth import (
    DEFAULT_ANTHROPIC_SECRET_REF,
    DEFAULT_OP_PATH,
    DEFAULT_USAJOBS_SECRET_REF,
    KEYRING_SERVICE,
    load_credentials,
)
from .collectors.greenhouse import GreenhouseCollector
from .collectors.jobspy import JobSpyCollector
from .collectors.lever import LeverCollector
from .collectors.usajobs import USAJobsCollector
from .extract.cli_llm import ClaudeCliExtractor, is_cli_available
from .extract.llm import ClaudeExtractor
from .pipeline import Pipeline, PipelineOptions
from .seeds import GREENHOUSE_COMPANIES, LEVER_COMPANIES

ALL_SITES = ("jobspy", "usajobs", "greenhouse", "lever")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect, dedup, and extract requirements from junior SOC / IT support job listings."
    )
    parser.add_argument(
        "--sites",
        nargs="+",
        choices=ALL_SITES,
        default=["jobspy"],
        help=(
            "Which sources to query. Default: jobspy only (the highest-volume source). "
            "Pass 'greenhouse' / 'lever' / 'usajobs' explicitly to add them — "
            "they're opt-in because greenhouse yields ~13 listings/week and lever is empty."
        ),
    )
    parser.add_argument(
        "--role-buckets",
        nargs="+",
        choices=["junior_soc", "help_desk_it_admin"],
        default=["junior_soc", "help_desk_it_admin"],
    )
    parser.add_argument("--location", default="United States")
    parser.add_argument(
        "--no-remote-pass",
        action="store_true",
        help="Skip the second collection pass with location='Remote'.",
    )
    parser.add_argument(
        "--results-per-source",
        type=int,
        default=0,
        help="Soft cap on listings returned per source per query. 0 (default) = unlimited.",
    )
    parser.add_argument(
        "--freshness-days",
        type=int,
        default=14,
        help="Drop listings posted more than N days ago. Applied as a search filter where supported (USAJobs, JobSpy) and as a post-filter otherwise.",
    )
    parser.add_argument(
        "--min-description-chars",
        type=int,
        default=50,
        help="Drop listings with description bodies shorter than N characters. Set to 0 to keep everything. LinkedIn via JobSpy often returns no body — those listings can't be classified.",
    )
    parser.add_argument(
        "--seniority",
        nargs="+",
        choices=["entry", "mid", "senior", "leadership", "unclear"],
        default=["entry", "unclear"],
        help="Allowed seniority levels (title-based). Default: entry + unclear (drops senior/manager/director noise).",
    )
    parser.add_argument(
        "--include-unclassified",
        action="store_true",
        help="Keep listings the title classifier couldn't bucket. Default: drop them (most are off-topic full-text matches).",
    )
    parser.add_argument(
        "--reclassify",
        type=Path,
        default=None,
        metavar="SNAPSHOT_PATH",
        help="Skip scraping; load this existing snapshot file and re-filter it with current --seniority / --include-unclassified / --freshness-days flags. Writes a new snapshot under --output-dir.",
    )
    parser.add_argument(
        "--mine-unclear",
        action="store_true",
        help=(
            "During --reclassify, use the batched LLM path (enrich_unclear_batch) "
            "to classify any listing still 'unclear' after regex passes, AND emit "
            "reports/discovered-patterns-<date>.md aggregating the LLM-cited "
            "evidence phrases for human review."
        ),
    )
    parser.add_argument(
        "--enrich-filtered",
        action="store_true",
        help=(
            "During --reclassify, run a FULL LLM enrichment pass on every listing "
            "that survives the filters — pulls certifications, years-of-experience, "
            "degree, and clearance the regex extractor missed. Implies --llm-backend cli "
            "(or api). Cost: ~3-5 min on ~400 listings."
        ),
    )
    # ---- Subagent-orchestrated enrichment (window-free, Claude Code only) ----
    # These two subcommands are called by a Claude Code skill that dispatches
    # parallel Agent tool calls. Python can't dispatch agents itself (that
    # tool only exists in a Claude Code session), so we split prep + merge
    # into separate steps the skill orchestrates.
    parser.add_argument(
        "--subagent-prepare",
        type=Path,
        default=None,
        metavar="SNAPSHOT_PATH",
        help=(
            "Split the listings in SNAPSHOT_PATH into chunk files for parallel "
            "subagent dispatch. Writes chunk-NNN.json + manifest.json under "
            "--subagent-dir. Intended to be called by the llm-analyze-listings "
            "Claude Code skill."
        ),
    )
    parser.add_argument(
        "--subagent-merge",
        type=Path,
        default=None,
        metavar="SNAPSHOT_PATH",
        help=(
            "Read chunk-NNN-result.json files from --subagent-dir and merge them "
            "back into SNAPSHOT_PATH, writing the final snapshot/CSV/markdown to "
            "--output-dir. Pair with --subagent-prepare."
        ),
    )
    parser.add_argument(
        "--subagent-dir",
        type=Path,
        default=Path("cache/subagent-chunks"),
        help="Working directory for subagent chunk + result files.",
    )
    parser.add_argument(
        "--subagent-chunk-size",
        type=int,
        default=50,
        help="Listings per subagent chunk. Default 50 (gives ~8 parallel agents on a 400-listing snapshot).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip Claude enrichment (regex-only extraction).",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["cli", "api"],
        default="cli",
        help="LLM backend. 'cli' uses your Claude Code subscription via the `claude` CLI (no API key needed). 'api' uses the Anthropic SDK with ANTHROPIC_API_KEY/op://. Default: cli.",
    )
    parser.add_argument(
        "--llm-concurrency",
        type=int,
        default=4,
        help="Concurrent subprocess calls for --llm-backend cli. Default 4.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run collection + extraction but do not write outputs.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("cache"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports"),
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override the date stamp used in output filenames (YYYY-MM-DD). For testing.",
    )

    # Source-specific config
    parser.add_argument(
        "--greenhouse-companies",
        nargs="+",
        default=GREENHOUSE_COMPANIES,
        help="Greenhouse board slugs to query.",
    )
    parser.add_argument(
        "--lever-companies",
        nargs="+",
        default=LEVER_COMPANIES,
        help="Lever board slugs to query.",
    )
    parser.add_argument(
        "--jobspy-sites",
        nargs="+",
        default=["indeed", "linkedin", "zip_recruiter", "glassdoor", "google"],
        help="JobSpy sub-sites to scrape.",
    )

    # Auth
    parser.add_argument("--op-path", type=Path, default=DEFAULT_OP_PATH)
    parser.add_argument("--usajobs-secret-ref", default=DEFAULT_USAJOBS_SECRET_REF)
    parser.add_argument("--anthropic-secret-ref", default=DEFAULT_ANTHROPIC_SECRET_REF)
    parser.add_argument("--keyring-service", default=KEYRING_SERVICE)
    parser.add_argument(
        "--no-1password",
        action="store_true",
        help="Don't try 1Password — rely on keyring cache only. Useful for CI / offline runs.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subagent_prepare is not None:
        from .extract.subagent_io import prepare_chunks

        manifest = prepare_chunks(
            args.subagent_prepare,
            output_dir=args.subagent_dir,
            chunk_size=int(args.subagent_chunk_size),
        )
        n_chunks = len(manifest["chunk_paths"])
        print(f"Prepared {n_chunks} chunk file(s) of up to {args.subagent_chunk_size} listings each.")
        print(f"Manifest: {args.subagent_dir / 'manifest.json'}")
        print(f"Total listings to process: {manifest['total_listings']}")
        print("Next step: dispatch one Agent per chunk (see the llm-analyze-listings skill).")
        return 0

    if args.subagent_merge is not None:
        from .extract.subagent_io import merge_chunks

        summary = merge_chunks(
            args.subagent_merge,
            results_dir=args.subagent_dir,
            output_dir=args.output_dir,
            today=args.today,
        )
        print(f"Merged {summary['merged']} listings from {summary['result_files']} chunk result file(s).")
        print(
            f"Post-merge filters: dropped {summary['dropped_role']} via role disambiguators "
            f"({summary['compliance_relabeled']} compliance/GRC, {summary['physical_relabeled']} physical-security), "
            f"{summary['dropped_seniority']} via seniority filter. "
            f"Final: {summary['total']} listings."
        )
        if summary["missed"]:
            print(f"  WARN: {summary['missed']} listings had no agent result. First few IDs: {summary['missed_ids']}")
        print(
            f"Wrote: {Path(summary['snapshot_path']).name}, {Path(summary['csv_path']).name}, {Path(summary['md_path']).name}"
        )
        return 0

    if args.reclassify is not None:
        from .reclassify import reclassify_snapshot

        llm_extractor_for_reclassify = None
        if not args.no_llm:
            if args.llm_backend == "cli" and is_cli_available():
                llm_extractor_for_reclassify = ClaudeCliExtractor(
                    cache_dir=args.cache_dir / "extractions",
                    concurrency=args.llm_concurrency,
                )
            elif args.llm_backend == "api":
                # API path needs creds; fetch now since we skip the rest of main().
                api_creds, _ = load_credentials(
                    op_path=args.op_path,
                    usajobs_secret_ref=args.usajobs_secret_ref,
                    anthropic_secret_ref=args.anthropic_secret_ref,
                    service_name=args.keyring_service,
                    use_op=not args.no_1password,
                )
                llm_extractor_for_reclassify = ClaudeExtractor(
                    api_key=api_creds.anthropic_api_key,
                    cache_dir=args.cache_dir / "extractions",
                )

        return reclassify_snapshot(
            input_path=args.reclassify,
            output_dir=args.output_dir,
            freshness_days=args.freshness_days,
            min_description_chars=args.min_description_chars,
            allowed_seniority=args.seniority,
            include_unclassified=args.include_unclassified,
            role_buckets=args.role_buckets,
            today=args.today,
            llm_extractor=llm_extractor_for_reclassify,
            mine_unclear=args.mine_unclear,
            enrich_filtered=args.enrich_filtered,
        )

    creds, cred_warnings = load_credentials(
        op_path=args.op_path,
        usajobs_secret_ref=args.usajobs_secret_ref,
        anthropic_secret_ref=args.anthropic_secret_ref,
        service_name=args.keyring_service,
        use_op=not args.no_1password,
    )

    collectors = []
    extra_warnings: list[str] = list(cred_warnings)

    if "jobspy" in args.sites:
        collectors.append(JobSpyCollector(sites=args.jobspy_sites))

    if "usajobs" in args.sites:
        if creds.usajobs_email and creds.usajobs_api_key:
            collectors.append(USAJobsCollector(email=creds.usajobs_email, api_key=creds.usajobs_api_key))
        else:
            extra_warnings.append("usajobs collector skipped — missing email or API key.")

    if "greenhouse" in args.sites:
        collectors.append(GreenhouseCollector(company_slugs=args.greenhouse_companies))

    if "lever" in args.sites:
        collectors.append(LeverCollector(company_slugs=args.lever_companies))

    if not collectors:
        print("No collectors enabled.")
        for warning in extra_warnings:
            print(f"WARN: {warning}")
        return 2

    llm_extractor: ClaudeExtractor | ClaudeCliExtractor | None = None
    if not args.no_llm:
        if args.llm_backend == "cli":
            if not is_cli_available():
                extra_warnings.append(
                    "claude CLI not on PATH — falling back to --no-llm. "
                    "Install Claude Code or pass --llm-backend api."
                )
            else:
                llm_extractor = ClaudeCliExtractor(
                    cache_dir=args.cache_dir / "extractions",
                    concurrency=args.llm_concurrency,
                )
        else:  # api
            llm_extractor = ClaudeExtractor(
                api_key=creds.anthropic_api_key,
                cache_dir=args.cache_dir / "extractions",
            )

    options = PipelineOptions(
        location=args.location,
        also_remote=not args.no_remote_pass,
        results_per_source=args.results_per_source,
        freshness_days=args.freshness_days,
        min_description_chars=args.min_description_chars,
        allowed_seniority=args.seniority,
        include_unclassified=args.include_unclassified,
        role_buckets=args.role_buckets,
        use_llm=not args.no_llm,
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
        dry_run=args.dry_run,
        today=args.today,
    )
    pipeline = Pipeline(collectors=collectors, llm_extractor=llm_extractor, options=options)

    snapshot = pipeline.run()
    snapshot["warnings"] = sorted(set(list(snapshot.get("warnings", [])) + extra_warnings))

    summary = snapshot["summary"]
    print(
        f"Collected {summary['total_listings_pre_dedup']} raw -> "
        f"{summary['total_listings_post_dedup']} after dedup."
    )
    print(f"Per source (pre-dedup): {summary['per_source_pre_dedup']}")
    if args.dry_run:
        print("(--dry-run) outputs not written.")
    else:
        print(f"Outputs written to {args.output_dir.resolve()}")

    for warning in snapshot.get("warnings", []):
        print(f"WARN: {warning}")

    return 0
