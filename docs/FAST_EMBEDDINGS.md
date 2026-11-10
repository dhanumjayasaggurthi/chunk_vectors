# Distributed existing-table embeddings: operations and rollout

This workflow updates **existing PostgreSQL NULL vectors only** without
re-running PDF, OCR or chunk extraction. The implementation is
`fast_embedding_runtime.py`, invoked by `main.py --embed-existing`.
It uses a PostgreSQL work ledger, conditional batch writes, leases,
per-deployment throttle accounting, retries and per-row failure isolation.

**Current implementation branch:** `feat/distributed-fast-embeddings`,
draft PR #3. The existing PDF-ingestion and RAG paths are unchanged.

## Executive and engineering monitoring in Streamlit

The read-only dashboard has an **executive overview** with distinct document
counts from `doc_id`, fully/partially/not-embedded documents, total stored and
missing chunk vectors, and the highest-backlog documents (exportable CSV).
A **document explorer** supports exact `doc_id` lookups and chunk-level
readiness. The separate **engineering operations** view retains worker claims,
per-job throughput samples, failures/history and quota caps.

Unlike individual job task counts, leadership metrics come from the actual
source table and include previously embedded chunks. They require read
permissions on the source table and use a 10-minute cache plus a 60-second
query timeout. The source document rollup can be moderately expensive while
embedding writes are active; it never reads vector values or source text.

The dashboard does not start jobs or consume embedding API quota. New runner
starts record readable source-table/profile metadata; older jobs appear by
hashed job key.

```bash
./venv/bin/python -m streamlit run embedding_dashboard.py \
  --server.address 127.0.0.1 --server.port 8501
```

For remote viewing, use an SSH tunnel rather than exposing the port.
See [EMBEDDING_DASHBOARD.md](EMBEDDING_DASHBOARD.md) for secure access,
refresh behavior, permissions and troubleshooting.

## 1. Verified target and pilot measurements

Database: `regulatory`; target:
`palm_paw.doc_chunks_immbot_approved`.

| Source field | Mapping |
| --- | --- |
| Row key | `chunk_id` (text, unique + valid index confirmed) |
| Embedding input | `chunk_text` |
| Destination vector | `chunk_vector` (1536 dimensions confirmed) |
| Row note | `embedding_processing_note` |
| Profile | `profiles/immbot_approved_1536.json` |
| Existing vectors mode | `reuse_existing=true`; never overwrite non-NULL values |

**Source snapshot before pilot:** 1,094,208 rows; 323,940 filled vectors;
770,268 NULL vectors. `chunk_id` is non-NULL and distinct in every row.
The **valid UNIQUE B-tree index**
`palm_paw.uq_doc_chunks_immbot_approved_chunk_id` exists already.
**Do not create another index.**

Token-size sampling: 8,012 eligible rows; approx. 990 tokens/chunk average
and 2,571 tokens/chunk p95 (chars/4 estimate, not tokenizer-exact).

**Completed 1,000-row pilot (Windows, 4 workers):**

| Metric | Observation |
| --- | ---: |
| Successful | 991 (99.1%) |
| Failed | 9 (0.9%) |
| Failed reason | `INPUT_TOO_LONG` |
| Elapsed completion window | 112.4 seconds |
| Successful rows/min | 529.2 |
| Workers | 4 |
| Batch cap | 128 inputs, 24,000 aggregate input tokens/request |
| API throttling cap | 30 RPM, 750,000 TPM |

Long inputs in the failed sample exceeded the individual-model limit
(8,827–22,802 tokens). They remain NULL, with failure reason and comment
recorded. Retry will not solve these until text is appropriately rechunked.

At the short-pilot throughput, finishing the roughly 769,277 remaining
NULL-vector rows would require **about 24.2 hours**. Actual time may
differ substantially: input-size distribution, API pressure, index/WAL load,
network and different-host runtime can change throughput. The measured
529.2 rows/min figure is not a guaranteed sustained rate.

**Check embedding-model compatibility before expanding:** all 1536-dimensional
vectors need not be from the same model. Mixing models in a serving column
can impair similarity search even when dimensions match.

## 2. Azure quota interpretation

A small synthetic request returned **HTTP 200** with
`x-ratelimit-limit-tokens: 1000000` and no reported requests limit.
Thus **1,000,000 TPM is an observed gateway header**; actual sustainable
TPM and RPM can also be constrained by shared enterprise clients and
gateway policies. **Actual RPM has not been confirmed.**

