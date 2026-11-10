# Document Embedding Intelligence — Streamlit dashboard

The dashboard has been redesigned for **business leadership first** and
engineering operators second. It reads PostgreSQL only; it cannot start,
stop, cancel, retry, or update embeddings. There is no extra queue or
monitoring database.

## Three views

### Executive overview: document completion

Uses the actual source table (for immbot:
`regulatory.palm_paw.doc_chunks_immbot_approved`) to report:

- **Total documents:** distinct nonblank `doc_id` values.
- **Fully embedded documents:** every source chunk for that `doc_id`
  has a non-NULL `chunk_vector`; the document is fully ready.
- **Partially embedded:** at least one vector exists but other chunks
  in the same document still have NULL vectors.
- **Not started:** no source chunk in the document has a vector.
- **Needs attention:** documents with at least one still-missing vector
  whose `embedding_processing_note` is non-NULL. Not all such notes
  indicate a fatal error.
- **Chunk totals / stored vectors / missing chunks:** counts across the
  source table, including vectors created before this backfill.
- **Coverage percentages:** fully ready documents divided by all
  identifiable documents, and embedded chunks divided by all chunks.
- **Largest unfinished documents:** 25 documents ranked by missing
  chunks, with representative file name and completion percentage.

These figures intentionally differ from job SUCCESS/FAILED/PENDING counts:
a source row may already contain a vector from an earlier run, a job can
have failures while other documents are complete, and old job ledgers
may overlap. **Do not add job success counts to get document coverage.**

`doc_id` is treated as the document identity; `chunk_id` remains the unique
individual chunk key. Null/blank document IDs are excluded from document
counts but included in chunk totals, and flagged as unmapped.

### Document explorer: verification and audit

Choose one of the 25 largest unfinished documents or paste an exact
`doc_id`. The lookup uses the existing source `doc_id` B-tree index.
Inspect total chunks, stored vectors, missing vectors, unresolved notes,
`chunk_index`, `chunk_total` consistency and per-chunk readiness.
The UI returns at most 200 chunk rows per document; summary totals
include the full document.

The explorer never retrieves `chunk_text` or vector values, but document
IDs, file names and notes are still business metadata. Access control
remains mandatory.

### Engineering operations

Includes the existing job ledger, latest 5–30 job summaries, active
claim owner/hostname, worker lease state, recent success throughput,
failure breakdown and bounded CSV, optional historical errors and
configured deployment RPM/TPM. Older jobs lacking source metadata
remain visible by their job keys.

## Performance and refresh strategy

- The **task ledger** has a 30-second cache and refreshes at most every
  60 seconds while the dashboard is open.
- **Source document rollups** aggregate the entire selected source table
  by `doc_id`. These are deliberately cached for **10 minutes** across
  browser sessions and limited to **60 seconds** of PostgreSQL query time.
  For a million-row table this is heavier than reading job summaries;
  do not manually refresh repeatedly during a heavy backfill.
- The **document explorer** queries exactly one `doc_id` and returns
  no more than 200 rows. The result is cached briefly.
- No per-row success logs, extra queue or monitoring-write transactions
  are introduced.

If full-table document aggregation exceeds the 60-second budget,
the UI shows a clear timeout message rather than holding a query open.
For a permanent, high-frequency executive screen with many viewers,
the next scale step would be an incrementally maintained document
summary table or scheduled materialized view—not running a million-row
group-by every minute.

## Install

On your Linux embedding host (using the checked-out feature branch):

```bash
cd /home/sa-prd-rdi-react-dev/rimdocs_s3_pg
git fetch origin
git switch feat/distributed-fast-embeddings
git pull --ff-only
./venv/bin/python -m pip install -r requirements.txt
```

Run with the same local `config.ini` that defines your `[POSTGRES]`
connection. The dashboard uses only the `[POSTGRES]` section.

```bash
# Safe background launcher (localhost only, does not affect embeddings):
bash start_dashboard.sh
tail -f dashboard.log

# Or foreground:
./venv/bin/python -m streamlit run embedding_dashboard.py \
  --server.address 127.0.0.1 --server.port 8501
```

**Do not use `--server.address 0.0.0.0` on a network without authentication.**
Streamlit is not a production access-control system. Access the UI securely
through a localhost SSH tunnel (recommended for developer monitoring):

On your local Windows machine (PowerShell, in a second terminal):

```powershell
ssh -N -L 8501:127.0.0.1:8501 YOUR_LINUX_USER@YOUR_LINUX_HOST
```

Then open **http://localhost:8501** in your local browser.

