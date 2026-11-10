"""Read-only Streamlit dashboard for PostgreSQL embedding backfills.

Run: python -m streamlit run embedding_dashboard.py --server.address 127.0.0.1
For remote access use SSH local forwarding; do not expose this unauthenticated
app to a public interface.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pandas as pd
import psycopg2
import streamlit as st

from embedding_dashboard_data import (
    fetch_failure_history,
    fetch_failures,
    fetch_overview,
    summarize_job,
)

st.set_page_config(
    page_title="Embedding Operations",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(ttl=30, show_spinner=False)
def read_snapshot():
    # Cache protects the DB from refresh storms across browser sessions.
    return fetch_overview()


@st.cache_data(ttl=20, show_spinner=False)
def read_failed_rows(job_key, limit):
    return fetch_failures(job_key, limit)


def display_job(job):
    source = ".".join(str(x) for x in (job.get("source_schema"), job.get("source_table")) if x)
    if not source:
        source = "Unknown table (older run)"
    name = job.get("profile_name")
    label = f"{source} · {job['job_key'][:12]}"
    if name:
        label = f"{name} · {label}"
    if job.get("max_chunks"):
        label += f" · pilot ≤{job['max_chunks']:,}"
    return label


def safe_csv(rows):
    """Avoid Excel formula execution when an operator downloads error records."""
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        clean = {}
        for key, value in row.items():
            text = str(value) if value is not None else ""
            if text.lstrip().startswith(("=", "+", "-", "@")):
                text = "'" + text
            clean[key] = text
        writer.writerow(clean)
    return output.getvalue()


def remember_sample(job_key, successes, timestamp):
    """In-browser sample history only; no monitoring-table writes."""
    if "embedding_history" not in st.session_state:
        st.session_state["embedding_history"] = {}
    history = st.session_state["embedding_history"].setdefault(job_key, [])
    ts = timestamp.timestamp()
    if history and (ts - history[-1][0]) < 10:
        return history
    history.append((ts, int(successes)))
    del history[:-60]
    return history


def observed_rate(history):
    if len(history) < 2:
        return None
    start, end = history[-2], history[-1]
    duration = end[0] - start[0]
    if duration <= 0:
        return None
    return max(0, end[1] - start[1]) * 60.0 / duration


def render_dashboard():
    with st.spinner("Reading embedding job ledger..."):
        snapshot = read_snapshot()

    st.caption(
        f"Database snapshot: {snapshot['at'].strftime('%Y-%m-%d %H:%M:%S UTC')} · "
        "Read-only · 60-second auto-refresh if enabled"
    )

    jobs = snapshot["jobs"]
    if not jobs:
        st.info("No embedding jobs found in this database. Start a pilot or full run first.")
        return

    counts = snapshot["counts"]
    active = snapshot["active"]
    rows = []
    for j in jobs:
        key = j["job_key"]
        summary = summarize_job(j, counts.get(key, {}), active.get(key, {}).get("active_claims", 0))
        rows.append({
            "Job": key[:14] + "…",
            "Source": (
                f"{j['source_schema']}.{j['source_table']}"
                if j.get("source_schema") and j.get("source_table")
                else "Unknown (older job)"
            ),
            "Type": f"Pilot ({j['max_chunks']:,})" if j.get("max_chunks") else "Full / older",
            "Phase": summary["phase"],
            "Discovered": summary["total"],
            "Successful": summary["SUCCESS"],
            "Failed": summary["FAILED"],
            "Skipped": summary["SKIPPED"],
            "Pending": summary["PENDING"],
            "Running": summary["RUNNING"],
            "Active leases": summary["active_claims"],
            "Created (UTC)": j["created_at"],
        })

    st.subheader("All embedding jobs")
    st.caption(
        "Discovered is the number of task rows in this job, not necessarily the total "
        "source-table backlog. Active leases are evidence of recently claimed work, "
        "not a guarantee the process is healthy."
    )
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Discovered": st.column_config.NumberColumn(format="%d"),
            "Successful": st.column_config.NumberColumn(format="%d"),
            "Failed": st.column_config.NumberColumn(format="%d"),
            "Pending": st.column_config.NumberColumn(format="%d"),
        },
    )

    ordered_jobs = sorted(
        jobs,
        key=lambda j: (
            bool(active.get(j["job_key"], {}).get("active_claims", 0)),
            j["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    st.divider()
    st.subheader("Selected job")
    chosen = st.selectbox(
        "Choose a job to investigate",
        options=[j["job_key"] for j in ordered_jobs],
        format_func=lambda key: display_job(next(j for j in ordered_jobs if j["job_key"] == key)),
        key="embedding_selected_job",
    )
    job = next(j for j in jobs if j["job_key"] == chosen)
    summary = summarize_job(job, counts.get(chosen, {}), active.get(chosen, {}).get("active_claims", 0))
    st.code(chosen, language=None)
    st.caption(
        f"Status: {summary['phase']} · "
        f"Discovered: {'complete' if job.get('discovery_done') else 'in progress / unknown'} · "
        f"Model: {job.get('model_name') or 'Not recorded for this older job'}"
    )

    metric_cols = st.columns(6)
    for col, label, value in zip(
        metric_cols,
        ["Discovered", "Successful", "Failed", "Skipped", "Pending", "Running"],
        [summary["total"], summary["SUCCESS"], summary["FAILED"],
         summary["SKIPPED"], summary["PENDING"], summary["RUNNING"]],
    ):
        col.metric(label, f"{value:,}")

    st.progress(
        float(summary["progress"]),
        text=f"{summary['processed']:,} / {summary['total']:,} discovered tasks have terminal outcomes "
             f"({summary['progress']:.1%})",
    )
    if not job.get("discovery_done"):
        st.caption("Discovery is not complete; the progress percentage can decrease as new tasks are discovered.")

    history = remember_sample(chosen, summary["SUCCESS"], snapshot["at"])
    rate = observed_rate(history)
    left, right = st.columns([1, 2])
    with left:
        st.metric(
            "Successful rows/minute",
            f"{rate:,.1f}" if rate is not None else "Collecting samples",
        )
        st.caption("Measured between dashboard refreshes, not a provider token-rate metric.")
        last_start = job.get("last_started_at") or job.get("created_at")
        st.write("**Job created:**", job["created_at"])
        st.write("**Last worker start:**", last_start)
        lease = active.get(chosen, {}).get("latest_lease")
        st.write("**Latest active lease:**", lease if lease else "None currently")
    with right:
        if len(history) > 1:
            chart = pd.DataFrame(
                {"UTC time": [datetime.fromtimestamp(t,timezone.utc) for t, _ in history],
                 "Successful rows": [value for _, value in history]}
            ).set_index("UTC time")
            st.line_chart(chart)
        else:
            st.info("A small throughput trend will appear after the next refresh.")

    st.divider()
    st.subheader("Failures")
    st.caption("Failures are per task; a failed chunk does not stop other chunks.")

    left, right = st.columns([1, 3])
    with left:
        limit = st.selectbox("Rows to inspect", [20, 50, 100, 200], index=1)
        details = st.checkbox("Show detailed error messages", value=False)
    failure_data = read_failed_rows(chosen, limit) if summary["FAILED"] else {"rows":[],"reasons":[]}
    if failure_data["reasons"]:
        with right:
            st.dataframe(
                pd.DataFrame(failure_data["reasons"]).rename(
                    columns={"reason": "Failure reason", "amount": "Rows"}
                ),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.success("No current FAILED tasks for this job.")

    if failure_data["rows"]:
        fields = ["row_id","attempts","error_code","processing_comment","updated_at"]
        if details:
            fields.insert(3,"error_message")
        st.dataframe(
            pd.DataFrame(failure_data["rows"])[fields],
            hide_index=True,
            use_container_width=True,
        )
        st.download_button(
            "Download displayed failure rows (CSV)",
            safe_csv([{field: row[field] for field in fields} for row in failure_data["rows"]]),
            file_name=f"embedding_failures_{chosen[:12]}.csv",
            mime="text/csv",
        )

    if st.checkbox("Load historical failure events", value=False):
        # This is a separate opt-in query; it isn't executed on every normal refresh.
        with st.spinner("Reading failure history..."):
            records = fetch_failure_history(chosen, limit=50)
        if records:
            cols = ["row_id","error_code","processing_comment","occurred_at"]
            if details:
                cols.insert(2,"error_message")
            st.dataframe(pd.DataFrame(records)[cols], hide_index=True, use_container_width=True)
        else:
            st.info("No failure history found for this job.")

    with st.expander("Deployment quota settings (database-shared)", expanded=False):
        if snapshot["quotas"]:
            st.dataframe(
                pd.DataFrame([{
                    "Deployment key": q["deployment_key"][:14] + "…",
                    "RPM cap": q["rpm"],
                    "TPM cap": q["tpm"],
                    "Adaptive multiplier": round(float(q["multiplier"] or 1), 3),
                    "Cooldown until": q["cooldown"],
                } for q in snapshot["quotas"]]),
                hide_index=True,
                use_container_width=True,
            )
            st.caption(
                "Shared among workers using the same deployment and coordination database. "
                "Limits are configured caps, not verified Azure entitlements."
            )
        else:
            st.info("No deployment quotas found.")

    st.caption(
        "Security: Restrict access to an internal network or authenticated proxy. "
        "Use a PostgreSQL SELECT-only account. The dashboard never reads source chunk "
        "text or vector values, and it never launches, stops, or retries jobs."
    )


def main():
    st.title("Embedding Operations")
    st.write("Monitor PostgreSQL embedding backfills across servers, tables and profiles.")

    with st.sidebar:
        st.header("Monitor")
        auto = st.toggle("Auto-refresh", value=True)
        st.caption("One snapshot each minute. PostgreSQL is queried only while the dashboard is open.")
        if st.button("Refresh now", use_container_width=True):
            read_snapshot.clear()
            read_failed_rows.clear()
        st.divider()
        st.caption("Read-only dashboard. Source documents and API credentials are not displayed.")

    try:
        if auto and hasattr(st, "fragment"):
            # Streamlit 1.37+ supports scheduled fragment reruns.
            st.fragment(run_every="60s")(render_dashboard)()
        else:
            if auto:
                st.info("Auto-refresh needs Streamlit with st.fragment; use Refresh now on older versions.")
            render_dashboard()
    except (psycopg2.Error, ValueError) as error:
        # Never display a DSN, password, internal host or SQL details.
        st.error(f"Could not read embedding_runtime tables ({type(error).__name__}).")
        st.info("Check config.ini, PostgreSQL connectivity, read permissions and statement timeout.")


if __name__ == "__main__":
    main()