The original pilot settings were **30 RPM and 750,000 TPM**: intentionally
cautious historical caps, *not* verified quota grants. The current shared
database cap is **900,000 TPM**; new processes must match it.

Read endpoint limits without touching source rows:

```bash
python probe_embedding_quota.py --profile profiles/immbot_approved_1536.json
```

Only one small synthetic request is sent. It does not print credentials or
vector values. If headers don't expose RPM, do not infer an exact maximum.
Use the Azure deployment allocation/enterprise gateway documentation and
a bounded load test before raising throughput.

**Important:** `embedding.rpm` and `embedding.tpm` are also persisted in
`embedding_runtime.quota` per deployment. Every worker/host sharing that
deployment must agree; conflicting settings are rejected. To change them
after initialization, coordinate a maintenance window for all workers
and update the configured quota consistently. Do not simply launch
concurrent workers with different limits.

## Current full-run settings (after performance tuning)

The pilot measurements above are historical. After running the Linux
backfill, the shared quota was raised from **750,000 to 900,000 TPM**
with RPM unchanged at **30**, adaptive multiplier `1.0`, and no
cooldown at the observed snapshot. Eight workers with 48,000-token
batches achieved **677.9 successful rows/min** in one 10-minute window.
This is a measured observation, not a 12-hour SLA.

The latest immbot profile defaults now match the 900,000-TPM database
quota and 48,000-token batch setting. Keep `EMBED_WORKERS=8` explicit
for this tuned full run. A process launched using the old 750,000-TPM
profile will fail startup if the shared database quota has already been
changed to 900,000 TPM; use CLI overrides or pull the updated profile
before restarting. Never reinitialize the quota table to change caps.

```bash
# After stopping the original Linux worker and verifying it has exited:
EMBED_MODE=full EMBED_CONFIRM_FULL=YES EMBED_WORKERS=8 \
EMBED_RPM=30 EMBED_TPM=900000 EMBED_BATCH_TOKENS=48000 \
bash start_embed.sh
```

The changed throttle and batch size do not change the job identity,
so the same full-run ledger is resumed. Do not enable failure retries
or source rescan as part of a normal restart.

## 3. Prerequisites

- PostgreSQL with pgvector and enough WAL/disk space for the vector writes,
  metadata ledger and existing vector indexes.
- DB user permitted to create the `embedding_runtime` schema/tables/indexes
  and read the source table, update the target vectors and optional notes.
- Real unique single-column index on the configured `id_column`
  (confirmed for immbot only).
- Existing `config.ini` on every host with `[POSTGRES]` pointing to
  **regulatory**, and working `[AZURE_OPENAI_EMBEDDING]` credentials.
  Never commit the config or print the API key.
- Approved writes for the database, verified embedding model identity,
  and an operational window mindful of search/HNSW index overhead.

## 4. Install and execute on Linux

Use your actual checkout directory. The shell launcher automatically
resolves its own directory, unlike the earlier hardcoded production path.

```bash
cd /home/sa-prd-rdi-react-dev/rimdocs_s3_pg
git fetch origin
git switch feat/distributed-fast-embeddings
git pull --ff-only
./venv/bin/python -m pip install -r requirements.txt

# Metadata validation / count; do not write vectors:
EMBED_MODE=dry-run ./start_embed.sh

# Safety default; 1,000-row pilot, 4 workers. Already completed on this dataset:
./start_embed.sh
tail -f embedlogs.log

# Intentional uncapped backfill (requires operational approval):
EMBED_MODE=full EMBED_CONFIRM_FULL=YES ./start_embed.sh
tail -f embedlogs.log
```

The script returns after starting a background process; it records
`embed.pid` and writes output to `embedlogs.log`.
Check `ps -fp "$(cat embed.pid)"` and the log. A fast exit may be
completion *or* a startup error; the log is authoritative.
Use systemd for production service supervision.

The same capped pilot can be invoked directly in the foreground:

```bash
./venv/bin/python -u main.py --embed-existing \
  --profile profiles/immbot_approved_1536.json \
  --doc-workers 4 --max-files 1000 \
  --set embedding.batch_size=128 --set embedding.batch_tokens=48000 \
  --set embedding.rpm=30 --set embedding.tpm=900000
```

