"""Pure-Python helpers for the dashboard.

Lives separate from `dashboard.py` so the testable logic doesn't depend on
Streamlit. Everything here is import-safe in any Python process and
fully unit-testable with `unittest` + the existing WorkspaceTempDir pattern.

Public surface:
    list_snapshots(reports_dir)
    load_snapshot(path)
    load_trend_csv(path)
    detect_available_credentials()
    build_scrape_command(opts)
    scrub_secrets(text)
    ScrapeRunner
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

ScrapeStatus = Literal["running", "succeeded", "failed", "unknown"]

# Auto-load .env if python-dotenv is installed, so detect_available_credentials()
# sees the same env the CLI does. Silent if missing.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Snapshot + trend file helpers
# ---------------------------------------------------------------------------

_SNAPSHOT_GLOB = "snapshot-*.json"
# Strict canonical-filename regex. We deliberately EXCLUDE variants like
# `snapshot-YYYY-MM-DD.unfiltered.json` — the dashboard should only surface
# the post-filter snapshot since that's what the rest of the UI describes.
# The unfiltered files are debugging artifacts kept on disk for re-running
# --reclassify, not user-facing views.
_SNAPSHOT_NAME_RE = re.compile(r"^snapshot-\d{4}-\d{2}-\d{2}\.json$")


def list_snapshots(reports_dir: Path) -> list[Path]:
    """Return canonical snapshot-YYYY-MM-DD.json paths, newest first.

    Skips intermediate / debugging variants like
    `snapshot-2026-05-25.unfiltered.json` so the dashboard's snapshot
    selector only ever lists filtered, user-facing snapshots.

    Returns an empty list if reports_dir does not exist or contains no
    matching files.
    """
    if not reports_dir.exists() or not reports_dir.is_dir():
        return []
    snapshots = [p for p in reports_dir.glob(_SNAPSHOT_GLOB) if p.is_file() and _SNAPSHOT_NAME_RE.match(p.name)]
    # Reverse lexicographic = reverse chronological for YYYY-MM-DD naming.
    return sorted(snapshots, reverse=True)


def load_snapshot(path: Path) -> dict | None:
    """Load a snapshot file. Returns None on malformed JSON or missing file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_trend_csv(path: Path) -> list[dict]:
    """Load trend.csv as list of dicts. Returns [] if file missing or empty."""
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Credential availability detection
# ---------------------------------------------------------------------------


@dataclass
class SourceAvailability:
    """Each bool answers 'can this run on this machine right now?'

    The dashboard's default-checked state is a SEPARATE decision (see UI code):
      default-on  = available AND not ToS-grey
      default-off = unavailable OR ToS-grey (JobSpy)
    """

    greenhouse: bool = True  # always available (no creds needed)
    lever: bool = True  # always available (no creds needed)
    usajobs: bool = False  # True iff JOBMARKET_USAJOBS_SECRET_REF set + non-empty
    jobspy: bool = True  # always available; UI defaults OFF due to ToS posture
    llm: bool = False  # True iff JOBMARKET_ANTHROPIC_SECRET_REF set + non-empty


def detect_available_credentials() -> SourceAvailability:
    """Inspect env vars to decide which sources can run."""
    usajobs_ref = (os.environ.get("JOBMARKET_USAJOBS_SECRET_REF") or "").strip()
    anthropic_ref = (os.environ.get("JOBMARKET_ANTHROPIC_SECRET_REF") or "").strip()
    return SourceAvailability(
        usajobs=bool(usajobs_ref),
        llm=bool(anthropic_ref),
    )


# ---------------------------------------------------------------------------
# Scrape command construction
# ---------------------------------------------------------------------------


@dataclass
class ScrapeOptions:
    sites: list[str] = field(default_factory=list)
    role_buckets: list[str] = field(default_factory=lambda: ["junior_soc", "help_desk_it_admin"])
    use_llm: bool = False
    # 0 = unlimited; non-zero = soft cap per source per query.
    results_per_source: int = 0
    # Drop listings posted more than N days ago. 1-60.
    freshness_days: int = 14
    # Allowed seniority buckets — default keeps entry + unclear, drops everything else.
    allowed_seniority: list[str] = field(default_factory=lambda: ["entry", "unclear"])
    # If True, keep listings the title classifier couldn't bucket (default off — noise).
    include_unclassified: bool = False
    no_remote_pass: bool = False


