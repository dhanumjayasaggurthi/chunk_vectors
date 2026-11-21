# Ingestion v2 — fenced native-PDF work ledger (draft operational runbook)

**Scope:** Native PDF page ranges only. This is a separate experimental
execution path; legacy ingestion and embedding processes remain unchanged.
Do not enable it against production until DBA, security and representative
performance/quality reviews are complete.

The ledger uses PostgreSQL's short transactions and
`FOR UPDATE SKIP LOCKED` to avoid multiple workers owning the same
range. A randomly assigned claim token fences late or disconnected workers.
A worker can renew only its own unexpired lease, and completion is accepted
only with the same live token. PostgreSQL stores the error reason and
attempt number per failing range.

**No cloud OCR, no advanced tables, no vectorization, no production source
table changes or chunk publishing is implemented in this milestone.**

## What is implemented

| Capability | Status |
| --- | --- |
| Atomic registration of immutable source, document and page ranges | Implemented |
| Concurrent independent PostgreSQL range claiming | Implemented |
| Lease fencing, renewal and expired-lease recovery | Implemented |
| Nonblocking heartbeat and short SQL transactions | Implemented |
| Bounded retries and a permanent failure record | Implemented |
| Separate source document identity and source-content SHA-256 | Implemented |
| Atomic, checksum-addressed native extraction artifacts | Implemented on shared filesystem pilot |
| Linux/Windows-compatible Python worker | Implemented; source/mount mapping must be validated |
| Cross-host PDF/file access and artifact URI mapping | **Operator responsibility; not automatically verifiable** |
| Cloud Vision asynchronous OCR and quota-aware shared governor | Not implemented |
| Table/figure reconstruction and cross-page structural inference | Not implemented |
| Versioned chunk publication and semantic embedding routing | Not implemented |
| Production distributed soak/burn-in and multi-host failover validation | Not completed |

## Important deployment constraint

Do not run the distributed pilot unless **every participating host** can read
the exact same source document bytes, write artifacts to a storage location
available to downstream consumers, and agree on the same canonical
`--source-uri` and `--artifact-uri-prefix` strings.

For example, Linux might use `/epod_nas/docs/example.pdf`, Windows
might use `Z:\docs\example.pdf`, and both can register/read the
identity `nas://enterprise-archive/docs/example.pdf`. This is a
logical URI, **not a path that the script mounts for you**.

Only shared NAS/SMB filesystem mapping is supported for this pilot.
S3 object storage requires a separate, versioned object-storage adapter
before the runner can safely publish S3 artifacts.

## Run in a disposable PostgreSQL database first

Use a separate worktree and virtualenv, not the production checkout:

```bash
cd /home/sa-prd-rdi-react-dev/rimdocs_s3_pg
git fetch origin
git worktree add ../rimdocs_ingestion_v2_pilot origin/feat/ingestion-v2-foundation
cd ../rimdocs_ingestion_v2_pilot
python3 -m venv .venv
./.venv/bin/python -m pip install "pymupdf>=1.24,<2" "psycopg2-binary>=2.9,<3" "tiktoken>=0.7,<1" "PyYAML>=6.0.2,<7"
```

Set `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` for a **disposable
test database** using an authorized account. Supply the password through
your approved secure connection method (e.g. a temporary `PGPASSFILE`).
Never paste credentials into command output or a repository.

### 1. Create the new isolated schema

```bash
./.venv/bin/python -m ingestion_v2.distributed_cli init-ledger --apply
```

This applies only `ingestion_v2/sql/001_work_ledger.sql`, creating
`ingestion_v2_runtime.jobs`, `ranges`, `range_failures` and their
indexes. It does not modify the current `embedding_runtime` tables.

### 2. Register one representative, approved PDF

```bash
./.venv/bin/python -m ingestion_v2.distributed_cli register \
  --pdf /approved/mount/representative.pdf \
  --source-uri "nas://approved-archive/representative.pdf" \
  --profile profiles/ingestion_v2_general.json --confirm
```

The output contains `job_key`, source hash, page count and range count.
The command fingerprints the source PDF once before registration. If the
same `job_key` already exists with different immutable document metadata,
registration fails without replacing existing rows.