The Streamlit app is **separate from** `start_embed.sh`.
`start_dashboard.sh` stores `dashboard.pid` and writes `dashboard.log`.
Stopping/restarting the dashboard does not stop the embedding worker.
The embedding worker can continue running in the background with `nohup`,
or be supervised independently by systemd.

### Windows-local dashboard

```powershell
.\venv\Scripts\python.exe -m streamlit run embedding_dashboard.py `
  --server.address 127.0.0.1 --server.port 8501
```

## Least privilege

Ideally configure a dedicated PostgreSQL login with **SELECT-only**
access to `embedding_runtime.runs`, `tasks`, `quota`,
`failure_events` **and SELECT on each monitored source chunk table**.
For immbot, grant read-only access to
`palm_paw.doc_chunks_immbot_approved`. The dashboard needs only
`doc_id`, `chunk_id`, `chunk_vector` NULL status,
`file_name`, `chunk_total`, `chunk_index`, `updated_at`
and `embedding_processing_note` for its source view.
It does not select source text or vector values. The recommended
PostgreSQL credentials cannot modify embedding data.

Instead of editing `config.ini`, override the monitoring database login
with environment variables:

| Environment | Purpose |
| --- | --- |
| `EMBED_DASHBOARD_DB_HOST` | Alternative PostgreSQL host |
| `EMBED_DASHBOARD_DB_PORT` | Alternative PostgreSQL port |
| `EMBED_DASHBOARD_DB_NAME` | Coordination database (usually `regulatory`) |
| `EMBED_DASHBOARD_DB_USER` | Dedicated SELECT-only DB user |
| `EMBED_DASHBOARD_DB_PASSWORD` | Read-only user's secret (keep out of logs) |

All connections explicitly set PostgreSQL `default_transaction_read_only=on`.
The dashboard never changes task state or configuration.

**Scope:** one PostgreSQL coordination database at a time. If jobs are
tracked in two different PostgreSQL databases, run a separate dashboard
instance or point the environment variables to the desired database.
Within the *same* coordination database, multiple servers, profiles,
and source tables are visible together.

## Interpreting status

- **Active claims**: at least one row has a RUNNING status and a future
  lease expiration. This does not prove the worker is progressing.
- **Discovering / awaiting worker**: discovery is unfinished, but there
  are no active claims. Verify worker process and logs if this persists.
- **Pending / no active lease**: some tasks remain without a recent claim.
  The worker may have stopped, be waiting on quota, or have failed.
- **Completed with failures**: no queued/running work remains, but
  failed tasks are stored for investigation and later retry.
- **Completed**: the known task ledger has terminal outcomes; newer
  source inserts may still require `--rescan-embeddings`.

For large jobs, the dashboard counts **task records already discovered**,
not the full source-table backlog. During an ongoing ingestion the final
percentage can change as more chunks become eligible.

The throughput chart uses the browser's snapshots. Its rate starts
displaying after two refreshes and is reset when the browser session ends.
It is not a durable SLA measurement or a substitute for Azure telemetry.

## Troubleshooting

- **Dashboard won't connect**: check the local `config.ini`, network,
  credentials, SSL policies, SELECT grants and 12-second statement timeout.
- **No jobs**: verify that the dashboard and workers use the *same*
  PostgreSQL database and that `embedding_runtime.runs` exists.
- **Worker shown as UUID without hostname**: claim-owner hostnames were added
  after the original deployment. The existing worker can continue without
  interruption; only future starts include host:UUID.
- **Unknown source table / profile for an old job**: those fields were
  introduced after the original job started. The hashed job key,
  creation date and task statuses are still available. New worker
  startup fills in metadata without changing the job key.
- **High database load**: close unused dashboards. Executive document
  totals are cached 10 minutes; the operations view refreshes every minute.
  Consider a read replica or maintained aggregate for many concurrent
  leadership viewers. No DB-write counters or extra queues are introduced.
- **Document view says source mapping unavailable**: the selected profile
  must refer to a source table with a text `doc_id` column, and the
  dashboard user must have SELECT permission on that table.
- **Document figures differ from job task counts**: this is intentional;
  the executive page counts all current source vectors, including old
  vectors, whereas job ledgers record particular embedding attempts.
- **Stale progress**: a job may be rate-limited, stopped, or have no new
  successful rows; compare claims and pending counts, then check
  `embedlogs.log` and `logs/epod_pipeline.log`.
- **No historical failures**: the optional
  `embedding_runtime.failure_events` table may not exist on earlier
  deployments.

See [FAST_EMBEDDINGS.md](FAST_EMBEDDINGS.md) for approved run settings
and [../README.md](../README.md) for the project overview.
