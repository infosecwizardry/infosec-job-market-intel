"""Claude Code CLI-based enrichment.

Uses the locally-installed `claude` CLI (Claude Code) for LLM extraction
instead of the Anthropic API. Trade-offs vs the API path:

- ✓ Uses the user's existing Claude Code subscription (no API key needed)
- ✓ Same JSON output schema as the API extractor
- ✗ Higher per-call latency (~6-10s steady-state vs ~2s for direct API)
- ✗ Each subprocess invocation has CLI startup overhead

To compensate for the per-call overhead, this extractor implements concurrent
batch enrichment via ThreadPoolExecutor — multiple `claude` subprocesses run
in parallel, bounded by `concurrency`. Default 4 is a balance of throughput
and politeness toward the subscription's rate limits.

Same on-disk caching as the API extractor: per-listing JSON files keyed by
listing_id, so re-runs over the same data are no-ops.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from ..models import ExtractedRequirements, Listing


def _quiet_subprocess_kwargs() -> dict:
    """Spawn kwargs to keep child processes invisible + below-normal priority on Windows."""
    if sys.platform != "win32":
        return {}
    # CREATE_NO_WINDOW: don't allocate a console for the child claude.exe.
    # NOTE: `claude.exe` (a Node-bundled binary) may STILL spawn cmd.exe
    # helpers internally that pop visible consoles — that's a Node/claude
    # behavior we can't suppress from the parent on Windows. The pragmatic
    # workaround is to use the API backend (--llm-backend api) for batched
    # work where you don't want pop-ups.
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    below_normal = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": create_no_window | below_normal,
        "startupinfo": startupinfo,
    }


def _resolve_claude_invocation(override: str | None = None) -> list[str]:
    """Return the argv list that invokes Claude Code most quietly on this machine.

    Preferred path (Windows): the real `claude.exe` binary that ships with the
    npm package (Anthropic.Claude_Code shipped a native binary in late 2025).
    Bypasses cmd.exe / PowerShell / node.exe entirely, so no console windows
    leak when spawned with CREATE_NO_WINDOW.

    Fallback: whatever `shutil.which('claude')` returns (typically the
    `claude.CMD` or `claude.ps1` shim) — works but may briefly flash a
    console window depending on the platform.
    """
    if override:
        return [override]
    # Direct binary lookup (Windows npm default install location).
    win_exe = Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    if win_exe.exists():
        return [str(win_exe)]
    # Generic discovery: walk from whichever claude wrapper is on PATH and
    # look for bin/claude.exe under its sibling node_modules/@anthropic-ai
    wrapper = shutil.which("claude")
    if wrapper:
        wrapper_dir = Path(wrapper).resolve().parent
        candidate = wrapper_dir / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if candidate.exists():
            return [str(candidate)]
        return [wrapper]
    return ["claude"]


_SYSTEM_PROMPT = (
    "You extract structured fields from a SINGLE job listing. "
    "Output: a SINGLE JSON object on ONE line, no markdown fences, no commentary, no rationale. "
    "Fields: responsibilities (3-5 short strings), technical_skills (up to 5 strings), "
    "seniority_signal (one of: entry, mid, senior, unclear), "
    "remote_arrangement (one of: remote, hybrid, onsite, unspecified). "
    "Rules: entry means 0-2 years AND no senior, lead, principal, or staff language. "
    "Senior means 5 plus years OR explicit lead, principal, staff, or director language. "
    "Use 'unclear' when genuinely unknown. "
    "Reply with JSON only, nothing else. "
    "Do not include shell or pipe characters in your reply."
)
# Note: deliberately avoiding shell metacharacters (especially the pipe symbol)
# in this prompt because on Windows, Python's subprocess.run invokes the
# `claude.CMD` wrapper through cmd.exe, which interprets `|` in arguments as
# a shell pipe and breaks the call.

MAX_DESCRIPTION_CHARS = 8000  # truncate long listings — signal saturates well before

# Batched prompt — one call processes N listings at once. Each result includes
# level_clues: the literal phrases that justified the seniority call, which we
# aggregate downstream to mine new regex patterns.
_BATCH_SYSTEM_PROMPT = (
    "You receive a JSON array of job listings. Each listing has an id, title, and description. "
    "For EACH listing, return one JSON object inside a JSON array. "
    "The output must be ONE JSON array (no markdown fences, no commentary, no rationale, no preamble). "
    "Each object has these fields: "
    "{"
    '"id": copy from input, '
    '"seniority_signal": one of "entry","mid","senior","leadership","unclear", '
    '"remote_arrangement": one of "remote","hybrid","onsite","unspecified", '
    '"level_clues": array of 1 to 3 short verbatim phrases from the description that justified the seniority '
    '(empty array if seniority is "unclear"), '
    '"technical_skills": array of up to 8 short tool/skill names (e.g. "Splunk","Active Directory","PowerShell"), '
    '"responsibilities": array of 2 to 3 short phrases, '
    '"certifications": array of standard certification short-names the listing mentions or requires. '
    'Use canonical short-names like "Security+","CISSP","CCNA","CEH","CISA","CISM","OSCP","CySA+","ITIL",'
    '"AZ-104","AZ-500","SC-200","AWS Certified Cloud Practitioner","AWS Certified Solutions Architect",'
    '"Network+","A+","Linux+","CompTIA Cloud+","MS-900","Splunk Core Certified User". '
    "Include both required AND preferred certs. Empty array if none mentioned. "
    '"years_experience_min": integer or null. The MINIMUM years of experience required. '
    'For "3+ years" return 3. For "2 to 5 years" return 2. For "minimum of 4" return 4. '
    "Null if no number stated. "
    '"years_experience_max": integer or null. The UPPER number when a range is given. '
    'For "2 to 5 years" return 5. For "3+ years" return null (open-ended). '
    '"degree": one of "high_school","associate","bachelor","master","phd","none","unspecified". '
    'Pick the LOWEST acceptable level the listing mentions. Use "none" only if listing explicitly says '
    'no degree required. Use "unspecified" when degree is not discussed. '
    '"clearance": one of "secret","top_secret","ts_sci","public_trust","none". '
    'Use "none" if the listing does not mention clearance'
    "}. "
    "Seniority rules: "
    '"entry" = 0 to 2 years AND no senior/lead/principal/staff/manager language; '
    '"senior" = 5 plus years OR explicit lead/principal/staff language; '
    '"leadership" = manager/director/VP/head-of language; '
    '"mid" = 2 to 4 years OR explicit Tier 2 language; '
    '"unclear" when genuinely unknown. '
    "Reply with JSON only, nothing else."
)

# Per-listing cap in batched mode. Was 2500 originally (tight, to keep the
# JSON array prompt small), but real job descriptions routinely run 5-8 kB
# and the qualifications section (certs, YoE, degree) usually lives near the
# BOTTOM of the body — well past the 2.5 kB mark. Truncating at 2500 meant
# we were sending marketing copy / "About us" / responsibilities to the LLM
# while hiding the actual requirements. Bumped to 7000 to capture qual blocks.
# With batch_size=20 that's ~140 kB total per CLI call, comfortably within
# Haiku's 200 kB input window.
MAX_BATCH_DESCRIPTION_CHARS = 7000


class ClaudeCliExtractor:
    """LLM enrichment via the `claude` CLI subprocess.

    Implements the same .enrich() interface as ClaudeExtractor so the pipeline
    can swap backends without code changes. Also exposes .enrich_many() for
    concurrent batch enrichment.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        model: str = "haiku",
        concurrency: int = 4,
        timeout_seconds: float = 90.0,
        claude_binary: str | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.model = model
        self.concurrency = max(1, int(concurrency))
        self.timeout_seconds = timeout_seconds
        # Resolve the quietest possible invocation path (real .exe over .CMD shim).
        self._claude_argv = _resolve_claude_invocation(claude_binary)
        self._claude_path = self._claude_argv[0]  # back-compat for tests
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Single-listing enrichment (matches ClaudeExtractor.enrich signature)
    # ------------------------------------------------------------------

    def enrich(self, listing: Listing, base: ExtractedRequirements) -> tuple[ExtractedRequirements, list[str]]:
        """Apply Claude-extracted fields on top of the regex-extracted base.

        Returns (enriched, warnings). On any failure, returns base unchanged
        with a warning. Cache hit: skips the subprocess call entirely.
        """
        warnings: list[str] = []
        cache_path = self.cache_dir / f"{listing.listing_id}.json"

        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return _merge(base, cached), warnings
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"cli_llm cache read failed for {listing.listing_id}: {exc}")

        try:
            payload = self._call_claude(description=listing.description)
        except Exception as exc:
            warnings.append(f"cli_llm call failed for {listing.listing_id}: {exc}")
            return base, warnings

        try:
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            warnings.append(f"cli_llm cache write failed for {listing.listing_id}: {exc}")

        return _merge(base, payload), warnings

    # ------------------------------------------------------------------
    # Concurrent batch enrichment — preferred when there are many cache misses
    # ------------------------------------------------------------------

    def enrich_many(
        self,
        listings: list[Listing],
        bases: list[ExtractedRequirements],
        *,
        progress_cb=None,
    ) -> tuple[list[ExtractedRequirements], list[str]]:
        """Enrich a batch concurrently. Returns (enriched_list, warnings).

        progress_cb, if provided, is called as progress_cb(completed, total)
        after each listing finishes (in arbitrary order).
        """
        if len(listings) != len(bases):
            raise ValueError("listings and bases must have matching length")

        results: list[ExtractedRequirements | None] = [None] * len(listings)
        warnings: list[str] = []
        total = len(listings)
        completed = 0

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            future_to_idx = {
                pool.submit(self.enrich, li, bs): i for i, (li, bs) in enumerate(zip(listings, bases, strict=False))
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    enriched, warns = future.result()
                except Exception as exc:
                    enriched, warns = bases[idx], [f"enrich_many task crashed: {exc}"]
                results[idx] = enriched
                warnings.extend(warns)
                completed += 1
                if progress_cb is not None:
                    with contextlib.suppress(Exception):
                        progress_cb(completed, total)

        return [r if r is not None else bases[i] for i, r in enumerate(results)], warnings

    # ------------------------------------------------------------------
    # BATCHED enrichment + clue mining — preferred path for the unclear
    # set since it amortizes the per-call ~5s startup cost across many
    # listings (one CLI call processes 20-30 listings at once).
    # ------------------------------------------------------------------

    def enrich_unclear_batch(
        self,
        listings: list[Listing],
        bases: list[ExtractedRequirements],
        *,
        batch_size: int = 25,
        concurrency: int = 2,
        progress_cb=None,
    ) -> tuple[list[ExtractedRequirements], dict[str, list[str]], list[str]]:
        """Enrich many listings via batched JSON-array prompts.

        Returns (enriched, clues_by_seniority, warnings) where clues_by_seniority
        maps each seniority bucket to a flat list of verbatim phrases the LLM
        cited as evidence. Aggregating these across listings powers the
        pattern-mining step that surfaces new regex candidates.
        """
        if len(listings) != len(bases):
            raise ValueError("listings and bases must have matching length")
        if not listings:
            return [], {}, []

        # Chunk inputs into batches of batch_size, preserving index mapping.
        chunks: list[tuple[list[int], list[Listing], list[ExtractedRequirements]]] = []
        for start in range(0, len(listings), batch_size):
            end = min(start + batch_size, len(listings))
            chunks.append((list(range(start, end)), listings[start:end], bases[start:end]))

        enriched_out: list[ExtractedRequirements | None] = [None] * len(listings)
        clues_by_sen: dict[str, list[str]] = {
            "entry": [],
            "mid": [],
            "senior": [],
            "leadership": [],
        }
        warnings: list[str] = []
        completed_chunks = 0
        total_chunks = len(chunks)

        max_workers = max(1, min(int(concurrency), total_chunks))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._call_claude_batch, [_listing_to_batch_item(li) for li in chunk_lis]): (
                    chunk_idx_list,
                    chunk_lis,
                    chunk_bases,
                )
                for (chunk_idx_list, chunk_lis, chunk_bases) in chunks
            }
            for future in as_completed(futures):
                chunk_idx_list, chunk_lis, chunk_bases = futures[future]
                try:
                    results_by_id = future.result()
                except Exception as exc:
                    warnings.append(f"batch call crashed for {len(chunk_lis)} listings: {exc}")
                    for orig_idx, base in zip(chunk_idx_list, chunk_bases, strict=False):
                        enriched_out[orig_idx] = base
                    completed_chunks += 1
                    continue

                # Apply each parsed result to its corresponding listing.
                for orig_idx, listing, base in zip(chunk_idx_list, chunk_lis, chunk_bases, strict=False):
                    result = results_by_id.get(listing.listing_id)
                    if result is None:
                        warnings.append(f"batch missing result for {listing.listing_id}")
                        enriched_out[orig_idx] = base
                        continue
                    merged = _merge(base, result)
                    enriched_out[orig_idx] = merged
                    # Persist to cache so later single-listing enrich() calls hit it.
                    cache_path = self.cache_dir / f"{listing.listing_id}.json"
                    with contextlib.suppress(OSError):
                        cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
                    # Collect clues per seniority bucket for pattern mining.
                    sen = merged.seniority_signal
                    clues = result.get("level_clues") or []
                    if sen in clues_by_sen and isinstance(clues, list):
                        for clue in clues:
                            s = str(clue).strip()
                            if s:
                                clues_by_sen[sen].append(s)

                completed_chunks += 1
                if progress_cb is not None:
                    with contextlib.suppress(Exception):
                        progress_cb(completed_chunks, total_chunks)

        # Fill any None slots defensively.
        enriched_final = [e if e is not None else bases[i] for i, e in enumerate(enriched_out)]
        return enriched_final, clues_by_sen, warnings

    def enrich_full_batch(
        self,
        listings: list[Listing],
        bases: list[ExtractedRequirements],
        *,
        batch_size: int = 20,
        concurrency: int = 4,
        progress_cb=None,
    ) -> tuple[list[ExtractedRequirements], list[str]]:
        """Run the FULL extraction prompt on every listing — used for the
        post-filter enrichment pass that fills in certs / YoE / degree / etc.
        the regex missed.

        Same underlying batched code path as enrich_unclear_batch (single
        prompt covers seniority AND requirement fields). This wrapper just
        drops the clue-mining return value since callers of the full pass
        don't need it.
        """
        enriched, _clues, warnings = self.enrich_unclear_batch(
            listings,
            bases,
            batch_size=batch_size,
            concurrency=concurrency,
            progress_cb=progress_cb,
        )
        return enriched, warnings

    def _call_claude_batch(self, items: list[dict]) -> dict[str, dict]:
        """Send a batch of listing items to claude -p and return a dict keyed by id."""
        if not items:
            return {}
        payload = json.dumps(items, ensure_ascii=False)
        cmd = [
            *self._claude_argv,
            "-p",
            "--system-prompt",
            _BATCH_SYSTEM_PROMPT,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--model",
            self.model,
        ]
        try:
            completed = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds * max(2.0, len(items) / 6),
                shell=False,
                encoding="utf-8",
                errors="replace",
                **_quiet_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI batch timed out after {self.timeout_seconds}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError("claude CLI not found.") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip() or (completed.stdout or "").strip()
            raise RuntimeError(f"claude CLI exit {completed.returncode}: {stderr[:200]}")

        outer = json.loads(completed.stdout)
        if outer.get("is_error"):
            raise RuntimeError(f"claude CLI returned error: {outer.get('result', '<no result>')[:200]}")

        results_array = _parse_inner_json_array(outer.get("result", ""))
        results_by_id: dict[str, dict] = {}
        for entry in results_array:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                results_by_id[entry_id] = entry
        return results_by_id

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_claude(self, *, description: str) -> dict:
        body = (description or "").strip()[:MAX_DESCRIPTION_CHARS]
        if not body:
            raise ValueError("empty description")

        cmd = [
            *self._claude_argv,
            "-p",
            "--system-prompt",
            _SYSTEM_PROMPT,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--model",
            self.model,
        ]

        try:
            completed = subprocess.run(
                cmd,
                input=body,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
                encoding="utf-8",
                errors="replace",
                **_quiet_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI timed out after {self.timeout_seconds}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "claude CLI not found. Install Claude Code (npm install -g @anthropic-ai/claude-code) "
                "or pass --llm-backend api to use the Anthropic SDK instead."
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip() or (completed.stdout or "").strip()
            raise RuntimeError(f"claude CLI exit {completed.returncode}: {stderr[:200]}")

        outer = json.loads(completed.stdout)
        if outer.get("is_error"):
            raise RuntimeError(f"claude CLI returned error: {outer.get('result', '<no result>')[:200]}")

        return _parse_inner_json(outer.get("result", ""))


def _listing_to_batch_item(listing: Listing) -> dict:
    """Compact form sent to the batched LLM prompt — id + title + truncated description."""
    desc = (listing.description or "").strip()[:MAX_BATCH_DESCRIPTION_CHARS]
    return {"id": listing.listing_id, "title": listing.title, "description": desc}


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.lstrip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = re.sub(r"```\s*$", "", stripped).strip("` \n")
    return stripped


def _parse_inner_json_array(text: str) -> list[dict]:
    """Parse the model's response as a JSON array, tolerating code fences + prefix/suffix prose."""
    stripped = _strip_code_fences((text or "").strip())
    if not stripped:
        raise ValueError("empty result from claude CLI (batch)")
    first = stripped.find("[")
    last = stripped.rfind("]")
    if first == -1 or last == -1 or last <= first:
        raise ValueError(f"no JSON array in result: {stripped[:200]!r}")
    parsed = json.loads(stripped[first : last + 1])
    if not isinstance(parsed, list):
        raise ValueError(f"expected JSON array, got {type(parsed).__name__}")
    return parsed


def _parse_inner_json(text: str) -> dict:
    """The `result` field is the model's raw text — may be JSON, or JSON wrapped in code fences."""
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("empty result from claude CLI")

    # Strip ```json ... ``` fences if present.
    if stripped.startswith("```"):
        stripped = stripped.lstrip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        # Trim trailing fence.
        stripped = re.sub(r"```\s*$", "", stripped).strip("` \n")

    # Find outermost { ... } in case the model wrote a "Rationale:" suffix.
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ValueError(f"no JSON object in result: {stripped[:200]!r}")
    return json.loads(stripped[first : last + 1])


_VALID_DEGREE_VALUES = {
    "high_school",
    "associate",
    "bachelor",
    "master",
    "phd",
    "none",
    "unspecified",
}
_VALID_CLEARANCE_VALUES = {"secret", "top_secret", "ts_sci", "public_trust", "none"}


def _merge(base: ExtractedRequirements, payload: dict) -> ExtractedRequirements:
    """Layer the LLM payload onto regex-extracted base.

    Merge rules per field:
      - certifications: UNION (LLM-found certs supplement regex catches, never overwrite)
      - technical_skills: UNION (same)
      - responsibilities: REPLACE (regex doesn't produce these; LLM's view is authoritative)
      - seniority_signal / remote_arrangement: REPLACE if LLM returned a valid value
      - years_experience_min / _max: FILL ONLY IF base is None (regex is trusted first; LLM fills gaps)
      - degree: FILL ONLY IF base is None and LLM returned a concrete value (not "unspecified")
      - clearance: FILL ONLY IF base is None and LLM returned a concrete value (not "none")
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

    # Certifications: additive union. The regex already produced canonical
    # short-names, and the LLM is prompted to use the same vocabulary, so
    # naive de-duplication on equality works without fancy normalization.
    certs = payload.get("certifications", [])
    if isinstance(certs, list):
        merged_certs = list(out.certifications)
        for c in certs:
            c_str = str(c).strip()
            if c_str and c_str not in merged_certs:
                merged_certs.append(c_str)
        out.certifications = merged_certs[:15]

    seniority = payload.get("seniority_signal")
    if seniority in {"entry", "mid", "senior", "leadership", "unclear"}:
        out.seniority_signal = seniority  # type: ignore[assignment]

    arrangement = payload.get("remote_arrangement")
    if arrangement in {"remote", "hybrid", "onsite", "unspecified"}:
        out.remote_arrangement = arrangement  # type: ignore[assignment]

    # YoE: fill only when regex left it blank. Regex pulls explicit "N years
    # of experience" patterns with a context window; if it found one, we trust
    # it. If it didn't, the LLM saw the whole listing and can fill the gap.
    yoe_min_raw = payload.get("years_experience_min")
    if out.years_experience_min is None and isinstance(yoe_min_raw, int) and 0 <= yoe_min_raw <= 30:
        out.years_experience_min = yoe_min_raw
    yoe_max_raw = payload.get("years_experience_max")
    if out.years_experience_max is None and isinstance(yoe_max_raw, int) and 0 <= yoe_max_raw <= 40:
        out.years_experience_max = yoe_max_raw

    # Degree: fill only when regex left it blank. "unspecified" from the LLM
    # = no information, so it doesn't fill anything either.
    degree_raw = payload.get("degree")
    if (
        out.degree is None
        and isinstance(degree_raw, str)
        and degree_raw in _VALID_DEGREE_VALUES
        and degree_raw
        not in (
            "unspecified",
            "none",
        )
    ):
        out.degree = degree_raw

    # Clearance: same fill-only-if-blank pattern. The LLM is asked to return
    # "none" for non-mentions, so we filter that out before assigning.
    clearance_raw = payload.get("clearance")
    if (
        out.clearance is None
        and isinstance(clearance_raw, str)
        and clearance_raw in _VALID_CLEARANCE_VALUES
        and clearance_raw != "none"
    ):
        out.clearance = clearance_raw

    return out


def is_cli_available() -> bool:
    """Quick check used by the CLI to decide whether to default to cli backend."""
    return shutil.which("claude") is not None


# Re-exported sys to silence "unused import" linters when this module is imported.
_ = sys
