"""Console-script entry point that boots Streamlit with hardened defaults.

Registered as `job-market-dashboard` in pyproject.toml. Use that, not raw
`streamlit run`, so the bind address and security flags are always set.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DASHBOARD_PATH = Path(__file__).resolve().parent / "dashboard.py"


def launch() -> int:
    """Boot Streamlit pointing at dashboard.py with hardened flags.

    - Binds to 127.0.0.1 only (no local-network exposure).
    - Disables Streamlit telemetry.
    - Leaves XSRF and CORS at Streamlit's secure defaults.
    """
    try:
        from streamlit.web import cli as stcli  # type: ignore[import-not-found]
    except ImportError:
        print(
            "Streamlit is not installed. Install the UI extras with:\n" '    pip install -e ".[ui]"',
            file=sys.stderr,
        )
        return 1

    if not DASHBOARD_PATH.exists():
        print(f"Dashboard module not found: {DASHBOARD_PATH}", file=sys.stderr)
        return 1

    # Belt-and-suspenders: set telemetry-off via env var too, in case the CLI
    # flag parsing changes in a future Streamlit version.
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")

    sys.argv = [
        "streamlit",
        "run",
        str(DASHBOARD_PATH),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        "8501",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(launch())
