# infosec-job-market-intel

[![CI](https://github.com/infosecwizardry/infosec-job-market-intel/actions/workflows/ci.yml/badge.svg)](https://github.com/infosecwizardry/infosec-job-market-intel/actions/workflows/ci.yml)
[![CodeQL](https://github.com/infosecwizardry/infosec-job-market-intel/actions/workflows/codeql.yml/badge.svg)](https://github.com/infosecwizardry/infosec-job-market-intel/actions/workflows/codeql.yml)
[![codecov](https://codecov.io/gh/infosecwizardry/infosec-job-market-intel/branch/main/graph/badge.svg)](https://codecov.io/gh/infosecwizardry/infosec-job-market-intel)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## 🚀 Live dashboard

**👉 [infosec-job-market-intel-infosecwizard.streamlit.app](https://infosec-job-market-intel-infosecwizard.streamlit.app/)**

A public, read-only dashboard that updates whenever a new weekly snapshot
is committed to this repo. No login required. What you can do there:

- **Compare entry-level SOC analyst vs help desk / IT admin roles** side by
  side — see which certs, skills, and experience bars each market is asking
  for *this week*.
- **Skim the headline metrics per role bucket** — sample size, % stating a
  minimum YoE, % requiring a clearance, the most common degree, plus
  horizontal bar charts of the top certifications and technical skills.
- **Browse the complete listings table** — filter by role bucket, search by
  title / company / description, click any row to see the full job
  description, extracted requirements (certs, YoE, degree, clearance,
  skills), and **a link straight to the original posting** so you can
  apply if it looks like a fit.
- **Switch between past snapshots** via the sidebar dropdown to see how
  the market has shifted week-over-week.

The scraping, classification, and LLM enrichment all happen locally on the
maintainer's machine — only sanitized, post-filter snapshots are pushed
to the repo and served publicly.

---

A weekly job-market scanner for **junior SOC analyst** and **help desk / IT
admin** roles. Pulls listings from multiple free sources, dedupes them, and
extracts structured requirements (certifications, years of experience, degrees,
clearances, salary, schedule signals, technical skills) so you can answer
questions like:

> *Of 412 junior SOC postings this week, how many required Security+? How many
> mentioned Splunk? What's the median years-of-experience ask?*

Designed for content creators, career coaches, learners, and **job seekers**
who want data-backed answers instead of anecdote — and a single place to
browse every entry-level SOC and help-desk posting our scrapers caught
this week.

## How it works

```
collect -> dedup -> regex extract -> (optional) Claude enrich -> tabulate -> report
```

- **Collectors** (one per source, each independent and failure-isolated):
  - `jobspy` — wraps the [python-jobspy](https://pypi.org/project/python-jobspy/)
    library; covers Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs.
  - `usajobs` — official [USAJobs.gov REST API](https://developer.usajobs.gov/)
    (free key, federal roles only).
  - `greenhouse` — public board JSON per company.
  - `lever` — public board JSON per company.
- **Dedup** — listings cross-posted to multiple boards collapse on a
  `(company, title, location)` hash; the merged record keeps every source label
  and URL for provenance.
- **Extraction**:
  - *Regex pass* (free, deterministic) handles certifications (full canonical
    dictionary: Security+, Network+, CySA+, CCNA, CISSP, OSCP, GSEC, AWS, Azure,
    Splunk, etc.), years-of-experience ranges, degree/clearance/salary/schedule
    signals, named technical skills.
  - *Claude pass* (optional, cheap) catches responsibilities, ambiguous skills,
    and a seniority signal that titles often lie about. Uses Haiku 4.5 with
    prompt caching; per-listing extractions are cached on disk so re-runs are
    no-ops.
- **Output** to `reports/`:
  - `snapshot-YYYY-MM-DD.json` — full structured snapshot.
  - `snapshot-YYYY-MM-DD.csv` — flat one-row-per-listing.
  - `report-YYYY-MM-DD.md` — human-readable summary by role bucket with cert
    rankings, YoE histogram, degree/remote breakdowns, week-over-week deltas.
  - `trend.csv` — append-only long-form for tracking trends over months.

## Legal posture (read before running)

JobSpy scrapes Indeed, LinkedIn, Glassdoor, and ZipRecruiter, which conflicts
with those sites' Terms of Service even though the library itself is open
source. Practical implications:

- **IP blocks** are possible. Keep `--results-per-source` modest (50-100).
- **No commercial redistribution of raw listings.** Aggregated stats
  ("38% of N postings mentioned Security+") are derived data and broadly fine
  to publish. Pasting individual job descriptions verbatim is not.
- If you want a fully ToS-clean dataset, restrict to
  `--sites usajobs greenhouse lever`.

## Setup

```powershell
git clone https://github.com/yourname/infosec-job-market-intel.git
cd infosec-job-market-intel
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # PowerShell
# or: .venv\Scripts\activate.bat # cmd
pip install -e .
```

That installs the package and exposes a `job-market-intel` console command.

### Credentials

Credentials are configured via a local `.env` file (gitignored). Start from the
template:

```powershell
cp .env.example .env
# then edit .env to fill in the values you want
```

All variables are optional. The tool degrades gracefully — a Greenhouse/Lever
run needs nothing set at all:

| Variable | Purpose | If unset |
|---|---|---|
| `JOBMARKET_USAJOBS_SECRET_REF` | 1Password reference to a USAJobs JSON blob (`{"email","api_key"}`). Free key from [developer.usajobs.gov](https://developer.usajobs.gov/). | `usajobs` collector is skipped with a warning. |
| `JOBMARKET_ANTHROPIC_SECRET_REF` | 1Password reference to an Anthropic API key. Enables Claude Haiku enrichment (responsibilities, seniority signal). | LLM enrichment is skipped — regex extraction still runs. |
| `JOBMARKET_OP_PATH` | Path to the 1Password CLI binary. | Assumes `op` is on PATH. |
| `JOBMARKET_KEYRING_SERVICE` | OS keyring service name for the credential cache. | Defaults to `JobMarketIntel`. |

If you don't use 1Password, you can also pass values directly via CLI flags
(`--usajobs-secret-ref`, `--anthropic-secret-ref`), or skip the collectors
entirely:

```powershell
job-market-intel --sites greenhouse lever --no-llm
```

## Usage

Full weekly run, all sources, with LLM enrichment:

```powershell
job-market-intel --results-per-source 100
```

ToS-clean dry run (no JobSpy, no writes):

```powershell
job-market-intel --sites usajobs greenhouse lever --dry-run
```

Single bucket, no LLM, custom companies:

```powershell
job-market-intel `
  --role-buckets help_desk_it_admin `
  --sites greenhouse lever `
  --greenhouse-companies huntress expel `
  --lever-companies binarydefense `
  --no-llm
```

See `job-market-intel --help` for the full CLI.

## Configuration

Most settings live in the CLI flags, but two lists are baked into
`job_market_intel/seeds.py` and worth tuning to your audience:

- `GREENHOUSE_COMPANIES` / `LEVER_COMPANIES` — public board slugs to query.
  Pick MSSPs and mid-market employers whose hiring is representative of the
  market you care about.
- `ROLE_SEEDS` — the search phrases sent to each collector. The defaults are
  broad ("SOC analyst", "help desk technician", etc.); narrow them if you want
  a tighter sample.
- `CERTIFICATIONS` — dictionary of canonical name -> regex variants. Add new
  certs as the market shifts.

## Dashboard

Same Streamlit dashboard available two ways:

| Mode | URL | Capabilities |
|---|---|---|
| **Public** (read-only) | [infosec-job-market-intel-infosecwizard.streamlit.app](https://infosec-job-market-intel-infosecwizard.streamlit.app/) | Browse all weekly snapshots, view per-bucket metrics, search the listings table, click through to original postings. No scrape button. |
| **Local** (full UI) | `http://127.0.0.1:8501` | Everything above PLUS the "Run scrape now" form, credential status panel, and live log tail. Binds to localhost only — never exposed to the network. |

The public deploy reads only what's committed to `reports/` on this repo and
runs in `JOBMARKET_PUBLIC_MODE=1` so the scrape form and credential panels
are hidden. The local run gets the full toolkit.

```powershell
pip install -e ".[ui]"        # one-time install of dashboard extras
job-market-dashboard          # opens http://127.0.0.1:8501 in your browser
```

What you get in both modes:

- **Sidebar** — snapshot picker. (Local-only: credential status panel ✓ Greenhouse · ✓ Lever · ✓/✗ USAJobs · ✓/✗ Claude, "Run a new scrape" form with per-source toggles, live log tail.)
- **Header strip** — unique listings count (post-dedup) with week-over-week delta, raw count, generation date, LLM coverage.
- **Tabs**:
  - **Certifications** — top 10 per role bucket with week-over-week deltas, sample-size caption so percentages are interpretable.
  - **Requirements** — four at-a-glance tiles (listings count, % stating min YoE, % requiring clearance, most-common degree), a top-skills bar chart, a most-mentioned-responsibilities table, and compact distribution charts for YoE / degree / remote arrangement.
  - **Listings** — searchable table (title / company / description), filter by role bucket, click any row to open the full description and extracted-requirements panel below, plus a link to the original posting so candidates can apply.

**Smart defaults**: JobSpy is the only source enabled by default — it's where the volume lives (Indeed / LinkedIn / Google / Glassdoor / ZipRecruiter). Greenhouse is opt-in (~13 listings/week is too low to be worth the boilerplate noise). Lever is opt-in (no productive cybersec company slugs found yet). USAJobs is opt-in and credentials-gated. Claude enrichment auto-enables if `JOBMARKET_ANTHROPIC_SECRET_REF` is set.

**Scrape execution** (local mode only): the "Run scrape now" button spawns a child `job-market-intel` process — the same CLI you'd run directly. The dashboard polls the run via filesystem state (`cache/dashboard/`), so closing the browser tab does NOT kill an in-flight scrape; reopening picks up the live log right where it left off.

## Development

```powershell
pip install -e ".[dev,ui]"
pre-commit install                        # install git hooks (one time)
python -m unittest discover tests         # run tests
```

24 tests cover dedup, regex extraction (cert dictionary, YoE patterns, degree,
clearance, salary, schedule, skills), LLM cache hits, scoring, markdown
rendering, dry-run, and CLI wiring. No tests hit the network.

### Quality gates

Every commit runs these locally via `pre-commit`:

- **ruff** — lint + format (replaces flake8/black/isort)
- **bandit** — Python security AST scanner
- **gitleaks** — secret detection across staged diff and history
- Generic hygiene — trailing whitespace, large file blocker, private key detector, JSON/YAML/TOML syntax, line-ending normalization

Every push and pull request runs these in CI (`.github/workflows/ci.yml`):

- Everything above
- Full test suite on Python 3.11 and 3.13
- **pip-audit** — dependency vulnerability scan against the PyPI advisory DB
- **CodeQL** — GitHub's deep static analysis (`security-and-quality` query set)
- **Codecov** upload for coverage tracking

**Dependabot** opens weekly PRs for pip and GitHub Actions updates
(`.github/dependabot.yml`).

### Accepted security exceptions

- **CVE-2025-46656** (markdownify 0.13.1, ReDoS in HTML parsing) — `python-jobspy`
  hard-pins `markdownify<0.14.0`, so we cannot upgrade to the fixed `0.14.1+`
  without breaking the resolver. The CVE is suppressed in pip-audit via
  `--ignore-vuln CVE-2025-46656`. Risk is bounded: markdownify is only invoked
  by jobspy on scraped HTML and never rendered downstream. Users who want zero
  exposure should run with `--sites usajobs greenhouse lever`. Drop the
  suppression once python-jobspy updates its pin upstream.

## License

MIT.