# Whitelist of CLI flag values we ever pass — defense in depth against
# someone wiring a free-text widget directly into the command builder.
_VALID_SITES = {"greenhouse", "lever", "usajobs", "jobspy"}
_VALID_BUCKETS = {"junior_soc", "help_desk_it_admin"}
_VALID_SENIORITY = {"entry", "mid", "senior", "leadership", "unclear"}


def build_scrape_command(
    opts: ScrapeOptions,
    *,
    availability: SourceAvailability | None = None,
    executable: list[str] | None = None,
) -> list[str]:
    """Translate a ScrapeOptions into a scraper argv list.

    By default invokes the package as `<this-python> -m job_market_intel`,
    which is bulletproof against PATH issues (the dashboard subprocess
    inherits the dashboard's Python interpreter, which is always the venv
    one). Pass `executable=["job-market-intel"]` to use the console script.

    If `availability` is provided, sources the user toggled but that aren't
    available are silently omitted (defense in depth against checkbox bugs).

    Always returns a list[str] suitable for subprocess.Popen(args=..., shell=False).
    """
    if executable is None:
        executable = [sys.executable, "-m", "job_market_intel"]

    sites = [s for s in opts.sites if s in _VALID_SITES]
    if availability is not None:
        sites = [s for s in sites if getattr(availability, s, False)]
    buckets = [b for b in opts.role_buckets if b in _VALID_BUCKETS]
    # Clamp into a sane range; 0 means unlimited so accept it directly.
    raw_cap = int(opts.results_per_source)
    results_per_source = 0 if raw_cap <= 0 else min(raw_cap, 100_000)
    freshness_days = max(1, min(int(opts.freshness_days), 60))

    cmd: list[str] = list(executable)
    if sites:
        cmd.extend(["--sites", *sites])
    if buckets:
        cmd.extend(["--role-buckets", *buckets])
    cmd.extend(["--results-per-source", str(results_per_source)])
    cmd.extend(["--freshness-days", str(freshness_days)])

    seniority = [s for s in opts.allowed_seniority if s in _VALID_SENIORITY]
    if seniority:
        cmd.extend(["--seniority", *seniority])
    if opts.include_unclassified:
        cmd.append("--include-unclassified")

    if not opts.use_llm:
        cmd.append("--no-llm")
    if opts.no_remote_pass:
        cmd.append("--no-remote-pass")
    return cmd


# ---------------------------------------------------------------------------
# Secret scrubbing (defensive, belt-and-suspenders)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anthropic-style API keys
    (re.compile(r"sk-ant-[\w\-]{20,}", re.IGNORECASE), "sk-ant-***REDACTED***"),
    # 1Password secret references — strip the path, leave 'op://***'
    (re.compile(r"op://[^\s\"'`]+", re.IGNORECASE), "op://***REDACTED***"),
    # AWS access keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***REDACTED***"),
    # Generic api_key/apikey/token = value forms
    (
        re.compile(r"(?i)(api[_-]?key|apikey|token|password|secret)\s*[=:]\s*['\"]?([\w\-\.]{8,})['\"]?"),
        r"\1=***REDACTED***",
    ),
    # GitHub personal access tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "gh*_***REDACTED***"),
    # Google API keys
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "AIza***REDACTED***"),
)


def scrub_secrets(text: str) -> str:
    """Run all secret-redaction patterns over `text` before display."""
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# ScrapeRunner — subprocess lifecycle
# ---------------------------------------------------------------------------


