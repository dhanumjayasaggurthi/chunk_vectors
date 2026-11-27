# chunk_vectors — production quick start

One entry point: `production.py`. One local `config.ini`. No Docker.
Use the existing legacy `main.py` and embedding launchers only for legacy jobs.

> **Release gate:** This feature branch is still under validation. Native capture
> succeeded on the local 14-document workload; full OCR/visual enrichment did not.
> Do not run the full workload on the branch HEAD until the release gates in
> [project state](docs/PIPELINE_PROJECT_STATE.md) pass. Optional text enrichment is experimental and requires its own qualification.

## 1. Configure once

Activate your existing virtual environment and run from the repository root:

~~~bash
python -m pip install -r requirements.txt
python -m pip check
python -c "import pymupdf; print(pymupdf.VersionBind)"
~~~

For a new install, copy [config.ini.example](config.ini.example) to `config.ini`.
For an existing install, **merge only missing settings**. Preserve credentials,
approved policies, and existing table names. Never commit secrets.

| File / section | Configure |
| --- | --- |
| `config.ini` — POSTGRES | Connection, schema, native_table, chunk_table |
| PATHS / DEPLOYMENT | Input folder, receipt directory, profile references |
| RETENTION | Approved evidence directory, renderer version, retention permissions |
| GCP / Azure | Approved credentials, models and dimensions (even if embeddings disabled) |
| [Deployment YAML](profiles/deployment_policy.example.yml) | Extraction, metadata, table continuity, chunking, summaries and quotas |
| [Batch profile](profiles/retention_batch_example.json) | Document workers and maximum documents per run |

Windows: use `profiles/retention_batch_example.json`. Linux: use the
adaptive Linux profile if resource checks are supported. Start with a bounded
pilot; ensure the maximum-document cap covers the assigned folder for a full run.
Repeated capped runs revisit the same source prefix. Keep server source
assignments disjoint. Use the exact approved renderer version throughout.

## Size the assigned workload (read-only)

~~~bash
python production.py census
~~~

Reads PDF page counts and file sizes from the assigned source root or
configured inventory. Counts DOCX separately, reports unreadable PDFs,
and uses existing batch worker settings. Does not extract page contents,
write a database, or call providers. Counts are for the assigned shard;
unique/disjoint inventory entries are the upstream responsibility.
For 10-day sizing, use actual PDF pages divided by 864000 seconds and
include headroom, DOCX workload, provider quotas and storage constraints.
Do not infer content quality or duplicate-document detection from this census.

## 2. Initialize a NEW destination

~~~bash
python production.py preflight --before-init
python production.py init --apply
python production.py preflight
~~~

DBA must install pgvector first. Preflight is read-only. Initialization creates
the configured existing table families (OCR queue only when authorized).
Already initialized? Skip init; use preflight. Never truncate tables to retry.

## 3. Run ingestion

Use the following **only after release and provider approval**.

Native capture only (no cloud inference):

~~~bash
python -u production.py capture --apply
~~~

This produces a receipt path in the final JSON. Save that exact path.
Resume capture with the same command and unchanged capture settings.

Complete a successful capture without extracting the PDFs again:

~~~bash
python -u production.py finish --receipt "PATH_FROM_CAPTURE_OUTPUT.jsonl" --apply
~~~

Replace the placeholder with the actual receipt path inside the configured
receipt directory. For one document, add `--doc-id FULL_64_CHARACTER_ID`.
Successful OCR and visual results can be reused; an interrupted provider
response not yet committed can require a repeated call. Required OCR,
translation and selected visual LLM processing must be authorized.
For translation throughput, [DEPLOYMENT] translation_workers defaults to 1;
increase within the approved per-coordinator translation_rpm and character
quota. All workers share request pacing; allocate quota across servers. In the
current release, a selected visual failure blocks that document's publication.
Native evidence and earlier publications remain intact.

New input, one command (capture + finish):

~~~bash
python -u production.py run --apply
~~~

Use this only after the staged path is qualified. Keep automatic embeddings
off until retrieval quality is accepted. The optional future metadata/summary
backfill is not part of this command yet.

## 4. Monitor and recover

~~~bash
python production.py status
python production.py ocr-status
~~~

`status` reports native/retrieval manifest counts and OCR statuses for the
configured current native/OCR versions. A null section means its manifest or
queue has not been initialized. Retrieval counts are stored publication counts,
not a guarantee of current source freshness or full visual coverage.
If `profile_matches` is false, stop: the configured retrieval profile differs
from existing rows. Do not overwrite the table or blindly change the YAML.

Use the failed document ID from the log for a targeted retry:

~~~bash
python -u production.py finish --receipt "PATH_FROM_CAPTURE_OUTPUT.jsonl" --doc-id FULL_64_CHARACTER_ID --apply
~~~

For the specific retained-page boundary error, preview then explicitly requeue
only locally verified crops (no cloud requests):

~~~bash
python production.py ocr-recover-crops --doc-id FULL_64_CHARACTER_ID
python production.py ocr-recover-crops --doc-id FULL_64_CHARACTER_ID --apply
~~~

If any crop cannot be verified, none in that selected batch are requeued.
After review and provider approval, normal scoped ocr-run processes the
requeued jobs. Fix other failure causes before retrying. Do not reset
successful jobs or retry terminal failures indiscriminately. Logs are named after the configured chunk table:
`logs/<chunk_table>.log`, JSONL and per-process worker files.

For visual model diagnostics, use the existing selected-page inventory and
a single-page probe. Neither command re-extracts PDFs or publishes chunks.

