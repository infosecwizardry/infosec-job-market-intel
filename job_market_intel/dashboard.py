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
# Page config — must be the FIRST Streamlit call.
# ---------------------------------------------------------------------------

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
        use_container_width=True,
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
            # ignoring whatever order the dataframe is in.)
            chart = (
                alt.Chart(df)
                .mark_bar()
                .encode(
                    x=alt.X("Count:Q", title="Count"),
                    y=alt.Y("Certification:N", sort="-x", title=None),
                    tooltip=["Certification", "Count", alt.Tooltip("Pct:Q", format=".1f", title="% of listings")],
                )
                .properties(height=max(180, 28 * len(df)))
            )
            st.altair_chart(chart, use_container_width=True)

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


def _render_requirements_tab(snap: dict) -> None:
    stats_by_bucket = snap.get("stats_by_bucket", {}) or {}
    available_buckets = [b for b in ("junior_soc", "help_desk_it_admin") if b in stats_by_bucket]
    if not available_buckets:
        st.caption("No bucket data in this snapshot.")
        return
    label_map = {"junior_soc": "Junior SOC", "help_desk_it_admin": "Help Desk / IT Admin"}
    choice = st.radio(
        "Role bucket",
        available_buckets,
        format_func=lambda b: label_map.get(b, b),
        horizontal=True,
    )
    bucket = stats_by_bucket.get(choice, {})
    sample = int(bucket.get("sample_size", 0) or 0)
    bucket_label = label_map.get(choice, choice)
    st.caption(f"Based on **{sample}** {bucket_label} listing{'s' if sample != 1 else ''} in this snapshot.")

    # ------------------------------------------------------------------
    # Inline summary row — four compact at-a-glance numbers replacing the
    # two big bar charts the old layout used for single-value metrics.
    # ------------------------------------------------------------------
    yoe_with_value = int(bucket.get("yoe_with_value", 0) or 0)
    clearance = int(bucket.get("clearance_required", 0) or 0)
    degree_breakdown = bucket.get("degree_breakdown", {}) or {}
    if degree_breakdown:
        modal_degree, modal_count = max(degree_breakdown.items(), key=lambda kv: int(kv[1]))
        modal_label = f"{modal_degree} ({int(modal_count)})"
    else:
        modal_label = "—"

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Listings", sample)
    if sample:
        m2.metric("Stated min YoE", f"{yoe_with_value} ({round(100 * yoe_with_value / sample)}%)")
        m3.metric("Clearance required", f"{clearance} ({round(100 * clearance / sample)}%)")
    else:
        m2.metric("Stated min YoE", "—")
        m3.metric("Clearance required", "—")
    m4.metric("Most common degree", modal_label)

    st.divider()

    # ------------------------------------------------------------------
    # PRIMARY: top technical skills — what the role is actually asking for.
    # This is the answer to "what do I need to know to land this job?".
    # ------------------------------------------------------------------
    st.markdown(f"### Top skills employers are asking for ({bucket_label.lower()})")
    skills = bucket.get("technical_skills", []) or []
    if skills:
        sample_for_pct = max(sample, 1)
        skills_df = pd.DataFrame(
            [
                {
                    "Skill": s[0],
                    "Count": int(s[1]),
                    "Pct of listings": round(100 * int(s[1]) / sample_for_pct, 1),
                }
                for s in skills[:12]
            ]
        )
        # Use altair so the y-axis sort is honored (Vega-Lite otherwise
        # auto-sorts categorical axes alphabetically).
        skills_df = skills_df.sort_values("Count", ascending=False)
        skills_chart = (
            alt.Chart(skills_df)
            .mark_bar()
            .encode(
                x=alt.X("Count:Q", title="Count"),
                y=alt.Y("Skill:N", sort="-x", title=None),
                tooltip=[
                    "Skill",
                    "Count",
                    alt.Tooltip("Pct of listings:Q", format=".1f", title="% of listings"),
                ],
            )
            .properties(height=max(280, 28 * len(skills_df)))
        )
        st.altair_chart(skills_chart, use_container_width=True)
        with st.expander("Skill counts table", expanded=False):
            st.dataframe(skills_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No technical skills extracted for this bucket.")

    # ------------------------------------------------------------------
    # Common responsibilities — what you'll actually DO in the role.
    # ------------------------------------------------------------------
    responsibilities = bucket.get("responsibilities", []) or []
    if responsibilities:
        st.markdown("### Most-mentioned responsibilities")
        sample_for_pct = max(sample, 1)
        resp_df = pd.DataFrame(
            [
                {
                    "Responsibility": r[0],
                    "Count": int(r[1]),
                    "Pct": round(100 * int(r[1]) / sample_for_pct, 1),
                }
                for r in responsibilities[:8]
            ]
        )
        # Convention: categorical tables always sort most → least.
        resp_df = resp_df.sort_values("Count", ascending=False)
        st.dataframe(resp_df, use_container_width=True, hide_index=True)

    # ------------------------------------------------------------------
    # COMPACT secondary row — distribution charts at fixed small height.
    # YoE histogram | Degree breakdown | Remote arrangement
    # ------------------------------------------------------------------
    st.markdown("### Distribution details")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Years of experience**")
        yoe = bucket.get("yoe_histogram", {}) or {}
        if yoe:
            # NOTE: this chart is the one exception to the "sort most → least"
            # convention. YoE buckets are an ordinal/numeric axis; the X
            # position itself encodes the magnitude (0 → 1-2 → 3-5 → 6+), so
            # we preserve numeric order. Sorting by count would scramble the
            # ordering and make the distribution shape unreadable.
            order = ["0", "1-2", "3-5", "6+"]
            df = pd.DataFrame({"YoE": order, "Count": [int(yoe.get(k, 0)) for k in order]})
            st.bar_chart(df.set_index("YoE"), height=180)
        else:
            st.caption("No explicit YoE.")
    with c2:
        st.markdown("**Degree**")
        if degree_breakdown:
            df = pd.DataFrame([{"Degree": k, "Count": int(v)} for k, v in degree_breakdown.items()])
            df = df.sort_values("Count", ascending=False)  # most → least
            # Altair with explicit x-axis sort, so categorical bars stay in
            # count-descending order instead of getting alpha-sorted by Vega.
            chart = (
                alt.Chart(df)
                .mark_bar()
                .encode(x=alt.X("Degree:N", sort="-y"), y=alt.Y("Count:Q"))
                .properties(height=180)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.caption("None detected.")
    with c3:
        st.markdown("**Remote / hybrid / onsite**")
        remote = bucket.get("remote_arrangement", {}) or {}
        if remote:
            df = pd.DataFrame([{"Arrangement": k, "Count": int(v)} for k, v in remote.items()])
            df = df.sort_values("Count", ascending=False)  # most → least
            chart = (
                alt.Chart(df)
                .mark_bar()
                .encode(x=alt.X("Arrangement:N", sort="-y"), y=alt.Y("Count:Q"))
                .properties(height=180)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.caption("—")


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
        use_container_width=True,
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
    runner = ScrapeRunner(CACHE_DIR)
    selected_snapshot_path, opts, scrape_clicked = _render_sidebar(runner)

    if scrape_clicked:
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
        st.error(f"Snapshot corrupt or unreadable: {selected_snapshot_path.name}. " f"See reports/ for the file.")
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