**Pilot rerun note:** `--max-files 1000` is part of the durable job key. A
second identical run observes that pilot job's completed records; it is
not automatically the next 1,000 source rows. For the full backlog,
remove `--max-files` (or use `EMBED_MODE=full`).

## 5. Windows (PowerShell)

```powershell
python -m pip install -r requirements.txt
.\start_embed.ps1 -Mode dry-run
.\start_embed.ps1 -Mode pilot -Workers 4
.\start_embed.ps1 -Mode full -ConfirmFull -Workers 4
```

Windows runs in the foreground, so the terminal stays attached to the
embedding process. Linux and Windows workers can cooperate against the
same PostgreSQL task and quota ledgers.

## 6. Configuration reference

The **canonical embedding defaults** are in
`profiles/immbot_approved_1536.json`. The launcher does not
maintain a second independent quota configuration.

| Profile property | Current value | Meaning |
| --- | ---: | --- |
| `embedding.rpm` | 30 | Conservative deployment-wide requests/min |
| `embedding.tpm` | 900000 | Tuned shared deployment cap; below 1M gateway header |
| `embedding.batch_size` | 128 | Maximum inputs/request |
| `embedding.batch_tokens` | 48000 | Tuned input token cap across the request |
| `embedding.max_attempts` | 5 | Maximum HTTP attempts per batch |
| `embedding.timeout_seconds` | 90 | API read timeout |
| `embedding.lease_seconds` | 300 | Database claim lease |
| `embedding.dimensions` | 1536 | Vector size; cannot change without schema/model match |
| `embedding.reuse_existing` | true | Avoid rewriting successful embeddings |

Linux launcher env overrides:
`EMBED_PROFILE`, `EMBED_WORKERS` (default **4**),
`EMBED_MODE` (`pilot` by default),
`EMBED_MAX_FILES` (default **1000**, pilot only),
`EMBED_CONFIRM_FULL` (must be `YES` for full),
`EMBED_RPM`, `EMBED_TPM`, `EMBED_BATCH_SIZE`,
`EMBED_BATCH_TOKENS`, `EMBED_MAX_ATTEMPTS`,
`EMBED_TIMEOUT_SECONDS`, `EMBED_LEASE_SECONDS`,
`EMBED_RETRY_FAILED=1` and `EMBED_RESCAN=1`.
Optionally `BACK_DIR`, `PY`, `EMBED_PIDFILE`, `EMBED_LOGFILE`.

Examples, when deployment quota changes are coordinated:

```bash
# Different profile, still a capped pilot:
EMBED_PROFILE=profiles/haq_question_1536.json \
EMBED_RPM=<HAQ_APPROVED_RPM> EMBED_TPM=<HAQ_APPROVED_TPM> \
EMBED_MODE=pilot EMBED_MAX_FILES=1000 ./start_embed.sh

# The approved measured immbot profile with explicit full mode:
EMBED_MODE=full EMBED_CONFIRM_FULL=YES EMBED_WORKERS=4 ./start_embed.sh
```

## 7. Progress, failure history, retry and restart

**Linux background process and logs.** `start_embed.sh` launches the worker
with `nohup`, so it generally survives closing an SSH terminal; terminal
`tail -f` can be stopped with Ctrl+C without stopping embeddings. On
Linux, `embed.pid` holds the process ID and `embedlogs.log` receives
stdout/stderr. The central logger also writes `logs/epod_pipeline.log`
and warning/error records to `logs/epod_pipeline.jsonl`.

```bash
ps -fp "$(cat embed.pid)"
tail -f embedlogs.log
tail -f logs/epod_pipeline.log
```

**Progress logs added after the first 10k Linux pilot was started**:
newly started workers log `embedding_started` with the `job_key`,
profile target, worker count and quota, then one read-only
`embedding_progress` snapshot every 60 seconds, and
`embedding_finished` when complete. Progress includes queued/running,
successful/failed/skipped counts and recent successful rows/minute.
These messages are emitted to both the console and the configured file
logger. An already-running Python process does not gain these changes
automatically; do not restart an active backfill merely to update logging.

Query the persistent job ledger to distinguish the existing 1,000-row
pilot, 10,000-row Linux pilot and full run. `runs.created_at`
indicates the original job creation; `tasks.updated_at` indicates
the last task change. A resumed run reuses its original `job_key`
and `created_at`, so look at task activity too.