~~~bash
python production.py visual-plan
python production.py visual-probe --doc-id FULL_64_CHARACTER_ID
python production.py visual-probe --doc-id FULL_64_CHARACTER_ID --apply
~~~

The first two are read-only. The third requires approved Azure configuration,
makes at most one provider attempt and saves a reusable normal visual cache
result if validated. Add --page N to choose a specific selected physical page.
A successful probe is not semantic certification. Do not run full finish
until its failure reason and release gates are resolved.

Linux background execution (only on a qualified release):

~~~bash
nohup python -u production.py run --apply > ingestlogs.log 2>&1 </dev/null &
echo $! > ingest.pid
tail -f ingestlogs.log
~~~

Run one coordinator per server. See [production reference](docs/PRODUCTION_REFERENCE.md)
for OS resource protection and advanced diagnostics.

## Real-corpus qualification (public test PDFs only)

~~~bash
python tests/online_public_regulatory_benchmark.py --documents 100 --min-pages 100 --max-pages 0 --workers 4 --process-pool --production-tables --batch-size 16 --report public_corpus_report.json
~~~

Full distinct originals, no cloud inference or PostgreSQL writes. The report
separates download time from extraction wall time, successful native
pages/sec, complete documents/min, latency percentiles, memory and table
coverage indicators. It fails if fewer than 100 originals are available or
any document is incomplete or has a page error. This is native-only,
not a 10-day SLA or semantic-accuracy certification. The same command runs
in the public benchmark GitHub workflow; source download failures are
reported, not replaced by duplicate copies or sampled pages.

## Worker tuning

Use independent limits by feature, not one worker count for everything.

| Stage | Existing setting |
| --- | --- |
| Native text + tables | Batch JSON: workers |
| Concurrent documents during finish | INI DEPLOYMENT: finish_workers (1..8, default 1) |
| OCR network | YAML deferred_ocr.workers |
| OCR retained-page readers | INI DEPLOYMENT: ocr_read_workers |
| Visual network and readers | Visual execution JSON: workers, read_workers |
| Translation | INI DEPLOYMENT: translation_workers, translation_rpm |
| Standalone materialization | INI DEPLOYMENT: materialize_workers |
| Embeddings | INI DEPLOYMENT: embedding_workers |
| Optional LLM enrichment | One-document pilot; no independent worker pool yet |

For qualification, start with finish_workers = 3. Different documents can
overlap OCR, visual interpretation and translation/publication. Each feature
keeps one coordinator, so nested pools do not multiply by document count.
Native overlap reserves both reader process budgets when finish_workers > 1.
With the PostgreSQL pool enabled, parallel finish requires at least OCR
workers + 3 connections in the coordinator process. This is not a fleet
connection budget: include native worker processes and all other clients.
A larger finish window holds more document state in memory. Keep concurrency
within measured CPU, RAM, DB capacity and provider quotas; do not run several
coordinators against the same quota allocation. Settings are operational
and do not change source or retrieval identities. No quality gate is bypassed.

## 5. Optional enrichment — independent pilot

For an already-published PDF, merge [ENRICHMENT] from config.ini.example.
Enable metadata and/or table_summaries explicitly. Use an approved Azure
structured-response deployment and exclusive quota allocation.

~~~bash
python production.py enrich-preview --doc-id FULL_64_CHARACTER_ID
python production.py enrich --doc-id FULL_64_CHARACTER_ID --apply
~~~

Preview is read-only. Apply performs bounded paid requests after qualification.
Exact-quote title candidates and qualitative table descriptions go into
chunk_metadata.optional_enrichment, labeled REVIEW_REQUIRED. Source text,
canonical metadata, row counts and vectors are unchanged. Mixed/oversized
groups are deferred. Identical committed requests skip on replay.
Do not run concurrently with another visual coordinator on the same quota.
Chart backfill, full introduction and corpus scheduling are still pending.
JSONB descriptions are not automatically embedded.

## 6. Embeddings — separate and optional

After retrieval quality and provider quotas are approved:

~~~bash
python production.py embed-preview
python -u production.py embed --apply
~~~

`embed-preview` is read-only. Set the actual embedding dimensions and limits
before running; `embedding_max_chunks=0` means the full configured target.
Do not change shared quota settings while workers run. Reuse existing vectors.
See [embedding operations](docs/FAST_EMBEDDINGS.md) for legacy/distributed details.

## What is configurable?

| Feature | Where / status |
| --- | --- |
| Document metadata rules | YAML `metadata.document`; per-field enabled/required, scope and regex |
| Table continuity | YAML `chunking.table_continuation`; conservative, source rows retained |
| Table summaries | YAML `chunking.table_summary_mode`; none/structural/extractive; not narrative |
| OCR / translation / visual LLM / embedding | INI DEPLOYMENT authorization flags and existing provider profiles |
| Optional LLM title / table summaries | Experimental INI ENRICHMENT; one-document preview/apply |
| Chart backfill and complete introduction | Pending |

Changing metadata or chunking policy can change the retrieval profile identity.
Preserve the policy used by existing tables until an approved migration/backfill
exists. Never treat model observations or a successful OCR request as proof of
correct source facts. Keep native text and original evidence; do not purge originals
based on local capture alone.

[Release status and handoff](docs/PIPELINE_PROJECT_STATE.md) ·
[Full production reference](docs/PRODUCTION_REFERENCE.md) ·
[SmartHub / metadata](docs/LINUX_FLEET_SMARTHUB.md) ·
[Legacy README](README_LEGACY.md)
