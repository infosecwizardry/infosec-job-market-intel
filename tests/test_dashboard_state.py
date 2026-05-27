"""Tests for dashboard_state.py — pure helpers + ScrapeRunner lifecycle.

Layer 1: pure-function helpers, no real subprocess.
Layer 2: ScrapeRunner exercised with a fast fake command (sys.executable -c ...).
No Streamlit imports; this file runs in any Python env with python-dotenv.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from job_market_intel.dashboard_state import (
    ScrapeOptions,
    ScrapeRunner,
    SourceAvailability,
    build_scrape_command,
    detect_available_credentials,
    list_snapshots,
    load_snapshot,
    load_trend_csv,
    scrub_secrets,
)

WORKSPACE_TEMP_ROOT = Path.cwd() / ".tmp-test-work"
WORKSPACE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


class WorkspaceTempDir:
    def __enter__(self) -> str:
        self.path = WORKSPACE_TEMP_ROOT / f"case-{uuid4().hex}"
        self.path.mkdir(parents=True, exist_ok=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Layer 1: list_snapshots / load_snapshot / load_trend_csv
# ---------------------------------------------------------------------------


class SnapshotHelperTests(TestCase):
    def test_list_snapshots_returns_sorted_descending(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            reports = Path(tmpdir)
            for name in ("snapshot-2026-05-20.json", "snapshot-2026-05-25.json", "snapshot-2026-05-22.json"):
                (reports / name).write_text("{}", encoding="utf-8")
            result = list_snapshots(reports)
        self.assertEqual(
            [p.name for p in result],
            [
                "snapshot-2026-05-25.json",
                "snapshot-2026-05-22.json",
                "snapshot-2026-05-20.json",
            ],
        )

    def test_list_snapshots_empty_dir_returns_empty_list(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            self.assertEqual(list_snapshots(Path(tmpdir)), [])

    def test_list_snapshots_missing_dir_returns_empty_list(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            ghost = Path(tmpdir) / "does_not_exist"
            self.assertEqual(list_snapshots(ghost), [])

    def test_list_snapshots_ignores_non_snapshot_files(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            reports = Path(tmpdir)
            (reports / "snapshot-2026-05-25.json").write_text("{}", encoding="utf-8")
            (reports / "trend.csv").write_text("a,b\n", encoding="utf-8")
            (reports / "report-2026-05-25.md").write_text("# x", encoding="utf-8")
            (reports / "random.json").write_text("{}", encoding="utf-8")
            result = list_snapshots(reports)
        self.assertEqual([p.name for p in result], ["snapshot-2026-05-25.json"])

    def test_list_snapshots_excludes_unfiltered_variants(self) -> None:
        # The dashboard should only ever surface the canonical filtered
        # snapshot — debug artifacts like `.unfiltered.json` must not appear
        # in the snapshot selector (otherwise they sort to the top of the
        # list and the user lands on an un-filtered view by default).
        with WorkspaceTempDir() as tmpdir:
            reports = Path(tmpdir)
            (reports / "snapshot-2026-05-25.json").write_text("{}", encoding="utf-8")
            (reports / "snapshot-2026-05-25.unfiltered.json").write_text("{}", encoding="utf-8")
            (reports / "snapshot-2026-05-25.raw.json").write_text("{}", encoding="utf-8")
            result = list_snapshots(reports)
        self.assertEqual([p.name for p in result], ["snapshot-2026-05-25.json"])

    def test_load_snapshot_returns_dict_on_valid_json(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            path = Path(tmpdir) / "snapshot-2026-05-25.json"
            path.write_text('{"schema_version": "1.0"}', encoding="utf-8")
            result = load_snapshot(path)
        self.assertEqual(result, {"schema_version": "1.0"})

    def test_load_snapshot_returns_none_on_malformed_json(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            path = Path(tmpdir) / "snapshot-2026-05-25.json"
            path.write_text("{ not valid json", encoding="utf-8")
            self.assertIsNone(load_snapshot(path))

    def test_load_snapshot_returns_none_on_missing_file(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            self.assertIsNone(load_snapshot(Path(tmpdir) / "ghost.json"))

    def test_load_trend_csv_returns_empty_when_missing(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            self.assertEqual(load_trend_csv(Path(tmpdir) / "trend.csv"), [])

    def test_load_trend_csv_returns_list_of_dicts(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            path = Path(tmpdir) / "trend.csv"
            path.write_text(
                "date,role_bucket,requirement_type,requirement,count,total\n"
                "2026-05-25,junior_soc,cert,Security+,5,12\n"
                "2026-05-25,junior_soc,cert,Network+,3,12\n",
                encoding="utf-8",
            )
            result = load_trend_csv(path)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["requirement"], "Security+")
        self.assertEqual(result[1]["count"], "3")


# ---------------------------------------------------------------------------
# Layer 1: detect_available_credentials
# ---------------------------------------------------------------------------


class CredentialDetectionTests(TestCase):
    def test_clean_env_returns_usajobs_and_llm_unavailable(self) -> None:
        with patch.dict(
            os.environ, {"JOBMARKET_USAJOBS_SECRET_REF": "", "JOBMARKET_ANTHROPIC_SECRET_REF": ""}, clear=False
        ):
            os.environ.pop("JOBMARKET_USAJOBS_SECRET_REF", None)
            os.environ.pop("JOBMARKET_ANTHROPIC_SECRET_REF", None)
            av = detect_available_credentials()
        self.assertFalse(av.usajobs)
        self.assertFalse(av.llm)
        self.assertTrue(av.greenhouse)
        self.assertTrue(av.lever)
        self.assertTrue(av.jobspy)

    def test_usajobs_ref_set_returns_usajobs_available(self) -> None:
        with patch.dict(os.environ, {"JOBMARKET_USAJOBS_SECRET_REF": "op://vault/usajobs/x"}, clear=False):
            av = detect_available_credentials()
        self.assertTrue(av.usajobs)

    def test_anthropic_ref_set_returns_llm_available(self) -> None:
        with patch.dict(os.environ, {"JOBMARKET_ANTHROPIC_SECRET_REF": "op://vault/anthropic/x"}, clear=False):
            av = detect_available_credentials()
        self.assertTrue(av.llm)

    def test_blank_string_does_not_count_as_available(self) -> None:
        with patch.dict(os.environ, {"JOBMARKET_USAJOBS_SECRET_REF": "   "}, clear=False):
            av = detect_available_credentials()
        self.assertFalse(av.usajobs)


# ---------------------------------------------------------------------------
# Layer 1: build_scrape_command
# ---------------------------------------------------------------------------


class BuildScrapeCommandTests(TestCase):
    def test_returns_list_of_strings_no_shell_injection_surface(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse", "lever"], use_llm=False, results_per_source=20)
        cmd = build_scrape_command(opts)
        self.assertIsInstance(cmd, list)
        for arg in cmd:
            self.assertIsInstance(arg, str)
        # Default invocation is `<python> -m job_market_intel` — bulletproof against PATH.
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], "-m")
        self.assertEqual(cmd[2], "job_market_intel")
        self.assertIn("--sites", cmd)
        self.assertIn("greenhouse", cmd)
        self.assertIn("lever", cmd)
        self.assertIn("--no-llm", cmd)
        self.assertIn("--results-per-source", cmd)
        self.assertIn("20", cmd)

    def test_explicit_executable_override_is_honored(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse"])
        cmd = build_scrape_command(opts, executable=["job-market-intel"])
        self.assertEqual(cmd[0], "job-market-intel")

    def test_default_freshness_days_is_14(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse"])
        cmd = build_scrape_command(opts)
        self.assertIn("--freshness-days", cmd)
        idx = cmd.index("--freshness-days")
        self.assertEqual(cmd[idx + 1], "14")

    def test_freshness_days_clamped_to_1_60(self) -> None:
        cmd_high = build_scrape_command(ScrapeOptions(sites=["greenhouse"], freshness_days=999))
        idx = cmd_high.index("--freshness-days")
        self.assertEqual(cmd_high[idx + 1], "60")

        cmd_low = build_scrape_command(ScrapeOptions(sites=["greenhouse"], freshness_days=0))
        idx = cmd_low.index("--freshness-days")
        self.assertEqual(cmd_low[idx + 1], "1")

    def test_results_per_source_zero_passes_through_as_unlimited(self) -> None:
        # 0 means unlimited; should be emitted as-is (not clamped to 1).
        cmd = build_scrape_command(ScrapeOptions(sites=["greenhouse"], results_per_source=0))
        idx = cmd.index("--results-per-source")
        self.assertEqual(cmd[idx + 1], "0")

    def test_default_seniority_is_entry_and_unclear(self) -> None:
        cmd = build_scrape_command(ScrapeOptions(sites=["greenhouse"]))
        self.assertIn("--seniority", cmd)
        idx = cmd.index("--seniority")
        # The two values follow immediately after the flag (until next flag-looking token).
        following = cmd[idx + 1 : idx + 3]
        self.assertEqual(sorted(following), sorted(["entry", "unclear"]))

    def test_custom_seniority_list_is_emitted(self) -> None:
        cmd = build_scrape_command(ScrapeOptions(sites=["greenhouse"], allowed_seniority=["entry", "mid", "senior"]))
        idx = cmd.index("--seniority")
        following = cmd[idx + 1 : idx + 4]
        self.assertEqual(sorted(following), sorted(["entry", "mid", "senior"]))

    def test_invalid_seniority_values_are_dropped(self) -> None:
        cmd = build_scrape_command(ScrapeOptions(sites=["greenhouse"], allowed_seniority=["entry", "garbage"]))
        idx = cmd.index("--seniority")
        following = cmd[idx + 1 : idx + 3]
        self.assertNotIn("garbage", following)

    def test_include_unclassified_emits_flag_when_true(self) -> None:
        cmd_on = build_scrape_command(ScrapeOptions(sites=["greenhouse"], include_unclassified=True))
        self.assertIn("--include-unclassified", cmd_on)
        cmd_off = build_scrape_command(ScrapeOptions(sites=["greenhouse"], include_unclassified=False))
        self.assertNotIn("--include-unclassified", cmd_off)

    def test_omits_unavailable_sources_even_when_toggled(self) -> None:
        # User toggled usajobs ON, but creds unavailable: command must NOT include it.
        opts = ScrapeOptions(sites=["greenhouse", "usajobs"])
        av = SourceAvailability(usajobs=False, llm=False)
        cmd = build_scrape_command(opts, availability=av)
        self.assertIn("greenhouse", cmd)
        self.assertNotIn("usajobs", cmd)

    def test_unknown_sites_are_silently_dropped(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse", "evil_source"])
        cmd = build_scrape_command(opts)
        self.assertIn("greenhouse", cmd)
        self.assertNotIn("evil_source", cmd)

    def test_use_llm_omits_no_llm_flag(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse"], use_llm=True)
        cmd = build_scrape_command(opts)
        self.assertNotIn("--no-llm", cmd)

    def test_results_per_source_clamped_upper_bound(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse"], results_per_source=999_999_999)
        cmd = build_scrape_command(opts)
        idx = cmd.index("--results-per-source")
        self.assertEqual(cmd[idx + 1], "100000")  # safety cap

    def test_negative_results_per_source_treated_as_unlimited(self) -> None:
        # Both -5 and 0 mean "no cap"; passed through as 0 so the collector
        # uses its internal unlimited default.
        cmd = build_scrape_command(ScrapeOptions(sites=["greenhouse"], results_per_source=-5))
        idx = cmd.index("--results-per-source")
        self.assertEqual(cmd[idx + 1], "0")

    def test_no_remote_pass_flag_emitted(self) -> None:
        opts = ScrapeOptions(sites=["greenhouse"], no_remote_pass=True)
        cmd = build_scrape_command(opts)
        self.assertIn("--no-remote-pass", cmd)


# ---------------------------------------------------------------------------
# Layer 1: scrub_secrets
# ---------------------------------------------------------------------------


class SecretScrubbingTests(TestCase):
    def test_redacts_anthropic_key(self) -> None:
        text = "Using key sk-ant-api03-abc123def456ghi789jkl0 for enrichment"
        scrubbed = scrub_secrets(text)
        self.assertNotIn("sk-ant-api03-abc123def456ghi789jkl0", scrubbed)
        self.assertIn("REDACTED", scrubbed)

    def test_redacts_op_secret_reference(self) -> None:
        text = "Reading op://vault123/item-xyz/notesPlain for secrets"
        scrubbed = scrub_secrets(text)
        self.assertNotIn("vault123", scrubbed)
        self.assertIn("REDACTED", scrubbed)

    def test_redacts_apikey_assignment(self) -> None:
        text = 'config: api_key="super-secret-token-12345"'
        scrubbed = scrub_secrets(text)
        self.assertNotIn("super-secret-token-12345", scrubbed)

    def test_redacts_aws_access_key(self) -> None:
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        scrubbed = scrub_secrets(text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", scrubbed)

    def test_passes_through_safe_text(self) -> None:
        text = "Scraped 12 listings from greenhouse"
        self.assertEqual(scrub_secrets(text), text)

    def test_empty_string_is_safe(self) -> None:
        self.assertEqual(scrub_secrets(""), "")


# ---------------------------------------------------------------------------
# Layer 2: ScrapeRunner lifecycle (uses real Popen, fake command)
# ---------------------------------------------------------------------------


def _quick_cmd(duration: float = 0.3, exit_code: int = 0) -> list[str]:
    """A fast subprocess command that exercises real Popen + log writes."""
    body = (
        f"import sys, time; "
        f"sys.stdout.write('hello from child\\n'); "
        f"sys.stdout.flush(); "
        f"time.sleep({duration}); "
        f"sys.exit({exit_code})"
    )
    return [sys.executable, "-c", body]


class ScrapeRunnerLifecycleTests(TestCase):
    def test_start_writes_active_run_lock(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            run_id = runner.start(_quick_cmd(duration=0.5))
            lock = runner._active_path
            self.assertTrue(lock.exists())
            payload = json.loads(lock.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], run_id)
        self.assertIsInstance(payload["pid"], int)
        self.assertIn("started_at", payload)
        self.assertIn("cmd", payload)

    def test_start_rejects_non_list_command(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            with self.assertRaises(TypeError):
                runner.start("ls -la")  # type: ignore[arg-type]

    def test_start_is_idempotent_when_run_already_active(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            first = runner.start(_quick_cmd(duration=2.0))
            second = runner.start(_quick_cmd(duration=2.0))  # should be no-op
        self.assertEqual(first, second)

    def test_start_replaces_orphaned_lock(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            # Stale lock with a definitely-dead PID (very large unlikely-to-exist)
            stale_payload = {"run_id": "stale-run", "pid": 999999, "started_at": "x", "cmd": []}
            runner._active_path.write_text(json.dumps(stale_payload), encoding="utf-8")

            new_id = runner.start(_quick_cmd(duration=0.3))
        self.assertNotEqual(new_id, "stale-run")

    def test_status_returns_running_then_succeeded(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            run_id = runner.start(_quick_cmd(duration=0.5))
            self.assertEqual(runner.status(run_id), "running")
            # Wait for child + sentinel to complete.
            for _ in range(40):
                time.sleep(0.25)
                if runner.status(run_id) == "succeeded":
                    break
            self.assertEqual(runner.status(run_id), "succeeded")

    def test_status_returns_failed_on_nonzero_exit(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            run_id = runner.start(_quick_cmd(duration=0.3, exit_code=1))
            for _ in range(40):
                time.sleep(0.25)
                if runner.status(run_id) in {"succeeded", "failed"}:
                    break
            self.assertEqual(runner.status(run_id), "failed")

    def test_tail_log_returns_stdout_lines(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            run_id = runner.start(_quick_cmd(duration=0.3))
            for _ in range(40):
                time.sleep(0.25)
                if runner.status(run_id) == "succeeded":
                    break
            lines = runner.tail_log(run_id, max_lines=10)
        self.assertTrue(any("hello from child" in line for line in lines))

    def test_tail_log_bounded_by_max_lines(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            # Write a fake log directly to bypass needing a slow subprocess.
            log_path = runner._log_path("fakerun")
            log_path.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
            lines = runner.tail_log("fakerun", max_lines=10)
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[-1], "line 499")

    def test_clear_active_removes_lock(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            runner._active_path.write_text("{}", encoding="utf-8")
            self.assertTrue(runner._active_path.exists())
            runner.clear_active()
            self.assertFalse(runner._active_path.exists())

    def test_active_run_returns_none_when_no_lock(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            self.assertIsNone(runner.active_run())

    def test_active_run_returns_run_id_when_lock_present(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            runner = ScrapeRunner(Path(tmpdir))
            payload = {"run_id": "my-run-123", "pid": 1, "started_at": "x", "cmd": []}
            runner._active_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(runner.active_run(), "my-run-123")
