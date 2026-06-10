"""Streamlit dashboard for browsing job-market snapshots and triggering scrapes.

Run via:
    streamlit run job_market_intel/dashboard.py --server.address 127.0.0.1
or:
    job-market-dashboard   (console script; uses hardened launcher flags)

This file is intentionally UI-only — all testable logic lives in
dashboard_state.py. If something here looks "smart," it probably belongs there.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from job_market_intel.dashboard_state import (
    ScrapeOptions,
    ScrapeRunner,
    build_scrape_command,
    detect_available_credentials,
    list_snapshots,
    load_snapshot,
)


def _public_mode() -> bool:
    """Public-mode flag — set JOBMARKET_PUBLIC_MODE=1 when deploying to Streamlit
    Community Cloud (or any other public host). Hides the "Run a new scrape"
    sidebar form, the credential-status panel, and the last-run log tail. The
    read-only views (Certifications, Requirements, Listings tabs + the snapshot
    selector + warnings) all stay visible to anyone.

    Read on every call so the streamlit_app.py shim can set the env var before
    calling main() without worrying about module-load ordering.
    """
    return os.environ.get("JOBMARKET_PUBLIC_MODE", "").strip() in {"1", "true", "yes"}


REPORTS_DIR = Path("reports")
CACHE_DIR = Path("cache") / "dashboard"

# ---------------------------------------------------------------------------
# Color palette — used consistently across the Certifications and Requirements
# tabs so the eye learns the bucket identity quickly. WCAG AA on Streamlit's
# light AND dark themes; distinguishable under deuteranopia (hue + luminance
# both differ). Pinned here as module constants so colors are tunable in one
# place instead of hunting through six chart definitions.
# ---------------------------------------------------------------------------
_COLOR_SOC = "#1A9BA1"  # teal — Junior SOC Analyst
_COLOR_HELP = "#D4632B"  # deep orange — Help Desk / IT Admin
_BUCKET_LABELS = {"junior_soc": "Junior SOC", "help_desk_it_admin": "Help Desk / IT Admin"}
_BUCKET_COLORS = {"junior_soc": _COLOR_SOC, "help_desk_it_admin": _COLOR_HELP}


def _configure_page() -> None:
    """Set Streamlit page config — must be the FIRST Streamlit command per rerun.

    Called from `main()` so it runs on every Streamlit rerun, regardless of
    entry point. Previously this was at module-level: that worked locally
    (where `streamlit run dashboard.py` re-executes the whole file on every
    interaction) but broke on Streamlit Cloud (where the entry script is
    `streamlit_app.py` and the dashboard module is import-cached — module-
    level code only fires on the first page load, and Streamlit falls back
    to centered/narrow layout on every rerun after that).
    """
    st.set_page_config(
        page_title="Job Market Intel",
        page_icon="💼",
        layout="wide",
        initial_sidebar_state="expanded",
    )


# ---------------------------------------------------------------------------
# Cached data loaders. mtime in cache key auto-invalidates on file change.
# ---------------------------------------------------------------------------


@st.cache_data(ttl=5, show_spinner=False)
def _load_snapshot_cached(path_str: str, mtime: float) -> dict | None:
    del mtime  # part of cache key only
    return load_snapshot(Path(path_str))


def _snapshot_with_mtime(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _load_snapshot_cached(str(path), mtime)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _render_sidebar(runner: ScrapeRunner) -> tuple[Path | None, ScrapeOptions, bool]:
    """Return (selected_snapshot_path, scrape_options, scrape_clicked)."""
    st.sidebar.title("Job Market Intel")

    snapshots = list_snapshots(REPORTS_DIR)

    selected: Path | None = None
    if snapshots:
        options = {p.stem.replace("snapshot-", ""): p for p in snapshots}
        labels = list(options.keys())
        choice = st.sidebar.selectbox("Snapshot", labels, index=0)
        selected = options[choice]
        snap = _snapshot_with_mtime(selected)
        if snap is not None:
            count = snap.get("summary", {}).get("total_listings_post_dedup", "?")
            sources = ", ".join(sorted(snap.get("summary", {}).get("per_source_pre_dedup", {}).keys()))
            st.sidebar.caption(f"{count} unique listings · {sources or 'no sources'}")
    elif not _public_mode():
        st.sidebar.info("No snapshots yet. Run a scrape below.")

    st.sidebar.divider()

    # In public mode (Streamlit Cloud deploy etc.) hide the scrape form, the
    # credential panel, and the live-run log. Viewers just browse snapshots.
    if _public_mode():
        st.sidebar.caption(
            "📊 **Public read-only view.** Snapshots are produced offline and "
            "committed to the repo; pick one above to browse."
        )
        # Return a default opts (never used in public mode) and clicked=False.
        return selected, ScrapeOptions(), False

    availability = detect_available_credentials()

    active_run_id = runner.active_run()
    is_running = active_run_id is not None and runner.status(active_run_id) == "running"

    with st.sidebar.expander("Run a new scrape", expanded=not snapshots):
        _render_cred_status_panel(availability)
        opts, clicked = _render_scrape_form(availability, is_running=is_running)

    if active_run_id:
        _render_last_run_panel(runner, active_run_id)

    return selected, opts, clicked


def _render_cred_status_panel(av) -> None:
    st.markdown("**Credential status**")
    rows = [
        ("Greenhouse", True, "free"),
        ("Lever", True, "free"),
        ("USAJobs", av.usajobs, "creds detected" if av.usajobs else "no creds"),
        ("Claude (LLM)", av.llm, "key detected" if av.llm else "no API key"),
    ]
    for name, ok, note in rows:
        icon = "✓" if ok else "✗"
        color = "green" if ok else "gray"
        st.markdown(f":{color}[{icon}] **{name}** — {note}")
    st.markdown("")


def _render_scrape_form(av, *, is_running: bool) -> tuple[ScrapeOptions, bool]:
    st.markdown("**Sources to scrape**")
    use_greenhouse = st.checkbox(
        "Greenhouse",
        value=False,
        key="src_greenhouse",
        disabled=is_running,
        help="Public ATS boards for ~21 cybersec vendors. Low yield (~13 listings/week). Off by default.",
    )
    use_lever = st.checkbox(
        "Lever",
        value=False,
        key="src_lever",
        disabled=is_running,
        help="Public ATS boards. No productive cybersec slugs found. Off by default.",
    )
    use_usajobs = st.checkbox(
        "USAJobs",
        value=av.usajobs,
        key="src_usajobs",
        disabled=is_running or not av.usajobs,
        help="Requires JOBMARKET_USAJOBS_SECRET_REF in .env" if not av.usajobs else None,
    )
    use_jobspy = st.checkbox(
        "JobSpy ⚠ ToS-grey",
        value=True,
        key="src_jobspy",
        disabled=is_running,
        help="Scrapes Indeed/LinkedIn/Glassdoor — against those sites' ToS. Defaulted ON for full coverage; uncheck to skip.",
    )

    use_llm = st.checkbox(
        "Claude enrichment",
        value=av.llm,
        key="use_llm",
        disabled=is_running or not av.llm,
        help="Requires JOBMARKET_ANTHROPIC_SECRET_REF in .env" if not av.llm else None,
    )

    st.markdown("**Role buckets**")
    use_soc = st.checkbox("Junior SOC", value=True, key="role_soc", disabled=is_running)
    use_help = st.checkbox("Help Desk / IT Admin", value=True, key="role_help", disabled=is_running)

    freshness_days = st.number_input(
        "Freshness (days)",
        min_value=1,
        max_value=60,
        value=14,
        step=1,
        key="freshness_days",
        disabled=is_running,
        help="Only keep listings posted within the last N days.",
    )

    st.markdown("**Seniority filter**")
    allowed_seniority = st.multiselect(
        "Allowed seniority levels",
        options=["entry", "mid", "senior", "leadership", "unclear"],
        default=["entry", "unclear"],
        key="allowed_seniority",
        disabled=is_running,
        help=(
            "Drops listings outside the selected seniority buckets. "
            "Default keeps entry + unclear (bare titles like 'SOC Analyst' with no level modifier)."
        ),
    )
    include_unclassified = st.checkbox(
        "Include unclassified roles",
        value=False,
        key="include_unclassified",
        disabled=is_running,
        help="Keep listings the title classifier couldn't bucket. Most are off-topic noise from JobSpy full-text matches.",
    )

    sites: list[str] = []
    if use_greenhouse:
        sites.append("greenhouse")
    if use_lever:
        sites.append("lever")
    if use_usajobs:
        sites.append("usajobs")
    if use_jobspy:
        sites.append("jobspy")

    role_buckets: list[str] = []
    if use_soc:
        role_buckets.append("junior_soc")
    if use_help:
        role_buckets.append("help_desk_it_admin")

    opts = ScrapeOptions(
        sites=sites,
        role_buckets=role_buckets,
        use_llm=use_llm,
        results_per_source=0,  # always pull everything; cap removed from UI
        freshness_days=int(freshness_days),
        allowed_seniority=allowed_seniority or ["entry", "unclear"],
        include_unclassified=bool(include_unclassified),
    )

    button_label = "Scrape in progress…" if is_running else "Run scrape now"
    clicked = st.button(
        button_label,
        type="primary",
        disabled=is_running or not sites or not role_buckets,
        width="stretch",
    )
    if not sites and not is_running:
        st.caption("Pick at least one source.")
    if not role_buckets and not is_running:
        st.caption("Pick at least one role bucket.")
    return opts, clicked


def _render_last_run_panel(runner: ScrapeRunner, run_id: str) -> None:
    st.sidebar.divider()
    st.sidebar.markdown("**Last run**")
    status = runner.status(run_id)
    icon = {"running": "🟡", "succeeded": "✅", "failed": "❌", "unknown": "⚪"}.get(status, "⚪")
    st.sidebar.markdown(f"{icon} **{status.title()}** — `{run_id}`")

    if status == "running":
        # Auto-refresh while running so the log tail and status update.
        try:
            from streamlit_autorefresh import st_autorefresh

            st_autorefresh(interval=2000, key=f"poll_{run_id}")
        except ImportError:
            st.sidebar.caption("(install streamlit-autorefresh for live polling)")
    elif status in {"succeeded", "failed"}:
        if st.sidebar.button("Clear", key=f"clear_{run_id}"):
            runner.clear_active()
            st.rerun()
    elif status == "unknown":
        st.sidebar.warning("Stale run detected.")
        if st.sidebar.button("Clear stale run", key=f"clear_stale_{run_id}"):
            runner.clear_active()
            st.rerun()

    with st.sidebar.expander("Live log", expanded=status == "running"):
        log_lines = runner.tail_log(run_id, max_lines=200)
        if log_lines:
            st.code("\n".join(log_lines), language="text")
        else:
            st.caption("(no log output yet)")


# ---------------------------------------------------------------------------
# Welcome / empty state
# ---------------------------------------------------------------------------


def _render_welcome() -> None:
    st.markdown(
        """
        <div style='text-align:center; padding:4rem 2rem;'>
            <h1>👋 Welcome to Job Market Intel</h1>
            <p style='font-size:1.1rem; color:#888;'>
                You haven't run a scrape yet. The sidebar is preconfigured with the
                sources we detected — click <strong>Run scrape now</strong> to see what
                entry-level SOC and IT roles are actually asking for this week.
            </p>
            <p style='color:#aaa;'>
                First scrape takes ~30 seconds with Greenhouse + Lever only.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Header strip
