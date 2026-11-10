# Streamlit embedding operations dashboard

This application is a **read-only monitor** for existing PostgreSQL embedding
jobs. It does not start, cancel, retry, or modify any embedding run. It does
not read the source chunk table, text, vectors, or Azure credentials.

## What it shows

- Last 30 jobs in `embedding_runtime.runs`, ordered by creation date.
- Source schema/table, profile name, model and pilot cap for jobs launched
  by the latest runtime version.
- Existing pilot/full jobs started on older runtime versions still appear by
  job key, but may lack human-readable table/model metadata until restarted.
- Per-job discovered, success, failed, skipped, pending and running counts.
- Active *unexpired lease* count and newest lease expiration timestamp.
- Expandable per-worker active claims showing the worker UUID and, for
  newly started runtime processes, the server hostname.
- Progress of discovered tasks; not the current total number of NULL source
  vectors, especially while ingestion is adding more rows.
- Session-local success-throughput trend calculated between dashboard refreshes.
- Failed-row error codes and comments, optional detailed error messages,
  and bounded CSV download (max 200 rows).
- Optional historical failures and the shared database quota configuration.

Snapshots are cached for 30 seconds and refresh at most once per minute
per open browser session. The sidebar can restrict the view to the
5, 10, 20 or 30 latest jobs to reduce the cost of counting large ledgers. The job counts may scan the metadata ledger, so
monitoring many jobs still consumes some DB resources. Every read is
server-side bounded by a 12-second SQL statement timeout.

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
access to `embedding_runtime.runs`, `tasks`, `quota`, and
`failure_events`. A DBA can grant read-only access to the
`embedding_runtime` schema. Do not expose ingestion write credentials
to dashboard users.

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
- **High database load**: close unused dashboards, keep 60-second refreshes
  or disable auto-refresh. The monitor queries task counts; it deliberately
  does not add any DB-write counters or an additional queue.
- **Stale progress**: a job may be rate-limited, stopped, or have no new
  successful rows; compare claims and pending counts, then check
  `embedlogs.log` and `logs/epod_pipeline.log`.
- **No historical failures**: the optional
  `embedding_runtime.failure_events` table may not exist on earlier
  deployments.

See [FAST_EMBEDDINGS.md](FAST_EMBEDDINGS.md) for approved run settings
and [../README.md](../README.md) for the project overview.
