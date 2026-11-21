# chunk_vectors

## Ingestion v2 foundation (experimental, no production writes)

A separate opt-in pilot now supports bounded PyMuPDF page-range extraction
using independent CPU processes and atomic gzip checkpoints, a
**configurable 600-target / 800-max token** layout chunker, explicit source
element/bbox lineage, optional evidence-backed typed document metadata,
and read-only Cloud Vision / Document AI project-quota inspection.

It does **not yet** replace the production extraction path or run GCP OCR,
upload artifacts to S3, coordinate workers across servers or write vectors.
The local pilot deliberately refuses to chunk documents whose OCR-candidate
pages have not been resolved.

```bash
# Read-only page count / task plan
./.venv/bin/python -m ingestion_v2.cli plan --pdf /path/to/sample.pdf

# Native extraction: safe first two page ranges
./.venv/bin/python -m ingestion_v2.cli extract \
  --pdf /path/to/sample.pdf \
  --output /tmp/ingestion-v2-pilot \
  --profile profiles/ingestion_v2_general.json

# Read the applied GCP Vision / Document AI quota values:
# With application credentials, from the existing repo directory:
./venv/bin/python ../rimdocs_ingestion_v2_pilot/scripts/check_gcp_quotas.py
```

Run v2 from a **separate Git worktree and virtualenv**; never switch the
production checkout while workers are active. See
[Ingestion v2 foundation runbook](docs/INGESTION_V2_FOUNDATION.md)
for the exact safe worktree and quota-inspection commands.
The experiments are on `feat/ingestion-v2-foundation`, based on
`feat/distributed-fast-embeddings`, and do not change `main.py`.

Existing PDF ingestion/RAG plus an opt-in **distributed, resumable PostgreSQL
embedding backfill**. Normal document ingestion remains the default when
`--embed-existing` is not specified.

## Live embedding dashboard (Streamlit)

A **read-only leadership and engineering UI** has three views:

- **Executive overview:** actual distinct `doc_id` documents, fully embedded
  documents (every chunk vector present), partially embedded/not-started
  documents, unresolved notes, chunk coverage, and highest-backlog documents.
- **Document explorer:** enter any indexed `doc_id` to inspect the document's
  total/embedded/missing chunk counts, source `chunk_total` consistency,
  individual chunk readiness and per-row processing notes.
- **Engineering operations:** distributed job status, active worker claims,
  failure reasons/history, recent in-browser throughput and RPM/TPM caps.

Leadership coverage is computed from the **source table across all runs**,
not from a single job's overlapping/pilot task ledger. The heavier document
aggregation is cached for **10 minutes** and has a 60-second DB timeout.
It reads metadata and vector NULL status, not `chunk_text` or vector values.

```bash
# Background dashboard on port 8501:
bash start_dashboard.sh
tail -f dashboard.log

# Alternatively, run interactively:
./venv/bin/python -m streamlit run embedding_dashboard.py \
  --server.address 127.0.0.1 --server.port 8501
```

For remote access, forward port 8501 securely over SSH; **do not expose
Streamlit directly on an unauthenticated public interface**.
This dashboard is read-only; it cannot pause/restart a running backfill.
See [dashboard deployment and monitoring](docs/EMBEDDING_DASHBOARD.md).

## Quick start: immbot approved embeddings

Use `profiles/immbot_approved_1536.json` for
`regulatory.palm_paw.doc_chunks_immbot_approved`:
`chunk_id` (unique chunk key), `chunk_text`, `chunk_vector`
(`vector(1536)`), and `embedding_processing_note`.

**Confirmed source snapshot and pilot results (November 2026):**

| Measurement | Observed |
| --- | ---: |
| Total source rows | 1,094,208 |
| NULL vectors before pilot | 770,268 |
| Existing populated vectors before pilot | 323,940 |
| Unique, valid `chunk_id` index | Yes |
| Approx. input tokens/chunk (8,012-row sample) | 990 average; 2,571 p95 |
| Gateway-reported embedding token limit | 1,000,000 TPM |
| Confirmed RPM allocation | **Unknown** |
| Pilot request caps | **30 RPM / 750,000 TPM** (conservative, not guaranteed entitlement) |
| Pilot settings | 4 workers, 128 texts, 24,000 tokens/batch |
| Pilot `--max-files 1000` result | **991 SUCCESS / 9 FAILED** |
| Pilot measurement window | **112.4 s; 529.2 successful rows/min** |
| Pilot failures | 9 `INPUT_TOO_LONG`; text exceeded model per-input token limit |

At the same throughput, the remaining ~769k NULL vectors would require
roughly **24 hours** before allowing for variability. This is an *extrapolation
from a short pilot*, not a completion SLA. The 1M-TPM header does not prove
any particular RPM quota; request-rate tuning still needs measurement.

### Linux

From the checked-out repository directory with an existing `config.ini`:

```bash
python -m pip install -r requirements.txt

# No vector writes; validate mapping and count eligible rows:
EMBED_MODE=dry-run ./start_embed.sh

# Safety default: <=1,000-row pilot only (uses the same pilot job if rerun):
./start_embed.sh
tail -f embedlogs.log

# Full backfill is deliberate; no --max-files cap:
EMBED_MODE=full EMBED_CONFIRM_FULL=YES ./start_embed.sh
```

The launcher uses its own directory by default (no hardcoded production host
path). It starts in the background, writes `embed.pid`/`embedlogs.log`, and
prevents a duplicate process **on the same host**. PostgreSQL leases coordinate
workers across different hosts.

### Windows (PowerShell)

```powershell
python -m pip install -r requirements.txt
.\start_embed.ps1 -Mode dry-run
.\start_embed.ps1 -Mode pilot -Workers 4
.\start_embed.ps1 -Mode full -ConfirmFull -Workers 4
```