# ---------------------------------------------------------------------------


def _render_header(snap: dict, prior_snap: dict | None) -> None:
    summary = snap.get("summary", {})
    unique = int(summary.get("total_listings_post_dedup", 0) or 0)
    raw = int(summary.get("total_listings_pre_dedup", 0) or 0)
    llm = int(summary.get("listings_with_llm_extraction", 0) or 0)
    generated_at = snap.get("generated_at", "")

    delta = None
    if prior_snap is not None:
        prior_unique = int(prior_snap.get("summary", {}).get("total_listings_post_dedup", 0) or 0)
        delta = unique - prior_unique

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Unique listings", unique, delta=delta if delta else None)
    col2.metric("Pre-dedup raw", raw)
    col3.metric("Generated", _human_date(generated_at))
    col4.metric("LLM coverage", f"{llm} / {unique}" if unique else "0 / 0")

    warnings = snap.get("warnings", []) or []
    if warnings:
        with st.expander(f"⚠ Last scrape completed with {len(warnings)} warning(s)", expanded=False):
            for w in warnings:
                st.text(f"• {w}")


def _human_date(iso_string: str) -> str:
    if not iso_string:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    except ValueError:
        return iso_string[:10] or "—"
    days = (datetime.now(UTC) - dt).days
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def _render_tabs(snap: dict, prior_snap: dict | None) -> None:
    tab_certs, tab_reqs, tab_listings = st.tabs(["Certifications", "Requirements", "Listings"])
    with tab_certs:
        _render_certs_tab(snap, prior_snap)
    with tab_reqs:
        _render_requirements_tab(snap)
    with tab_listings:
        _render_listings_tab(snap)


