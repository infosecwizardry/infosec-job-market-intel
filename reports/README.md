# reports/

Weekly snapshots of the job-market scan, plus the human-readable summaries.

## What's tracked in git

| File pattern | Description |
|---|---|
| `snapshot-YYYY-MM-DD.json` | Canonical post-filter snapshot. The Streamlit dashboard reads these. |
| `snapshot-YYYY-MM-DD.csv` | Same data, flattened for spreadsheet use. |
| `report-YYYY-MM-DD.md` | Human-readable bucket summary from the same scan. |
| `discovered-patterns-YYYY-MM-DD.md` | Pattern-mining report (when LLM ran with `--mine-unclear`). |
| `trend.csv` | Append-only time-series of certs/skills/YoE — feeds future trend views. |

## What's NOT tracked

The `.gitignore` keeps these out of the repo:

- `snapshot-YYYY-MM-DD.unfiltered.json` — pre-filter debug artifact (~40 MB, used only for re-running `--reclassify`).
- `snapshot-YYYY-MM-DD.raw.json` — any other intermediate stage.
- Anything in `cache/` (LLM-call caches, dashboard state, subagent chunks).

## Weekly workflow

1. Scrape locally: `job-market-intel --sites jobspy ...` (your creds, your machine).
2. Optionally enrich via the `llm-analyze-listings` Claude Code skill (window-free subagent dispatch).
3. `git add reports/snapshot-DATE.json reports/snapshot-DATE.csv reports/report-DATE.md reports/trend.csv`
4. `git commit -m "Snapshot YYYY-MM-DD"`
5. `git push` — Streamlit Cloud auto-redeploys with the new snapshot in the dropdown.
