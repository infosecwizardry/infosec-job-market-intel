"""Streamlit Community Cloud entry point.

Streamlit Cloud looks for `streamlit_app.py` (or `app.py`) at the repo root.
We delegate to the real dashboard inside the package so the local CLI
(`job-market-dashboard`) and the cloud deploy share the exact same code.

The cloud build also expects to read `requirements.txt` at the repo root
to install dependencies; pyproject.toml is sometimes parsed but only loosely.
"""

from __future__ import annotations

import os

from job_market_intel.dashboard import main

# Force public mode on Streamlit Cloud so the scrape form / credential
# panels stay hidden no matter what env vars the deploy ends up with.
# Locally this env var is unset and the full UI shows. Safe to set AFTER
# the import because dashboard reads JOBMARKET_PUBLIC_MODE lazily on each
# render, not at module load.
if "STREAMLIT_SERVER_PORT" in os.environ:
    os.environ.setdefault("JOBMARKET_PUBLIC_MODE", "1")

main()