def _render_certs_tab(snap: dict, prior_snap: dict | None) -> None:
    stats_by_bucket = snap.get("stats_by_bucket", {}) or {}
    prior_by_bucket = (prior_snap or {}).get("stats_by_bucket", {}) or {}

    col_soc, col_help = st.columns(2)
    for col, bucket_key, title in (
        (col_soc, "junior_soc", "Junior SOC Analyst"),
        (col_help, "help_desk_it_admin", "Help Desk / IT Admin"),
    ):
        with col:
            st.subheader(title)
            bucket = stats_by_bucket.get(bucket_key, {})
            sample = int(bucket.get("sample_size", 0) or 0)
            # Always show the sample-size caption so the reader knows what
            # population the percentages below are computed against.
            st.caption(f"Based on **{sample}** {title.lower()} listing{'s' if sample != 1 else ''} in this snapshot.")
            certs = bucket.get("certifications", []) or []
            if not certs:
                st.caption("No certifications detected in this bucket.")
                continue
            sample_for_pct = max(sample, 1)
            df = pd.DataFrame(
                [
                    {"Certification": c[0], "Count": int(c[1]), "Pct": 100 * int(c[1]) / sample_for_pct}
                    for c in certs[:10]
                ]
            )
            # Use altair directly so we can pin the y-axis sort order. (Bare
            # st.bar_chart lets Vega-Lite auto-sort the categorical axis,
            # ignoring whatever order the dataframe is in.) Bucket-specific
            # color so SOC reads teal and Help Desk reads orange consistently
            # across all panes in the dashboard.
            chart = (
                alt.Chart(df)
                .mark_bar(color=_BUCKET_COLORS.get(bucket_key, _COLOR_SOC))
                .encode(
                    x=alt.X("Count:Q", title="Count"),
                    y=alt.Y("Certification:N", sort="-x", title=None),
                    tooltip=["Certification", "Count", alt.Tooltip("Pct:Q", format=".1f", title="% of listings")],
                )
                .properties(height=max(180, 28 * len(df)))
            )
            st.altair_chart(chart, width="stretch")

            # Week-over-week deltas
            prior_certs = dict(prior_by_bucket.get(bucket_key, {}).get("certifications", []) or [])
            with st.expander("Week-over-week deltas", expanded=False):
                for cert, count in certs[:10]:
                    prev = prior_certs.get(cert)
                    if prev is None:
                        delta_str = "🆕 new this week"
                    else:
                        d = int(count) - int(prev)
                        if d > 0:
                            delta_str = f"▲ {d}"
                        elif d < 0:
                            delta_str = f"▼ {abs(d)}"
                        else:
                            delta_str = "= (no change)"
                    st.text(f"{cert}: {count}  ({delta_str})")


