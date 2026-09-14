# MIR-AI Production Resilience, Resume, Logging, and Status

This document describes the operational behavior implemented for MIR-AI. It is intentionally factual: it does **not** call the pipeline “failproof.” The design goal is bounded retry, idempotent persistence, durable resume, explicit failure states, and enough evidence to diagnose the observed failure without inventing a root cause.

## Failure model

A document failure must not terminate the remaining batch when `batch_continue_on_error=true`. Transient dependencies are retried with bounded exponential backoff. Permanent errors (for example a dependency HTTP 400) are not repeatedly retried with an unchanged request.

PostgreSQL pooled connections that fail with transport/session errors such as `SSL error: unexpected eof while reading` are discarded rather than returned as healthy pool members. TCP keepalives and connection timeout are configurable. Critical idempotent writes are retried. Document-level retry then resumes from durable page state if a longer outage exceeds the DB-operation retry budget.

Generation claiming is idempotent for the same worker identity. A retry after an ambiguous claim/activation commit does not intentionally increment the attempt counter twice or create a second generation. The old ACTIVE generation is retained until the new generation activates.

## Resume boundaries

The durable sequence is:

```text
DISCOVERED
  -> PAGE_EXTRACTION              (pages persisted individually)
  -> PAGE_EXTRACTION_COMPLETE
  -> CHUNKING / EMBEDDING         (deterministic chunks; identical persisted embeddings reusable)
  -> METADATA or METADATA_SKIPPED
  -> ACTIVATION
  -> ACTIVE
```

`last_page_completed` advances only across contiguous persisted pages. After a process/DB/API failure, the PDF parser seeks to the next uncommitted page. Chunk construction is deterministic. If a matching chunk already has an embedding in the same generation, the embedding call can be skipped and the persisted vector is preserved.

## Batch reconciliation

`source_max_files=100` is a limit on resolved logical documents when control-table mode is off. PDF/DOCX representation resolution happens before ingestion. Use `--plan` before a qualification run to print the exact selected physical source, format, and selection reason.

In control-table mode, the limit applies to the authoritative logical IDs read from the control table. Missing/ambiguous/disabled representations are reported explicitly, so 100 requested control IDs can legitimately result in fewer than 100 selected source files. This is reported rather than hidden.

A per-document exception is converted to an ERROR result and the batch continues by default. `batch_exit_nonzero_on_error=true` still makes the process return a non-zero exit code after the run if error-status items exist.

## Logs

Three durable/operator surfaces are used:

- `logs/mir_ai.log` — rotating human-readable diagnostics.
- `logs/mir_ai.jsonl` — rotating machine-readable structured diagnostics.
- `logs/mir_ai_emergency.jsonl` — emergency fallback for ERROR/CRITICAL records if the asynchronous logging queue cannot accept the record.

The console defaults to the human-readable formatter. Set `log_console_json=true` for JSON console output.

Sensitive credential patterns are redacted. The pipeline must not log API keys, DB passwords, AWS secrets/session tokens, authorization headers, or full embedding/document request payloads.

## PostgreSQL observability

`--init-db` creates the core schema and the following operational objects:

- `mirai_ingestion_runs` — one row per batch execution.
- `mirai_ingestion_run_items` — one row per logical document/source-selection issue in that run.
- `mirai_ingestion_events` — significant lifecycle, retry, recovery, and failure events.
- `mirai_ingestion_status` — operational status view comparable to the original ingestion log while preserving the richer generation model.

The event table is intentionally **not** a copy of every DEBUG log line. It stores significant events such as run/document start, checkpoint, retry, dependency failure, document failure/activation, and run completion. This prevents logging from becoming the ingestion bottleneck.

If PostgreSQL itself is unavailable, PostgreSQL cannot persist an event describing its own outage at that instant. The console/rotating files remain the durable diagnostic surface, and DB event writes are best-effort once connectivity exists again. This is a physical limitation, not hidden by the implementation.

## Error evidence: where / what / why / resolution

Errors capture observed facts where available:

- component/service and operation;
- pipeline stage, logical object, document/generation, page/chunk when known;
- exception class and exact traceback location;
- structured category and dependency error code;
- HTTP status and APIM/Azure/S3 request ID when returned;
- retryable flag and attempt number;
- sanitized observed error text;
- resolution hint;
- `root_cause_status`.

`root_cause_status=unconfirmed` means the system does **not** know the root cause from the available evidence. A resolution hint uses language such as “verify endpoint/deployment/API version” rather than asserting that one of those is the cause. If a dependency returns a machine-readable error code, the code is preserved and the status can be marked dependency-reported.

## Preflight

With `preflight_enabled=true`, a batch checks the configured source plus PostgreSQL/schema and enabled external dependencies before processing documents. Embedding preflight validates vector shape. Chat preflight performs a minimal text request. Vision preflight validates local Google Vision client/credential initialization and does not claim to prove a later remote OCR call will succeed.

Use:

```powershell
python mir_ai_main.py --config config.ini --plan
python mir_ai_main.py --config config.ini --preflight-only
python mir_ai_main.py --config config.ini
python mir_ai_main.py --config config.ini --status
python mir_ai_main.py --config config.ini --failed 100
python mir_ai_main.py --config config.ini --run-status <run-id>
```

## Enterprise gateway HTTP 400

A 400 response is considered permanent for that unchanged request. The Azure/APIM response code/message, HTTP status, and request ID are captured without logging the API key or input document text. The system does not automatically change the required embedding API version/model to make a request pass.

`requirements_mode=strict` enforces `text-embedding-3-large`, 3072 dimensions, and API `2025-04-01-preview`. `requirements_mode=warn` exists only for controlled gateway compatibility diagnosis and must not be represented as requirements-qualified output.
