"""Streamlit Community Cloud entry point.

Streamlit Cloud looks for `streamlit_app.py` (or `app.py`) at the repo root.
We delegate to the real dashboard inside the package so this file and the
local CLI (`job-market-dashboard`) share one dashboard implementation.

The cloud build also reads `requirements.txt` at the repo root to install
dependencies; pyproject.toml is sometimes parsed but only loosely.

# SECURITY POSTURE

THIS FILE IS THE PUBLIC ENTRY POINT. It UNCONDITIONALLY forces public mode
so that ANY use of this file — whether on Streamlit Cloud, on a misconfigured
internal host, or by a developer accidentally running `streamlit run
streamlit_app.py` locally — has the scrape form, credential status panel,
and live-run log fully disabled.

Local development with the full scrape UI MUST go through the dedicated
local entry point (`job-market-dashboard` console script, which invokes
`dashboard_launcher.launch()` and runs `streamlit run dashboard.py`
directly). That bypasses this file entirely.

Setting JOBMARKET_PUBLIC_MODE here is layer 1 of 4 defenses:
  1. (this file)           streamlit_app.py forces JOBMARKET_PUBLIC_MODE=1.
  2. (dashboard.py)        main() short-circuits the scrape block when public.
  3. (dashboard_state.py)  build_scrape_command() refuses to build a cmd.
  4. (dashboard_state.py)  ScrapeRunner.start() refuses to spawn a process.
Any layer alone blocks scrape execution. All four would have to regress
simultaneously for a visitor to trigger a scrape from the public deploy.
"""

from __future__ import annotations

import os

# Force public mode UNCONDITIONALLY before the dashboard code runs. The
# dashboard reads this env var lazily inside _public_mode(), so setting it
# either before or after the import works — but setting it first leaves
# zero window for any module-level Streamlit call to see "not public" first.
os.environ["JOBMARKET_PUBLIC_MODE"] = "1"

from job_market_intel.dashboard import main

main()
