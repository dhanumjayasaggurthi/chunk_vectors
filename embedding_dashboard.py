"""Executive and engineering views for distributed embedding backfills.

Read-only source-table rollups are cached for 10 minutes to avoid repeatedly
aggregating a million-row table while PostgreSQL is writing vectors.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pandas as pd
import psycopg2
import streamlit as st

from embedding_dashboard_data import (
    fetch_failure_history, fetch_failures, fetch_overview, summarize_job,
)
from embedding_dashboard_docs import (
    DOCUMENT_CACHE_SECONDS,
    calculate_coverage,
    datasets_from_profiles,
    document_stage,
    fetch_document_details,
    fetch_document_rollup,
)

st.set_page_config(
    page_title="Document Embedding Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


def apply_theme():
    st.markdown("""
    <style>
    .block-container {max-width: 1440px; padding-top: 1.6rem; padding-bottom: 3rem;}
    h1, h2, h3 {letter-spacing: -.025em;}
    div[data-testid="stMetric"] {
        padding: 15px 16px; border: 1px solid rgba(125,145,167,.18);
        border-radius: 13px; background: var(--secondary-background-color);
    }
    div[data-testid="stMetricLabel"] {font-weight: 600;}
    div[data-testid="stMetricValue"] {letter-spacing: -.035em;}
    div[data-testid="stSidebar"] {border-right: 1px solid rgba(125,145,167,.18);}
    .leadership-kicker {font-weight: 700; letter-spacing: .12em;
      text-transform: uppercase; font-size: .74rem; opacity: .7;}
    .leadership-intro {font-size: 1.02rem; line-height: 1.7; opacity: .86;}
    .leadership-section {font-weight: 700; letter-spacing: .035em; font-size: .84rem;
      text-transform: uppercase; opacity: .65; margin-top: .8rem;}
    </style>
    """, unsafe_allow_html=True)


@st.cache_data(ttl=30, show_spinner=False)
def read_snapshot(job_limit):
    return fetch_overview(job_limit)


@st.cache_data(ttl=20, show_spinner=False)
def read_failed_rows(job_key, limit):
    return fetch_failures(job_key, limit)


@st.cache_data(ttl=DOCUMENT_CACHE_SECONDS, show_spinner=False)
def read_document_rollup(schema, table, vector_column, id_column):
    return fetch_document_rollup({
        "schema": schema, "table": table,
        "vector_column": vector_column, "id_column": id_column,
    })


@st.cache_data(ttl=30, show_spinner=False)
def read_document_detail(schema, table, vector_column, id_column, doc_id):
    return fetch_document_details({
        "schema":schema, "table":table, "vector_column":vector_column,
        "id_column":id_column,
    },doc_id)


def safe_csv(rows):
    if not rows:
        return ""
    output=io.StringIO()
    writer=csv.DictWriter(output,fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        clean={}
        for key,value in row.items():
            cell=str(value) if value is not None else ""
            if cell.lstrip().startswith(("=","+","-","@")):
                cell="'"+cell
            clean[key]=cell
        writer.writerow(clean)
    return output.getvalue()


def remember_sample(job_key, success, timestamp):
    history=st.session_state.setdefault("embedding_history",{}).setdefault(job_key,[])
    t=timestamp.timestamp()
    if history and t-history[-1][0]<10:
        return history
    history.append((t,int(success)))
    del history[:-60]
    return history


def observed_rate(history):
    if len(history)<2:
        return None
    elapsed=history[-1][0]-history[-2][0]
    return max(0,history[-1][1]-history[-2][1])*60/elapsed if elapsed>0 else None


def job_label(job):
    source=".".join(str(x) for x in (job.get("source_schema"),job.get("source_table")) if x)
    source=source or "Older job · target unlabeled"
    profile=job.get("profile_name")
    label=f"{source} · {job['job_key'][:12]}"
    if profile:
        label=f"{profile} · {label}"
    if job.get("max_chunks"):
        label+=f" · pilot ≤{job['max_chunks']:,}"
    return label


def read_docs(dataset):
    with st.spinner(
        "Calculating documents and chunk coverage from source rows. "
        "This runs at most once every 10 minutes per dataset..."
    ):
        return read_document_rollup(
            dataset["schema"],dataset["table"],
            dataset["vector_column"],dataset["id_column"],
        )


def frame_priorities(rows):
    if not rows:
        return pd.DataFrame(columns=["Document ID","File","Chunks","Embedded",
                                     "Remaining","Coverage %","Attention notes"])
    return pd.DataFrame([{
        "Document ID": row["document_id"],
        "File": row.get("file_name") or "—",
        "Chunks": int(row["total_chunks"]),
        "Embedded": int(row["embedded_chunks"]),
        "Remaining": int(row["remaining_chunks"]),
        "Coverage %": round(
            100*calculate_coverage(int(row["embedded_chunks"]),int(row["total_chunks"])),1),
        "Attention notes": int(row.get("noted_chunks") or 0),
    } for row in rows])


def executive_view(dataset, snapshot):
    st.markdown('<div class="leadership-kicker">Portfolio readiness</div>',
                unsafe_allow_html=True)
    st.title("Document embedding coverage")
    st.markdown(
        '<div class="leadership-intro">How much of our searchable document '
        'collection is ready for vector retrieval?</div>', unsafe_allow_html=True
    )
    st.caption(
        f"Source of truth: {dataset['schema']}.{dataset['table']} "
        f"· PostgreSQL database: {snapshot['database']} "
        "· Source coverage is across all jobs, including embeddings that existed before the current run."
    )
    report=read_docs(dataset)
    total_docs=int(report["total_documents"])
    done=int(report["completed_documents"])
    partial=int(report["partial_documents"])
    untouched=int(report["untouched_documents"])
    attention=int(report["attention_documents"])
    total_chunks=int(report["total_chunks"])
    embedded=int(report["embedded_chunks"])
    remaining=int(report["missing_chunks"])
    doc_coverage=calculate_coverage(done,total_docs)
    chunk_coverage=calculate_coverage(embedded,total_chunks)

    st.markdown('<div class="leadership-section">Document readiness</div>',
                unsafe_allow_html=True)
    a,b,c,d,e=st.columns(5,gap="small")
    a.metric("Total documents",f"{total_docs:,}")
    b.metric("Fully embedded",f"{done:,}",help="Every chunk in the document has a stored vector.")
    c.metric("Partially embedded",f"{partial:,}",help="At least one chunk, but not all, has a vector.")
    d.metric("Not started",f"{untouched:,}",help="No chunks in these documents have vectors.")
    e.metric("Needs attention",f"{attention:,}",help="Documents with NULL vectors and non-NULL processing notes; not necessarily fatal errors.")

    st.progress(doc_coverage,text=f"{doc_coverage:.1%} of documents fully embedded")
    st.markdown('<div class="leadership-section">Chunk coverage</div>',
                unsafe_allow_html=True)
    x,y,z=st.columns(3)
    x.metric("Total chunks",f"{total_chunks:,}")
    y.metric("Vectors available",f"{embedded:,}")
    z.metric("Chunks still missing vectors",f"{remaining:,}")
    st.progress(chunk_coverage,
                text=f"{chunk_coverage:.1%} of source chunks have stored embeddings")

    if total_docs:
        if done==total_docs and remaining==0:
            st.success("All identifiable documents are fully embedded at this snapshot.")
        elif partial:
            st.info(
                f"{partial:,} documents are partially searchable but **not yet fully ready**. "
                f"{remaining:,} chunks still need vectors across this source table."
            )

    if int(report.get("unmapped_chunks") or 0)>0:
        st.warning(
            f"{int(report['unmapped_chunks']):,} chunks have no usable doc_id. "
            "They are included in chunk totals but excluded from document counts."
        )

    st.markdown('<div class="leadership-section">Where documents stand</div>',
                unsafe_allow_html=True)
    left,right=st.columns([1,1.5],gap="large")
    with left:
        phases=pd.DataFrame({
            "Readiness":["Fully embedded","Partially embedded","Not started"],
            "Documents":[done,partial,untouched],
        })
        st.bar_chart(phases,x="Readiness",y="Documents",height=245)
        st.caption("A document is fully ready only when all its chunk vectors are present.")
    with right:
        priority=frame_priorities(report.get("priority_documents") or [])
        st.markdown("**Largest unfinished documents**")
        st.caption(
            "Up to 25 documents with the most missing chunks. "
            "File name is a representative value, not necessarily a unique document name."
        )
        if priority.empty:
            st.success("No unfinished document IDs found.")
        else:
            st.dataframe(
                priority[["Document ID","File","Chunks","Embedded",
                          "Remaining","Coverage %","Attention notes"]],
                hide_index=True,use_container_width=True,height=245,
                column_config={
                    "Coverage %":st.column_config.ProgressColumn(
                        "Coverage %",min_value=0,max_value=100,format="%.1f%%"
                    ),
                },
            )
            st.download_button(
                "Download highest-backlog documents (CSV)",
                safe_csv(priority.to_dict("records")),
                file_name=f"document_backlog_{dataset['table']}.csv",
                mime="text/csv",
                help="Exports the 25 highlighted documents, not the entire source table.",
            )

    st.caption(
        f"Source snapshot: {report['snapshot_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}. "
        "Document rollups are cached for 10 minutes to protect PostgreSQL. "
        "New ingestions can increase the total. This is source-table coverage, "
        "not one embedding job's task-completion percentage."
    )


def document_explorer(dataset):
    st.markdown('<div class="leadership-kicker">Document explorer</div>',
                unsafe_allow_html=True)
    st.title("Verify individual documents")
    st.write(
        "Every document is identified by doc_id. Search by the exact doc_id or "
        "choose one of the documents with the most missing chunks."
    )
    report=read_docs(dataset)
    priorities=report.get("priority_documents") or []
    options=["Enter a document ID"]+[p["document_id"] for p in priorities]
    selected=st.selectbox(
        "Suggested unfinished documents",options,
        format_func=lambda x: x if x=="Enter a document ID"
                      else f"{x[:48]}… — {next(p['remaining_chunks'] for p in priorities if p['document_id']==x)} chunks left"
    )
    prefill="" if selected==options[0] else selected
    requested=st.text_input(
        "Exact doc_id",value=prefill,
        key=f"document_input_{dataset['profile']}_{prefill}",
        placeholder="Paste an existing doc_id from the source table",
    )
    if not st.button("Inspect document",type="primary",disabled=not requested.strip()):
        st.caption("Document lookup is read-only and uses the indexed doc_id column.")
        return

    with st.spinner("Reading this document's chunks..."):
        data=read_document_detail(
            dataset["schema"],dataset["table"],
            dataset["vector_column"],dataset["id_column"],requested,
        )
    total=int(data["summary"]["total_chunks"])
    embedded=int(data["summary"]["embedded_chunks"])
    noted=int(data["summary"]["noted_chunks"])
    remaining=total-embedded
    if not total:
        st.warning("No source chunks found with that exact doc_id.")
        return
    st.subheader("Document readiness")
    a,b,c,d=st.columns(4)
    a.metric("Total chunks",f"{total:,}")
    b.metric("Embedded",f"{embedded:,}")
    c.metric("Remaining",f"{remaining:,}")
    d.metric("Unresolved notes",f"{noted:,}")
    st.progress(calculate_coverage(embedded,total),
                text=f"{document_stage(total,embedded)} · {embedded:,}/{total:,} chunks embedded")
    declared=data["summary"].get("declared_chunk_total")
    if declared is not None and int(declared)!=total:
        st.warning(
            f"Data-quality check: source chunk_total reports {int(declared):,}, "
            f"but {total:,} chunk rows currently exist for this doc_id."
        )
    if data["truncated"]:
        st.info("Showing only the first 200 chunks; totals include all chunks.")
    table=pd.DataFrame([{
        "Chunk ID":row["chunk_id"],
        "Index":row.get("chunk_index"),
        "Ready": "Embedded" if row["embedded"] else "Missing",
        "Processing note":row.get("note") or "",
        "Last source update":row.get("updated_at"),
    } for row in data["rows"]])
    st.dataframe(table,use_container_width=True,hide_index=True)
    st.caption(
        "This page intentionally does not display source text or embedding vectors. "
        "Last source update may not equal embedding generation time."
    )


def operational_view(snapshot,job_limit):
    st.markdown('<div class="leadership-kicker">Engineering operations</div>',
                unsafe_allow_html=True)
    st.title("Workers, jobs and exceptions")
    st.caption(
        f"{snapshot['database']} · Snapshot {snapshot['at'].strftime('%Y-%m-%d %H:%M:%S UTC')}. "
        "These are task records, not document counts."
    )
    jobs=snapshot["jobs"]
    if not jobs:
        st.info("No embedding jobs found.")
        return
    count=snapshot["counts"]
    active=snapshot["active"]
    grid=[]
    for j in jobs:
        key=j["job_key"]
        summary=summarize_job(j,count.get(key,{}),
                              active.get(key,{}).get("active_claims",0))
        grid.append({
            "Job":key[:14]+"…",
            "Source":(f"{j['source_schema']}.{j['source_table']}"
                      if j.get("source_schema") and j.get("source_table")
                      else "Unlabeled legacy job"),
            "Type":(f"Pilot ≤{int(j['max_chunks']):,}" if j.get("max_chunks")
                    else "Full / unknown"),
            "State":summary["phase"],
            "Discovered":summary["total"],
            "Success":summary["SUCCESS"],
            "Failed":summary["FAILED"],
            "Queued":summary["PENDING"],
            "Running":summary["RUNNING"],
            "Active claims":summary["active_claims"],
            "Created UTC":j["created_at"],
        })
    st.dataframe(pd.DataFrame(grid),hide_index=True,use_container_width=True)
    st.caption("Job discovery counts can overlap between historical pilots and full runs.")

    ordered=sorted(jobs,key=lambda j:(
        bool(active.get(j["job_key"],{}).get("active_claims",0)),
        j.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)),reverse=True)
    chosen=st.selectbox("Inspect one job",options=[j["job_key"] for j in ordered],
                        format_func=lambda k:job_label(next(j for j in ordered if j["job_key"]==k)))
    job=next(j for j in jobs if j["job_key"]==chosen)
    info=summarize_job(job,count.get(chosen,{}),
                       active.get(chosen,{}).get("active_claims",0))
    st.code(chosen,language=None)
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Successful tasks",f"{info['SUCCESS']:,}")
    c2.metric("Pending tasks",f"{info['PENDING']:,}")
    c3.metric("Failed tasks",f"{info['FAILED']:,}")
    c4.metric("Active claimed rows",f"{info['active_claims']:,}")
    st.progress(info["progress"],
                text=f"{info['progress']:.1%} of discovered tasks have terminal outcomes")
    if not job.get("discovery_done"):
        st.caption("Discovery is still running; percentage can decrease as more rows are discovered.")
    trend=remember_sample(chosen,info["SUCCESS"],snapshot["at"])
    rate=observed_rate(trend)
    left,right=st.columns([1,2])
    with left:
        st.metric("Recent successes/min",f"{rate:,.1f}" if rate is not None else "Collecting")
        st.write("**Original job creation:**",job["created_at"])
        st.write("**Last worker startup:**",
                 job.get("last_started_at") or "Not recorded")
        st.write("**Current phase:**",info["phase"])
    with right:
        if len(trend)>1:
            chart=pd.DataFrame({
                "UTC time":[datetime.fromtimestamp(t,timezone.utc) for t,_ in trend],
                "Successful chunks":[v for _,v in trend],
            }).set_index("UTC time")
            st.line_chart(chart,height=190)
        else:
            st.info("Throughput history appears after the next dashboard refresh.")

    claims=[item for item in snapshot.get("worker_claims",[]) if item["job_key"]==chosen]
    with st.expander(f"Worker ownership · {len(claims)} active processes"):
        if claims:
            st.dataframe(pd.DataFrame([{
                "Worker / host":c["owner"] or "Unlabeled",
                "Rows claimed":int(c["active_claims"]),
                "Lease expires":c["latest_lease"],
            } for c in claims]),hide_index=True,use_container_width=True)
        else:
            st.caption("No unexpired leases right now.")

    st.subheader("Errors and recovery")
    limit=st.selectbox("Recent failed tasks",[20,50,100,200],index=1)
    show_detail=st.checkbox("Show detailed exception text",value=False)
    failures=read_failed_rows(chosen,limit) if info["FAILED"] else {"rows":[],"reasons":[]}
    if failures["reasons"]:
        st.dataframe(pd.DataFrame(failures["reasons"]).rename(
            columns={"reason":"Failure reason","amount":"Chunks"}),
            hide_index=True,use_container_width=True)
    else:
        st.success("No current FAILED tasks in this job.")
    if failures["rows"]:
        cols=["row_id","attempts","error_code","processing_comment","updated_at"]
        if show_detail:
            cols.insert(3,"error_message")
        error_rows=[{k:r[k] for k in cols} for r in failures["rows"]]
        st.dataframe(pd.DataFrame(error_rows),hide_index=True,use_container_width=True)
        st.download_button("Download displayed failures",safe_csv(error_rows),
                           file_name=f"embedding_failures_{chosen[:12]}.csv",
                           mime="text/csv")
    if st.checkbox("Show previous failure events",value=False):
        events=fetch_failure_history(chosen,limit=50)
        if events:
            cols=["row_id","error_code","processing_comment","occurred_at"]
            if show_detail:
                cols.insert(2,"error_message")
            st.dataframe(pd.DataFrame(events)[cols],hide_index=True,use_container_width=True)
        else:
            st.info("No historical errors found.")

    with st.expander("Shared deployment quota caps"):
        quotas=snapshot.get("quotas") or []
        if quotas:
            st.dataframe(pd.DataFrame([{
                "Deployment key":q["deployment_key"][:14]+"…",
                "RPM cap":q["rpm"],"TPM cap":q["tpm"],
                "Adaptive multiplier":round(float(q["multiplier"] or 1),3),
                "Cooldown":q["cooldown"],
            } for q in quotas]),hide_index=True,use_container_width=True)
            st.caption("Database-shared configured caps, not guaranteed Azure entitlements.")
        else:
            st.info("No quota records reported.")


def main():
    apply_theme()
    datasets=datasets_from_profiles()
    with st.sidebar:
        st.markdown("### Embedding Intelligence")
        page=st.radio("View",[
            "Executive overview","Document explorer","Engineering operations"
        ],label_visibility="collapsed")
        selected=None
        if datasets:
            selected=st.selectbox(
                "Source dataset",
                options=range(len(datasets)),
                format_func=lambda i:f"{datasets[i]['schema']}.{datasets[i]['table']}",
            )
        refresh=st.toggle("Auto-refresh (60 seconds)",value=True)
        job_limit=st.select_slider("Job history depth",options=[5,10,20,30],value=10)
        if st.button("Refresh data",use_container_width=True):
            read_snapshot.clear()
            read_document_rollup.clear()
            read_document_detail.clear()
            read_failed_rows.clear()
        st.divider()
        st.caption(
            "Read-only monitoring. Document metrics use source-table vectors; "
            "the operations page uses embedding_runtime. "
            "Use an SSH tunnel or authenticated internal proxy."
        )

    if page!="Engineering operations" and not datasets:
        st.warning("No existing-table embedding profiles found under profiles/.")
        return

    def render():
        snapshot=read_snapshot(job_limit)
        if page=="Executive overview":
            executive_view(datasets[selected],snapshot)
        elif page=="Document explorer":
            document_explorer(datasets[selected])
        else:
            operational_view(snapshot,job_limit)

    try:
        if refresh and hasattr(st,"fragment"):
            st.fragment(run_every="60s")(render)()
        else:
            render()
    except psycopg2.errors.QueryCanceled:
        st.warning(
            "The document aggregate exceeded its 60-second database budget. "
            "Embedding workers are unaffected. Try again later or use "
            "pre-aggregated document metrics for continuous executive reporting."
        )
    except (psycopg2.Error,ValueError) as exc:
        st.error(f"Dashboard query unavailable ({type(exc).__name__}).")
        st.caption(
            "Check that the selected profile has a doc_id column and the dashboard "
            "user has SELECT permissions on its source table and embedding_runtime. "
            "Do not expose database credentials in the UI."
        )


if __name__=="__main__":
    main()