### Configuration

**The profile is the canonical source for embedding settings.** The immbot
profile currently contains `rpm=30`, `tpm=900000`, `batch_size=128`,
`batch_tokens=48000`, `max_attempts=5`, `timeout_seconds=90`, and
`lease_seconds=300`. `start_embed.sh` uses 4 workers by default and honors
optional environment overrides:

| Linux environment | Profile/behavior |
| --- | --- |
| `EMBED_PROFILE` | Full path to another profile |
| `EMBED_WORKERS` | Worker threads; default `4` |
| `EMBED_MODE` | `dry-run`, `pilot` (default), `full` |
| `EMBED_MAX_FILES` | Pilot task cap; default `1000` |
| `EMBED_CONFIRM_FULL` | Must be `YES` with `EMBED_MODE=full` |
| `EMBED_RPM`, `EMBED_TPM` | Override `embedding.rpm`, `embedding.tpm` |
| `EMBED_BATCH_SIZE`, `EMBED_BATCH_TOKENS` | Override batch limits |
| `EMBED_MAX_ATTEMPTS` | Override API retries |
| `EMBED_TIMEOUT_SECONDS`, `EMBED_LEASE_SECONDS` | Override timeout/lease |
| `EMBED_RETRY_FAILED=1` | Requeue failed tasks (only after resolving failures) |
| `EMBED_RESCAN=1` | Rediscover additional/missing source vectors |

Example **only after checking live quota and coordinating workers**:

```bash
EMBED_MODE=full EMBED_CONFIRM_FULL=YES EMBED_WORKERS=8 \
EMBED_RPM=30 EMBED_TPM=900000 EMBED_BATCH_TOKENS=48000 ./start_embed.sh
```

Do **not** change deployment RPM/TPM on a live shared quota ledger: the engine
persists them in PostgreSQL and rejects inconsistent process settings.
Coordinate configuration changes when workers are stopped.

### Logs and progress

Linux `start_embed.sh` detaches the process with `nohup`. Check it
using `ps -fp "$(cat embed.pid)"`; watch output with
`tail -f embedlogs.log`. The centralized logger also writes
`logs/epod_pipeline.log`. New process starts print an
`embedding_started` record containing `job_key`, then one
`embedding_progress` record every **60 seconds** and a final summary.
These log enhancements do not affect an already-running worker; consult
the PostgreSQL `embedding_runtime.runs` (created_at) and
`embedding_runtime.tasks` (updated_at) tables immediately.

### Updated full-backfill tuning (after the original pilots)

The original pilot used 4 workers, 30 RPM / 750,000 TPM and 24k-token
batches. Subsequent Linux tuning used **8 workers**, **48,000 tokens/batch**,
and a manually raised shared DB quota of **900,000 TPM**. The observed
10-minute successful rate was **677.9 rows/min**, with `multiplier=1`
and no gateway cooldown at that snapshot.

The immbot profile now persists `embedding.tpm=900000` and
`embedding.batch_tokens=48000` to match the approved running quota.
**These settings are caps, not verified sustained provider allowances.**
The Linux launcher still defaults to four workers for safe pilots. Use
`EMBED_WORKERS=8` explicitly for the full-backfill configuration.
All workers using this Azure deployment and coordination database must
agree with the shared `embedding_runtime.quota` values. For existing
jobs, safely stop the process before restarting with new settings; the
same job key resumes committed work.

### Failures, recovery and progress

A bad row does not stop healthy rows. Failure reasons are stored in
`embedding_runtime.tasks`, with historical failure events in
`embedding_runtime.failure_events`; a concise note is also written to the
configured source note column. The completed pilot's nine oversized rows
remain NULL and should be **split upstream**, not retried unchanged or
silently truncated. Successful vectors are preserved when
`reuse_existing=true`. Fatal Azure authentication errors or database outages
stop the run rather than mislabel all rows as bad.

```sql
SELECT status, count(*) FROM embedding_runtime.tasks
GROUP BY status ORDER BY status;

SELECT row_id, error_code, error_message, processing_comment
FROM embedding_runtime.tasks WHERE status='FAILED'
ORDER BY updated_at DESC LIMIT 50;
```

For the exact commands, rate-limit probe, operational checks and rollout
limitations, see **[docs/FAST_EMBEDDINGS.md](docs/FAST_EMBEDDINGS.md)**.

## Legacy PDF ingestion (unchanged)

```bash
python main.py --doc-workers 4 --workers 4
python main.py --no-embed --max-files 100
```

## Optional processing profiles

```bash
python main.py --profile profiles/regulatory.json --max-files 100 --doc-workers 4
python main.py --profile profiles/regulatory.json --set chunking.max_tokens=600 --file /path/file.pdf
python main.py --profile profiles/extracted_content.json --rechunk-extracted --max-files 100
```

Profiled output remains separate from legacy RAG storage. For full details see
[docs/USER_GUIDE.md](docs/USER_GUIDE.md).

### HAQ 3072-dimensional questions

For `question_embedding vector(3072)`, use
`profiles/haq_question_3072.json` with an approved
`text-embedding-3-large` Azure deployment. Set that deployment's verified
RPM and TPM in the profile or with CLI overrides, and validate the table ID,
vector dimension and optional note column before running.

## Validation and deployment status

```bash
python -m pip install -r requirements.txt
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
```

The 1,000-row pilot demonstrated per-row isolation, but does not replace
extended load, mixed-model compatibility, quota, WAL, PostgreSQL
lock-contention, recovery and retrieval-quality checks. The changes remain
on draft PR #3 until rollout gates are satisfied. Do not commit `config.ini`.
