# chunk_vectors — production quick start

One entry point: `production.py`. One local `config.ini`. No Docker.
Use the existing legacy `main.py` and embedding launchers only for legacy jobs.

> **Release gate:** This feature branch is still under validation. Native capture
> succeeded on the local 14-document workload; full OCR/visual enrichment did not.
> Do not run the full workload on the branch HEAD until the release gates in
> [project state](docs/PIPELINE_PROJECT_STATE.md) pass. The independent optional
> metadata/table/chart enrichment command is planned, not available yet.

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
translation and selected visual LLM processing must be authorized. In the
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

Fix the cause before retrying. Do not reset successful jobs or retry terminal
failures indiscriminately. Logs are named after the configured chunk table:
`logs/<chunk_table>.log`, JSONL and per-process worker files.

Linux background execution (only on a qualified release):

~~~bash
nohup python -u production.py run --apply > ingestlogs.log 2>&1 </dev/null &
echo $! > ingest.pid
tail -f ingestlogs.log
~~~

Run one coordinator per server. See [production reference](docs/PRODUCTION_REFERENCE.md)
for OS resource protection and advanced diagnostics.

## 5. Embeddings — separate and optional

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
| Optional LLM metadata, narrative table/chart summaries | Planned independent enrichment; not an executable command yet |

Changing metadata or chunking policy can change the retrieval profile identity.
Preserve the policy used by existing tables until an approved migration/backfill
exists. Never treat model observations or a successful OCR request as proof of
correct source facts. Keep native text and original evidence; do not purge originals
based on local capture alone.

[Release status and handoff](docs/PIPELINE_PROJECT_STATE.md) ·
[Full production reference](docs/PRODUCTION_REFERENCE.md) ·
[SmartHub / metadata](docs/LINUX_FLEET_SMARTHUB.md) ·
[Legacy README](README_LEGACY.md)