def _modal_value(d: dict) -> str:
    """Return the most-frequent key in a {label: count} dict, formatted with count."""
    if not d:
        return "—"
    k, v = max(d.items(), key=lambda kv: int(kv[1]))
    return f"{k} ({int(v)})"


def _pct_label(num: int, denom: int) -> str:
    """Format 'count (pct%)' with a clean placeholder when denominator is 0."""
    if denom <= 0:
        return "—"
    return f"{num} ({round(100 * num / denom)}%)"


def _req_summary_table(soc: dict, help_: dict, soc_n: int, help_n: int) -> None:
    """Render the at-a-glance comparison table for the Requirements tab.

    A simple HTML table beats four `st.metric` widgets per side because the
    rows force the eye to read "this metric in both buckets" left-to-right
    instead of treating each tile as an isolated number. Also avoids the
    fake clickable affordance st.dataframe gives a flat data table.
    """
    soc_yoe = _pct_label(int(soc.get("yoe_with_value", 0) or 0), soc_n)
    help_yoe = _pct_label(int(help_.get("yoe_with_value", 0) or 0), help_n)
    soc_clear = _pct_label(int(soc.get("clearance_required", 0) or 0), soc_n)
    help_clear = _pct_label(int(help_.get("clearance_required", 0) or 0), help_n)
    soc_deg = _modal_value(soc.get("degree_breakdown", {}) or {})
    help_deg = _modal_value(help_.get("degree_breakdown", {}) or {})

    # Inline CSS uses rgba so it works on Streamlit's light and dark themes.
    # Pill dots reinforce the color-bucket mapping the rest of the tab uses.
    html = f"""
<style>
.req-summary {{
  width: 100%; border-collapse: collapse;
  font-family: -apple-system, "Segoe UI", Inter, system-ui, sans-serif;
}}
.req-summary th, .req-summary td {{
  padding: 10px 14px; text-align: left;
  border-bottom: 1px solid rgba(128,128,128,0.2);
}}
.req-summary th {{ font-weight: 600; font-size: 14px; }}
.req-summary tr:nth-child(odd) td {{ background: rgba(128,128,128,0.04); }}
.req-pill {{
  display: inline-block; width: 10px; height: 10px;
  border-radius: 50%; margin-right: 6px; vertical-align: middle;
}}
.req-pill.soc {{ background: {_COLOR_SOC}; }}
.req-pill.help {{ background: {_COLOR_HELP}; }}
</style>
<table class="req-summary">
  <thead>
    <tr>
      <th>Metric</th>
      <th><span class="req-pill soc"></span>Junior SOC</th>
      <th><span class="req-pill help"></span>Help Desk / IT Admin</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>Listings</td><td>{soc_n}</td><td>{help_n}</td></tr>
    <tr><td>Stated min YoE</td><td>{soc_yoe}</td><td>{help_yoe}</td></tr>
    <tr><td>Clearance required</td><td>{soc_clear}</td><td>{help_clear}</td></tr>
    <tr><td>Most common degree</td><td>{soc_deg}</td><td>{help_deg}</td></tr>
  </tbody>
</table>
"""
    st.markdown(html, unsafe_allow_html=True)


