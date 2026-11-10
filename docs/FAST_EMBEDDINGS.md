# Distributed existing-table embeddings

Production backfill is isolated from PDF/OCR ingestion. Do not launch the old
single-thread runner. The new implementation is in `fast_embedding_runtime.py`,
called by `main.py --embed-existing`.

## Preconditions

- PostgreSQL 13+ with pgvector installed and a user permitted to create
  schema/tables/indexes in `embedding_runtime`, read/write target vectors
  (and optionally notes), and inspect `pg_catalog`.
- `config.ini` on each host, identical embedding endpoint, deployment,
  API version and shared quota settings. Keep API keys outside version control.
- The mapped row ID **must have a single-column unique index**. A document ID
  repeated across chunks cannot be used. This validation is enforced before
  any vector writes. Fix your profile's `existing_chunks.id_column` to a
  real unique chunk or question ID if needed.
- Target pgvector dimension must match `embedding.dimensions`.
- Use a stable, finalized model/deployment; do not mix embedding models in
  the same vector column.

## Linux

```bash
python -m pip install -r requirements.txt
EMBED_WORKERS=16 EMBED_BATCH_SIZE=128 \
EMBED_RPM=<approved_RPM> EMBED_TPM=<approved_TPM> ./start_embed.sh
```

Or run explicitly (foreground is preferable for a managed service):

```bash
python -u main.py --embed-existing --profile profiles/clinical_1536.json \
 --doc-workers 16 --set embedding.rpm=<approved_RPM> \
 --set embedding.tpm=<approved_TPM>
```

## Windows

```powershell
python -m pip install -r requirements.txt
.\start_embed.ps1 -Profile .\profiles\clinical_1536.json -Workers 16 `
 -BatchSize 128 -Rpm <approved_RPM> -Tpm <approved_TPM>
```

Both operating systems can run concurrently, sharing exactly one PostgreSQL
task ledger per target/profile and rate limiter per Azure deployment.

## Restart, failure handling and observability

Stopping a worker returns unfinished claims to PENDING; a killed process
automatically loses leases, and another process reclaims the work. Successful
vectors are never rewritten with `reuse_existing=true`.

```bash
# Preview eligible rows without changing the DB:
python main.py --embed-existing --profile profiles/clinical_1536.json --dry-run

# Explicitly retry rows previously recorded FAILED:
python main.py --embed-existing --profile profiles/clinical_1536.json \
 --doc-workers 16 --retry-embed-failures

# Discover new rows or recompute rows that have had vectors cleared:
python main.py --embed-existing --profile profiles/clinical_1536.json \
 --doc-workers 16 --rescan-embeddings
```

The durable ledger stores `status`, `attempts`, `error_code`,
`error_message`, `processing_comment`, `owner`, `claim_token`,
`lease_until` and `updated_at`. If a profile declares a text note column,
a concise error note is also stored on the original source row.

Inspect results with read-only SQL:

```sql
SELECT status, count(*) FROM embedding_runtime.tasks
WHERE job_key = '<job_key_returned_by_cli>' GROUP BY status;
SELECT row_id, error_code, error_message, processing_comment, attempts
FROM embedding_runtime.tasks
WHERE job_key = '<job_key_returned_by_cli>' AND status = 'FAILED'
ORDER BY updated_at DESC LIMIT 100;
```

Failures in one input generate a recorded FAILED task and do not block other
rows. Blank inputs and missing source rows get SKIPPED with comments. API
authentication/deployment errors halt all workers intentionally; they are
configuration failures, not input failures. DB unavailability similarly stops
workers to avoid hiding systemic outages.

For mixed 400 responses, the worker bisects the batch until the invalid row
is identified, preserving valid neighbors. Invalid vectors are rejected.
The API request may repeat after a crash, but the database update is fenced by
claim token and conditional on the source-text MD5.

## Tuning and capacity

The starting values are 128 texts/request, 24k input tokens/request and
16 concurrent worker threads per Linux host. Adjust from observed input
lengths, Azure quotas and database write time. Define **both** `embedding.rpm`
and `embedding.tpm` for deterministic cross-machine throttle. If both are
omitted, 429-driven shared cooldown still works but a high worker count can
initially hit the gateway limit. There is no guarantee of zero 429s.

All processes targeting the same deployment must agree on RPM and TPM.
The token bucket reserves one request and its estimated input tokens for
every attempt, including retries. A 429 adds shared cooldown and decreases
permitted rate; successful batches gradually recover rate.

The production read path remains the legacy RAG UI and is not modified here.
Do not bulk-reindex an HNSW index concurrently with heavy production reads
without a DB maintenance plan. Verify I/O, WAL, lock waits, target indexes,
active DB connections and remaining quotas before scaling beyond the initial
workers.

## Limitations / validation gates

- This branch has no access to your production PostgreSQL or Azure endpoint;
  only deterministic unit tests can run in CI. Conduct a real 10k–100k row
  pilot under approved quota and inspect throughput, row counts, exception
  taxonomy, pg_stat_activity, WAL and index maintenance before large backfill.
- Discovery enumerates row IDs into a PostgreSQL metadata table. For millions
  of rows plan metadata storage, autovacuum and DB capacity accordingly.
- A re-run with a different `--max-files` uses a different run key. For real
  production runs leave `--max-files` unset.
- Use `--rescan-embeddings` after inserting new source rows. Avoid rescan while
  active workers from the same run are processing.
- Bulk throughput is fundamentally limited by Azure account/deployment TPM and
  RPM, gateway policies, and PostgreSQL write bandwidth, not only worker count.