```sql
SELECT r.job_key, r.created_at, r.discovery_done,
       MAX(t.updated_at) AS last_task_update,
       COUNT(t.row_id) AS task_count,
       COUNT(*) FILTER (WHERE t.status='PENDING') AS pending,
       COUNT(*) FILTER (WHERE t.status='RUNNING') AS running,
       COUNT(*) FILTER (WHERE t.status='SUCCESS') AS success,
       COUNT(*) FILTER (WHERE t.status='FAILED') AS failed,
       COUNT(*) FILTER (WHERE t.status='SKIPPED') AS skipped
FROM embedding_runtime.runs r
LEFT JOIN embedding_runtime.tasks t ON t.job_key=r.job_key
GROUP BY r.job_key,r.created_at,r.discovery_done
ORDER BY r.created_at DESC;
```

```sql
-- Use job_key to distinguish the 1,000-row pilot from the full job.
SELECT job_key, status, COUNT(*) AS tasks
FROM embedding_runtime.tasks
GROUP BY job_key,status ORDER BY job_key,status;

-- Current failures and comments:
SELECT job_key,row_id,attempts,error_code,error_message,processing_comment,updated_at
FROM embedding_runtime.tasks WHERE status='FAILED'
ORDER BY updated_at DESC LIMIT 50;

-- Durable failure history, including failures later recovered:
SELECT job_key,row_id,error_code,error_message,processing_comment,occurred_at
FROM embedding_runtime.failure_events
ORDER BY occurred_at DESC LIMIT 50;
```

Tasks use `PENDING`, `RUNNING`, `SUCCESS`, `FAILED` and `SKIPPED`.
Workers lease tasks with `FOR UPDATE SKIP LOCKED`, heartbeat, and
verify an ownership token before each successful vector write.
Requests may repeat if a process crashes, but SQL persistence is fenced
against stale workers, preserving already-filled vectors.

Malformed inputs and empty source text record per-row outcomes.
Mixed HTTP 400 batches are bisected to find the invalid input.
HTTP 429 responses trigger shared cooldown and adaptive backoff.
An Azure authentication error or database outage stops the job,
because those are systemic faults rather than malformed rows.

```bash
# Requeue eligible FAILED tasks only after fixing their root cause:
./venv/bin/python -u main.py --embed-existing \
  --profile profiles/immbot_approved_1536.json \
  --doc-workers 4 --retry-embed-failures

# Rediscover sources inserted since last completion:
./venv/bin/python -u main.py --embed-existing \
  --profile profiles/immbot_approved_1536.json \
  --doc-workers 4 --rescan-embeddings
```

Those commands read the cap settings from the profile. The original nine
`INPUT_TOO_LONG` records should *not* be retried with unchanged text.
Long inputs need a reviewed chunk-splitting or aggregate-embedding design.

## 8. Why this can be slow, and what to measure

- Each 128-ID claim is batched and committed as a short transaction.
- A token budget of 48,000 with ~990 tokens per chunk yields **about 48
  inputs per full request**, rather than 128; this is an approximation.
- There are at least two task-ledger status changes per processed row
  (claim and completion), plus necessary vector writes, discovery and
  quota reservations. The ledger can generate significant WAL at million-row
  scale; metadata storage/autovacuum should be monitored.
- Failed rows also record error details and optional source-table notes.
- Each successful API request consumes shared RPM and TPM reservations.
  Quota-waiting workers **do not write** the quota row.
- More workers cannot overcome a saturated deployment quota. Observe
  API latency and 429s, PostgreSQL `pg_stat_activity`, transaction
  commit latency, WAL rate, index maintenance and row throughput.

Scale up cautiously: compare throughput for 4 workers, then 8,
while retaining the same cap first. Only raise RPM after measuring gateway
behavior and coordinating other users of the deployment. If PostgreSQL
becomes the limiter, consider range-lease coordination and sparse
per-row failure metadata rather than adding another queue service.

## 9. Rollout gates

The observed 1,000-row pilot verifies that a full test group can finish
despite nine failed inputs. It **does not** prove multi-day stability,
maximum sustainable RPM, or recovery across host restarts.

Before full production rollout: verify model consistency, gateway load,
DB storage/WAL/index headroom, failure logs, deployed feature-branch version,
read/query latency and a longer 10k–100k-row scale test. The work remains
under draft review until those gates are met.