def _req_skills_chart(items: list, sample: int, color: str, label: str, *, limit: int = 8) -> None:
    """One side of the side-by-side skills comparison.

    Pct on the x-axis (NOT count) so cross-bucket comparison is fair despite
    sample-size asymmetry. The chart is bucket-colored so SOC reads teal and
    Help Desk reads orange consistently.
    """
    st.markdown(f"##### {label} <span style='color:{color}'>●</span>", unsafe_allow_html=True)
    st.caption(f"n = {sample}")
    if not items:
        st.caption("No skills extracted for this bucket.")
        return
    sample_for_pct = max(sample, 1)
    df = pd.DataFrame(
        [
            {
                "Skill": s[0] if len(s[0]) <= 24 else s[0][:23] + "…",
                "Pct": round(100 * int(s[1]) / sample_for_pct, 1),
                "Count": int(s[1]),
                "FullSkill": s[0],
            }
            for s in items[:limit]
        ]
    )
    chart = (
        alt.Chart(df)
        .mark_bar(color=color)
        .encode(
            x=alt.X("Pct:Q", title="% of listings"),
            y=alt.Y("Skill:N", sort="-x", title=None, axis=alt.Axis(labelLimit=200)),
            tooltip=[
                alt.Tooltip("FullSkill:N", title="Skill"),
                alt.Tooltip("Pct:Q", format=".1f", title="% of listings"),
                alt.Tooltip("Count:Q", title="Count"),
            ],
        )
        .properties(height=max(220, 28 * len(df)))
    )
    st.altair_chart(chart, width="stretch")


def _req_grouped_bar(
    categories: list[str],
    soc_pcts: list[float],
    help_pcts: list[float],
    title: str,
    *,
    x_order: list[str] | None = None,
    note: str | None = None,
) -> None:
    """A grouped bar chart with both buckets as colored series on shared x-axis.

    Used for distributions where the x categories are the same across buckets
    (YoE bins, degree levels, remote arrangement). Forces direct per-category
    comparison — your eye doesn't have to ping-pong between two charts.
    """
    st.markdown(f"**{title}**")
    if note:
        st.caption(note)
    rows = []
    for i, cat in enumerate(categories):
        rows.append({"Category": cat, "Bucket": "Junior SOC", "Pct": soc_pcts[i]})
        rows.append({"Category": cat, "Bucket": "Help Desk / IT Admin", "Pct": help_pcts[i]})
    df = pd.DataFrame(rows)
    x_enc = alt.X(
        "Category:N",
        title=None,
        sort=x_order if x_order is not None else "-y",
        axis=alt.Axis(labelAngle=0),
    )
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=x_enc,
            y=alt.Y("Pct:Q", title="% of listings"),
            color=alt.Color(
                "Bucket:N",
                scale=alt.Scale(
                    domain=["Junior SOC", "Help Desk / IT Admin"],
                    range=[_COLOR_SOC, _COLOR_HELP],
                ),
                legend=None,  # legend rendered once at the top of the section
            ),
            xOffset="Bucket:N",  # this is what makes the bars "grouped" not stacked
            tooltip=[
                alt.Tooltip("Bucket:N"),
                alt.Tooltip("Category:N"),
                alt.Tooltip("Pct:Q", format=".1f", title="% of listings"),
            ],
        )
        .properties(height=240)
    )
    st.altair_chart(chart, width="stretch")