def _now_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID is currently a live process.

    Uses psutil if available (most reliable on Windows); falls back to os.kill(0).
    """
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    # POSIX fallback (Windows os.kill semantics differ but this is best-effort
    # when psutil isn't installed).
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class ScrapeRunner:
    """Owns the subprocess lifecycle. All state lives on the filesystem.

    Files (under run_dir, default cache/dashboard/):
        active-run.json     - {"run_id": str, "pid": int, "started_at": iso, "cmd": list}
                              Present iff a scrape is in-flight (or stale).
        run-{run_id}.log    - stdout+stderr capture (tee'd)
        run-{run_id}.exit   - exit code, written by sentinel after process completes
    """

    def __init__(self, run_dir: Path, *, cwd: Path | None = None) -> None:
        self.run_dir = run_dir
        self.cwd = cwd or Path.cwd()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _active_path(self) -> Path:
        return self.run_dir / "active-run.json"

    def _log_path(self, run_id: str) -> Path:
        return self.run_dir / f"run-{run_id}.log"

    def _exit_path(self, run_id: str) -> Path:
        return self.run_dir / f"run-{run_id}.exit"

    def active_run(self) -> str | None:
        """Return the run_id of an active scrape, or None.

        Returns the run_id whether or not the process is still alive — callers
        should use status() to distinguish 'running' from 'unknown' (orphaned).
        """
        if not self._active_path.exists():
            return None
        try:
            return json.loads(self._active_path.read_text(encoding="utf-8")).get("run_id")
        except (OSError, json.JSONDecodeError):
            return None

    def start(self, cmd: list[str]) -> str:
        """Start a scrape subprocess. Returns the run_id.

        Idempotent: if a live active run already exists, returns its run_id
        without spawning a new process. If the lock is stale (PID dead and no
        .exit file), the lock is cleared and a fresh run starts.
        """
        if not isinstance(cmd, list) or not all(isinstance(arg, str) for arg in cmd):
            raise TypeError("cmd must be a list[str] (Popen with shell=False)")

        existing_run_id = self.active_run()
        if existing_run_id is not None:
            if self.status(existing_run_id) == "running":
                return existing_run_id
            self.clear_active()

        run_id = _now_run_id()
        log_path = self._log_path(run_id)
        exit_path = self._exit_path(run_id)

        # Open the log file and spawn the child with stdout+stderr tee'd into it.
        # We close the parent's handle immediately; the child keeps writing.
        log_fp = log_path.open("w", encoding="utf-8", buffering=1)  # line-buffered
        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                cwd=str(self.cwd),
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except Exception:
            log_fp.close()
            raise
        finally:
            # Detach the parent's handle; the child has its own fd now.
            with contextlib.suppress(OSError):
                log_fp.close()

        # Write the active-run lock atomically.
        active_payload = {
            "run_id": run_id,
            "pid": process.pid,
            "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "cmd": cmd,
        }
        tmp = self._active_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(active_payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._active_path)

        # Spawn a tiny background sentinel that waits for the child and writes
        # the .exit file. This way callers see "succeeded" / "failed" status
        # even if the dashboard process is restarted mid-run.
        self._spawn_exit_sentinel(pid=process.pid, exit_path=exit_path)

        return run_id

    def _spawn_exit_sentinel(self, *, pid: int, exit_path: Path) -> None:
        """Spawn a detached Python process that waits for the scrape PID and writes the .exit file."""
        sentinel_code = (
            "import os, sys, time;"
            f"pid={pid};"
            f"exit_path=r'{exit_path}';"
            "code='unknown'\n"
            "try:\n"
            "    import psutil\n"
            "    p = psutil.Process(pid)\n"
            "    code = p.wait()\n"
            "except Exception:\n"
            "    # psutil missing or process gone: poll with os.kill(0)\n"
            "    while True:\n"
            "        try:\n"
            "            os.kill(pid, 0)\n"
            "        except OSError:\n"
            "            break\n"
            "        time.sleep(0.5)\n"
            "    code = -1  # we can't know the real exit code without psutil\n"
            "open(exit_path, 'w', encoding='utf-8').write(str(code))\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", sentinel_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.cwd),
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

    def status(self, run_id: str) -> ScrapeStatus:
        """Return the current status of a run."""
        exit_path = self._exit_path(run_id)
        if exit_path.exists():
            try:
                code_str = exit_path.read_text(encoding="utf-8").strip()
                code = int(code_str)
            except (OSError, ValueError):
                return "unknown"
            return "succeeded" if code == 0 else "failed"

        # No .exit yet — check if the process is still alive.
        active_payload = self._read_active_payload()
        if active_payload is None or active_payload.get("run_id") != run_id:
            return "unknown"
        pid = active_payload.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            return "running"
        return "unknown"

    def tail_log(self, run_id: str, max_lines: int = 200) -> list[str]:
        """Return the last `max_lines` of the run's log file, secret-scrubbed."""
        log_path = self._log_path(run_id)
        if not log_path.exists():
            return []
        try:
            # Bounded read — only the tail. For very large logs, this prevents
            # loading the whole file into memory.
            with log_path.open(encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        tail = lines[-max_lines:] if max_lines > 0 else []
        return [scrub_secrets(line.rstrip("\n")) for line in tail]

    def clear_active(self) -> None:
        """Remove the active-run lock file. Safe if it doesn't exist."""
        with contextlib.suppress(FileNotFoundError):
            self._active_path.unlink()

    def _read_active_payload(self) -> dict | None:
        if not self._active_path.exists():
            return None
        try:
            return json.loads(self._active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