### 3. Run a bounded pilot on Linux

```bash
./.venv/bin/python -m ingestion_v2.distributed_cli worker \
  --job-key YOUR_JOB_KEY \
  --pdf /approved/mount/representative.pdf \
  --source-uri "nas://approved-archive/representative.pdf" \
  --output /approved/shared-mount/v2-artifacts \
  --artifact-uri-prefix "nas://approved-archive/v2-artifacts" \
  --worker-id "linux-pilot-1" --max-claims 2
```

The worker processes at most two ranges by default. It exits when no
range is currently claimable. For another host, use its own local path
and mount to the SAME underlying shared source/artifact storage, with
the identical canonical `--source-uri` and artifact URI prefix.

**Do not use `--unlimited`** until repeated bounded tests, simulated
worker failure, long-document memory measurements, and storage checks
pass. `--unlimited` is a one-shot backlog drain, not an always-on
service: it exits when no range is currently claimable, which may
occur while other workers are running or retries are delayed. Use a
supervisor and polling lifecycle only after operational review.

### 4. Inspect status

```bash
./.venv/bin/python -m ingestion_v2.distributed_cli status --job-key YOUR_JOB_KEY
```

Optional SQL (read-only):

```sql
SELECT job_key,status,COUNT(*) AS ranges, SUM(end_page-start_page) AS pages,
       MIN(updated_at) AS earliest_activity,MAX(updated_at) AS latest_activity
FROM ingestion_v2_runtime.ranges
WHERE job_key='YOUR_JOB_KEY'
GROUP BY job_key,status ORDER BY status;

SELECT start_page+1 AS page_from,end_page AS page_to,attempts,
       error_code,error_message,updated_at
FROM ingestion_v2_runtime.ranges
WHERE job_key='YOUR_JOB_KEY' AND status='FAILED'
ORDER BY start_page LIMIT 100;
```

## Failure semantics and data integrity

- A FAILED/PENDING range does not block unrelated ranges.
- Each claim is bounded by a lease. If a worker dies, another can reclaim
  an expired lease; attempts are counted, with a terminal `LEASE_EXHAUSTED`.
- A previous worker's claim token cannot complete or mark failure after
  another worker reclaims it.
- The work process keeps PostgreSQL transactions short; all PDF work runs
  in an independent process.
- Each worker writes a **token-specific** immutable staging filename so
  the old and new worker cannot overwrite each other's artifact.
- Success requires the worker to write the artifact first, calculate its
  SHA-256 checksum, and then commit a fenced SUCCESS record.
- If the process crashes after the file write but before DB completion,
  that uncommitted file is an **orphan**. Orphan cleanup and retention must
  be implemented and tested before production scale.
- An exact source SHA-256 and page count are checked at worker startup;
  native extraction also checks file size/mtime before and after its
  page range. For full production-grade source immutability, prefer an
  immutable NAS snapshot or versioned S3 object rather than a mutable path.
- The stored artifact URI is logical: a downstream consumer still needs
  a trusted mapping from the URI to the actual shared filesystem.
- The ledger is not a substitute for detecting unrecognized OCR, table,
  chart or diagram content. Publishing native-only chunks is still
  prohibited while OCR candidates remain unresolved.

## Release gates still open

1. Confirm PostgreSQL privilege segregation and migrations in a staging DB.
2. Multi-hour, multi-host failure injection, including one crashed host and
   a 25k-page PDF, with a validated shared source and artifact mount.
3. Benchmark page-range duration, file-open overhead, NAS throughput and
   memory/CPU for multiple process counts before increasing workers.
4. Validate OCR routing and GCP project-applied quotas before implementing
   the separate OCR worker pool.
5. Implement versioned artifacts in an approved storage backend, orphan
   retention/GC, document completeness checks, structural stitching,
   and atomic publication before calling the pipeline production-ready.
6. Add scheduled workers/supervision and observability as part of a
   separately reviewed deployment.

Reference: PostgreSQL SELECT row locking and SKIP LOCKED,
https://www.postgresql.org/docs/current/sql-select.html