def _req_distribution_charts(soc: dict, help_: dict, soc_n: int, help_n: int) -> None:
    """The three grouped-bar distribution charts: YoE / Degree / Remote.

    Each uses Pct on the y-axis so the visual comparison is normalized for
    sample-size asymmetry. Skips Remote when both buckets are >90%
    "unspecified" (regex-only snapshots; LLM enrichment populates the field
    but the regex extractor doesn't yet).
    """
    st.markdown("### Distribution details")
    st.caption(
        "Same x-axis categories across buckets — bars are grouped so you can see at a glance which role demands more in each bin."
    )
    # Legend (rendered once for the whole section so each chart can omit it).
    legend_html = (
        f"<div style='font-size:13px;margin-bottom:8px;'>"
        f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;"
        f"background:{_COLOR_SOC};margin-right:6px;vertical-align:middle;'></span>Junior SOC"
        f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;"
        f"background:{_COLOR_HELP};margin:0 6px 0 16px;vertical-align:middle;'></span>"
        f"Help Desk / IT Admin</div>"
    )
    st.markdown(legend_html, unsafe_allow_html=True)

    # ---- Years of experience ----
    yoe_order = ["0", "1-2", "3-5", "6+"]
    soc_yoe = soc.get("yoe_histogram", {}) or {}
    help_yoe = help_.get("yoe_histogram", {}) or {}
    soc_yoe_total = max(sum(int(soc_yoe.get(k, 0)) for k in yoe_order), 1)
    help_yoe_total = max(sum(int(help_yoe.get(k, 0)) for k in yoe_order), 1)
    soc_yoe_pcts = [100 * int(soc_yoe.get(k, 0)) / soc_yoe_total for k in yoe_order]
    help_yoe_pcts = [100 * int(help_yoe.get(k, 0)) / help_yoe_total for k in yoe_order]
    if soc_yoe or help_yoe:
        _req_grouped_bar(yoe_order, soc_yoe_pcts, help_yoe_pcts, "Years of experience required", x_order=yoe_order)
    else:
        st.caption("No explicit YoE captured in this snapshot.")

    # ---- Degree ----
    soc_deg = soc.get("degree_breakdown", {}) or {}
    help_deg = help_.get("degree_breakdown", {}) or {}
    deg_keys = sorted({*soc_deg.keys(), *help_deg.keys()})
    if deg_keys:
        soc_deg_total = max(sum(int(soc_deg.get(k, 0)) for k in deg_keys), 1)
        help_deg_total = max(sum(int(help_deg.get(k, 0)) for k in deg_keys), 1)
        soc_deg_pcts = [100 * int(soc_deg.get(k, 0)) / soc_deg_total for k in deg_keys]
        help_deg_pcts = [100 * int(help_deg.get(k, 0)) / help_deg_total for k in deg_keys]
        _req_grouped_bar(deg_keys, soc_deg_pcts, help_deg_pcts, "Degree requirement")
    else:
        st.caption("No explicit degree captured in this snapshot.")

    # ---- Remote arrangement ----
    # Guard: skip when both buckets are dominated by "unspecified" — that's
    # a "no signal" state (the regex extractor doesn't populate the field)
    # and a chart of one "unspecified" bar per bucket is just noise.
    soc_remote = soc.get("remote_arrangement", {}) or {}
    help_remote = help_.get("remote_arrangement", {}) or {}

    def _unspec_dominates(d: dict) -> bool:
        total = sum(int(v) for v in d.values()) or 1
        return int(d.get("unspecified", 0)) / total > 0.9

    if soc_remote and help_remote and (_unspec_dominates(soc_remote) or _unspec_dominates(help_remote)):
        st.markdown("**Remote arrangement**")
        st.caption(
            "Not available for this snapshot — the regex extractor doesn't catch "
            "remote/hybrid/onsite signals yet, so most listings show as 'unspecified'. "
            "LLM-enriched snapshots will populate this chart."
        )
    elif soc_remote or help_remote:
        remote_keys = ["remote", "hybrid", "onsite"]
        soc_remote_total = max(sum(int(soc_remote.get(k, 0)) for k in remote_keys), 1)
        help_remote_total = max(sum(int(help_remote.get(k, 0)) for k in remote_keys), 1)
        soc_remote_pcts = [100 * int(soc_remote.get(k, 0)) / soc_remote_total for k in remote_keys]
        help_remote_pcts = [100 * int(help_remote.get(k, 0)) / help_remote_total for k in remote_keys]
        _req_grouped_bar(
            ["Remote", "Hybrid", "Onsite"],
            soc_remote_pcts,
            help_remote_pcts,
            "Remote arrangement",
        )


def _req_responsibilities(soc: dict, help_: dict, soc_n: int, help_n: int) -> None:
    """Render the responsibilities tables side-by-side, but only when at
    least one bucket has data. Empty in regex-only snapshots — the field
    is populated by the LLM extractor only."""
    soc_resp = soc.get("responsibilities", []) or []
    help_resp = help_.get("responsibilities", []) or []
    if not soc_resp and not help_resp:
        return  # don't allocate a section header when both are empty

    st.markdown("### Most-mentioned responsibilities")
    col_a, col_b = st.columns(2)
    for col, items, sample, label, color in (
        (col_a, soc_resp, soc_n, "Junior SOC", _COLOR_SOC),
        (col_b, help_resp, help_n, "Help Desk / IT Admin", _COLOR_HELP),
    ):
        with col:
            st.markdown(f"##### {label} <span style='color:{color}'>●</span>", unsafe_allow_html=True)
            if not items:
                st.caption("None detected for this snapshot.")
                continue
            sample_for_pct = max(sample, 1)
            df = pd.DataFrame(
                [
                    {
                        "Responsibility": r[0],
                        "Pct of listings": round(100 * int(r[1]) / sample_for_pct, 1),
                        "Count": int(r[1]),
                    }
                    for r in items[:8]
                ]
            )
            df = df.sort_values("Count", ascending=False)
            st.dataframe(df, width="stretch", hide_index=True)


