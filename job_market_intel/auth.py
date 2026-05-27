"""Secret fetch for USAJobs (email + key) and Anthropic API key, via 1Password CLI.

Mirrors `youtube_trend_report/auth.py` shape: 1Password is the source of truth,
keyring is the cache. CLI args let you point at a different vault or `op` binary
without editing code.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import keyring

# All three defaults are driven by environment variables so the source tree
# stays free of personal paths, vault UUIDs, or item names. See `.env.example`
# for the canonical list and how to populate them locally.
DEFAULT_OP_PATH = Path(os.environ.get("JOBMARKET_OP_PATH") or "op")
DEFAULT_USAJOBS_SECRET_REF = os.environ.get("JOBMARKET_USAJOBS_SECRET_REF", "")
DEFAULT_ANTHROPIC_SECRET_REF = os.environ.get("JOBMARKET_ANTHROPIC_SECRET_REF", "")
KEYRING_SERVICE = os.environ.get("JOBMARKET_KEYRING_SERVICE", "JobMarketIntel")


@dataclass(slots=True)
class JobMarketCredentials:
    usajobs_email: str | None
    usajobs_api_key: str | None
    anthropic_api_key: str | None


def load_credentials(
    *,
    op_path: Path = DEFAULT_OP_PATH,
    usajobs_secret_ref: str = DEFAULT_USAJOBS_SECRET_REF,
    anthropic_secret_ref: str = DEFAULT_ANTHROPIC_SECRET_REF,
    service_name: str = KEYRING_SERVICE,
    use_op: bool = True,
) -> tuple[JobMarketCredentials, list[str]]:
    """Best-effort credential load. Missing secrets degrade gracefully with warnings."""
    warnings: list[str] = []

    cached_usajobs = _read_keyring(service_name, "usajobs")
    cached_anthropic = _read_keyring(service_name, "anthropic")

    usajobs_email: str | None = None
    usajobs_api_key: str | None = None
    if cached_usajobs:
        try:
            parsed = json.loads(cached_usajobs)
            usajobs_email = parsed.get("email")
            usajobs_api_key = parsed.get("api_key")
        except json.JSONDecodeError:
            warnings.append("Cached USAJobs credentials in keyring were not valid JSON; refreshing from 1Password.")

    anthropic_api_key: str | None = cached_anthropic

    if (not usajobs_email or not usajobs_api_key) and use_op and usajobs_secret_ref:
        try:
            raw = _read_secret(op_path, usajobs_secret_ref)
            parsed = json.loads(raw)
            usajobs_email = parsed.get("email")
            usajobs_api_key = parsed.get("api_key")
            if usajobs_email and usajobs_api_key:
                _write_keyring(
                    service_name, "usajobs", json.dumps({"email": usajobs_email, "api_key": usajobs_api_key})
                )
        except Exception as exc:
            warnings.append(f"USAJobs credentials unavailable: {exc}")

    if not anthropic_api_key and use_op and anthropic_secret_ref:
        try:
            raw = _read_secret(op_path, anthropic_secret_ref).strip()
            if raw:
                anthropic_api_key = raw
                _write_keyring(service_name, "anthropic", anthropic_api_key)
        except Exception as exc:
            warnings.append(f"Anthropic API key unavailable: {exc}")

    return (
        JobMarketCredentials(
            usajobs_email=usajobs_email,
            usajobs_api_key=usajobs_api_key,
            anthropic_api_key=anthropic_api_key,
        ),
        warnings,
    )


def _read_secret(op_path: Path, secret_ref: str) -> str:
    # No existence check — `op_path` may be a bare command name resolved via PATH.
    # subprocess.run raises FileNotFoundError naturally if the binary can't be found,
    # and load_credentials() catches it as a warning.
    completed = subprocess.run(
        [str(op_path), "read", secret_ref],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"op read failed for {secret_ref}")
    return completed.stdout


def _read_keyring(service: str, username: str) -> str | None:
    try:
        return keyring.get_password(service, username)
    except Exception:
        return None


def _write_keyring(service: str, username: str, value: str) -> None:
    try:
        keyring.set_password(service, username, value)
    except Exception:
        return


def clear_cached_credentials(service_name: str = KEYRING_SERVICE) -> None:
    for username in ("usajobs", "anthropic"):
        try:
            keyring.delete_password(service_name, username)
        except keyring.errors.PasswordDeleteError:
            continue