def _render_requirements_tab(snap: dict) -> None:
    """Side-by-side comparison of Junior SOC vs Help Desk / IT Admin.

    The whole point of the tool is comparison, so both buckets render
    simultaneously — no radio toggle. Section order:
        1. Sample-size headline + optional warning banner
        2. Summary metrics table (full width)
        3. Top skills (side-by-side, % of listings on x-axis)
        4. Grouped-bar distributions (YoE, Degree, Remote-when-populated)
        5. Responsibilities (side-by-side, only when non-empty)
    """
    stats_by_bucket = snap.get("stats_by_bucket", {}) or {}
    soc = stats_by_bucket.get("junior_soc", {}) or {}
    help_ = stats_by_bucket.get("help_desk_it_admin", {}) or {}
    soc_n = int(soc.get("sample_size", 0) or 0)
    help_n = int(help_.get("sample_size", 0) or 0)

    if soc_n == 0 and help_n == 0:
        st.info("No listings in this snapshot.")
        return

    # ---- Header strip ----
    st.markdown(
        f"### Comparing "
        f"<span style='color:{_COLOR_SOC};font-weight:600;'>{soc_n} Junior SOC</span>"
        f" vs "
        f"<span style='color:{_COLOR_HELP};font-weight:600;'>{help_n} Help Desk / IT Admin</span>"
        f" listings",
        unsafe_allow_html=True,
    )
    # Imbalance warning — percentages are far more reliable than raw counts
    # when the sample sizes differ by 5x or more.
    if min(soc_n, help_n) > 0:
        ratio = max(soc_n, help_n) / min(soc_n, help_n)
        if ratio >= 5:
            st.warning(
                f"Sample sizes differ significantly ({ratio:.1f}:1). "
                "Percentages (rendered throughout) are reliable; raw counts can mislead."
            )

    # ---- Summary metrics table ----
    _req_summary_table(soc, help_, soc_n, help_n)

    st.divider()

    # ---- Top skills (side-by-side, % on x-axis) ----
    st.markdown("### Top skills employers are asking for")
    st.caption("% of listings is on the x-axis, not raw count, so the comparison is fair across the two buckets.")
    col_a, col_b = st.columns(2)
    with col_a:
        _req_skills_chart(soc.get("technical_skills") or [], soc_n, _COLOR_SOC, "Junior SOC")
    with col_b:
        _req_skills_chart(help_.get("technical_skills") or [], help_n, _COLOR_HELP, "Help Desk / IT Admin")

    st.divider()

    # ---- Distribution grouped bars (YoE / Degree / Remote-conditional) ----
    _req_distribution_charts(soc, help_, soc_n, help_n)

    # ---- Responsibilities (conditional — populated only by the LLM extractor) ----
    _req_responsibilities(soc, help_, soc_n, help_n)


def _render_listings_tab(snap: dict) -> None:
    listings = snap.get("listings", []) or []
    if not listings:
        st.caption("No listings in this snapshot.")
        return

    # Build a parallel dataframe of display fields. The DataFrame keeps the
    # ORIGINAL positional index in `df.index` so that even after filtering we
    # can map the selected visible row back to the source listing via
    # `listings[df.index[selected]]`.
    rows = []
    descriptions: list[str] = []  # parallel index for body-text search
    for listing in listings:
        extracted = listing.get("extracted") or {}
        rows.append(
            {
                "Title": listing.get("title", ""),
                "Company": listing.get("company", ""),
                "Location": listing.get("location", ""),
                "Role": listing.get("role_bucket", ""),
                "Sources": ", ".join(listing.get("sources", []) or []),
                "Certs": ", ".join(extracted.get("certifications", []) or []),
                "YoE min": extracted.get("years_experience_min"),
                "Remote": extracted.get("remote_arrangement", ""),
                "Posted": listing.get("posted_at", ""),
                "URL": (listing.get("source_urls") or [""])[0],
            }
        )
        descriptions.append(listing.get("description", "") or "")
    df = pd.DataFrame(rows)
    # Attach a Series so we can include description-body matches in the filter
    # without polluting the visible table.
    description_series = pd.Series(descriptions)

    search = st.text_input(
        "Search title / company / description",
        key="listings_search",
        help="Case-insensitive substring match across title, company, AND description body.",
    )
    if search:
        mask = (
            df["Title"].str.contains(search, case=False, na=False)
            | df["Company"].str.contains(search, case=False, na=False)
            | description_series.str.contains(search, case=False, na=False)
        )
        df = df[mask]

    bucket_filter = st.multiselect("Role bucket", sorted(df["Role"].unique().tolist()), default=None)
    if bucket_filter:
        df = df[df["Role"].isin(bucket_filter)]

    if df.empty:
        st.caption("No listings match these filters.")
        return

    st.caption(
        f"Showing {len(df)} listing{'s' if len(df) != 1 else ''}. **Click any row** to see the full details below."
    )

    # Native row-selection: each click triggers a rerun and reports the
    # POSITIONAL row index of the selection (0-based within the visible
    # dataframe). We map that back to the original `listings` list via
    # `df.index` — pandas preserves the original index across our filters,
    # so this stays correct after search + bucket filtering.
    event = st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="listings_table",
    )

    selected_visible_idx: int | None = None
    selection = getattr(event, "selection", None)
    if selection is not None:
        rows_selected = selection.get("rows") if isinstance(selection, dict) else getattr(selection, "rows", None)
        if rows_selected:
            selected_visible_idx = int(rows_selected[0])

    if selected_visible_idx is None:
        st.info(
            "👆 Click a row above to see the full job description, extracted requirements, and a link to the original posting."
        )
        return

    # Map visible-row index → original listings index via df's preserved index.
    original_indices = df.index.tolist()
    if selected_visible_idx >= len(original_indices):
        return  # defensive — shouldn't happen but stay graceful on stale state
    listing = listings[int(original_indices[selected_visible_idx])]

    st.divider()
    inspect_col1, inspect_col2 = st.columns([2, 1])
    with inspect_col1:
        st.markdown(f"### {listing.get('title')}")
        st.markdown(f"**{listing.get('company')}** · {listing.get('location')}")
        if listing.get("source_urls"):
            st.markdown(f"[Open original posting]({listing['source_urls'][0]})")
        st.markdown("**Description**")
        # Render in a scrollable bordered container instead of a disabled
        # text_area — disabled inputs render in low-contrast gray which makes
        # long job descriptions hard to read. A bordered container gives the
        # same visual frame and scroll behavior at full text contrast.
        with st.container(height=400, border=True):
            description_text = listing.get("description", "") or "_(no description)_"
            st.markdown(description_text)
    with inspect_col2:
        st.markdown("**Extracted**")
        st.json(listing.get("extracted") or {})


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    # First Streamlit call of the rerun — sets layout=wide etc. before any
    # other st.* call elsewhere in the dashboard. See _configure_page() docstring.
    _configure_page()
    runner = ScrapeRunner(CACHE_DIR)
    selected_snapshot_path, opts, scrape_clicked = _render_sidebar(runner)

    # Layer 2 of 4 public-mode defenses: even if the UI gate in
    # _render_sidebar somehow returned scrape_clicked=True (regression, bug,
    # session manipulation), refuse to even ATTEMPT the build+spawn here.
    # Layers 3 and 4 in dashboard_state.py would also raise PermissionError,
    # so a regression in any one of these four layers alone still blocks the
    # scrape. See streamlit_app.py for the full posture comment.
    if scrape_clicked and not _public_mode():
        availability = detect_available_credentials()
        cmd = build_scrape_command(opts, availability=availability)
        try:
            runner.start(cmd)
        except Exception as exc:
            st.sidebar.error(f"Failed to start scrape: {exc}")
        else:
            st.rerun()

    if selected_snapshot_path is None:
        _render_welcome()
        return

    snap = _snapshot_with_mtime(selected_snapshot_path)
    if snap is None:
        st.error(f"Snapshot corrupt or unreadable: {selected_snapshot_path.name}. See reports/ for the file.")
        return

    # Prior snapshot for deltas, if there are at least 2 snapshots.
    all_snapshots = list_snapshots(REPORTS_DIR)
    prior_snap = None
    if len(all_snapshots) >= 2:
        for p in all_snapshots:
            if p != selected_snapshot_path:
                prior_snap = _snapshot_with_mtime(p)
                break

    _render_header(snap, prior_snap)
    _render_tabs(snap, prior_snap)


# Guard the module-level invocation so importing `main` from this file does
# NOT also execute it. This matters for the Streamlit Cloud entry point
# (`streamlit_app.py`) which does `from job_market_intel.dashboard import main`
# and then calls `main()` itself. Without the guard, main() fires twice per
# page render, causing StreamlitDuplicateElementId errors on every widget.
#
# Local entry point (`streamlit run dashboard.py` via `job-market-dashboard`
# console script) still works because Streamlit's runner makes this file
# `__main__` directly — the guard passes and main() fires exactly once.
if __name__ == "__main__":
    main()
