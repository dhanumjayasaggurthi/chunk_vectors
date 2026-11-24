# Deferred OCR stage — design, controls and usage

Read [PIPELINE_PROJECT_STATE.md](PIPELINE_PROJECT_STATE.md) first. This stage is implemented and **cloud-free CI passed**, including [Windows/Linux plus disposable PostgreSQL](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45338875585) and [rich-document tests](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45338875427). **Original-document read-only discovery PASSED on 2026-11-24:** 778-page PBRER, 1 document and **323 visual candidates**, inserted=0, writes=false. No GCP OCR calls have been made. Proceed to isolated queue initialization/enqueue, then tiny source-scoped pilot. Final implementation evidence: [multi-platform ingestion/DB CI PASS](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45339898781), [rich-document CI PASS](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45339898776).

## End-to-end contract

- Native extraction publishes original page text, table source cells, layout, visual_asset image references and unresolved_region references. These published source rows and original PDFs remain immutable.
- deferred_ocr.py reads only source-version-matching published pages; it validates the original PDF path/doc ID and full SHA-256 before rendering bounded source clips.
- Independent stage, one additive PostgreSQL table named SOURCE_TABLE_visual_ocr in the same validation schema. init --apply is explicit, so ordinary validate/discover cause no writes.
- Each asset ID includes the original source checksum, extraction fingerprint, page/element evidence, and an OCR semantic fingerprint. Changes to resolution, provider or confidence policy create distinct OCR jobs; throttle/retry changes do not.
- enqueue --apply is idempotent; run --apply is the ONLY CLI command permitted to make GCP Vision calls. PostgreSQL FOR UPDATE SKIP LOCKED plus lease tokens and terminal attempt limits permit multiple independent servers. DB connections are released before cloud requests.
- Uses the existing Google Vision batched document OCR and shared Postgres request/image quotas. Per-thread PyMuPDF handles, bounded raster/payload dimensions and no LLM agents.
- TEXT_READY means acceptable OCR text, not validated chart/table meaning; VISUAL_PENDING means no text/low confidence and requires future visual interpretation. RETRY_WAIT and FAILED_TERMINAL are explicit. No fabricated numerical or clinical claims.
- Source page JSONB and source table grids are never overwritten by OCR. OCR results preserve text, confidence, language, word polygons, source hashes and error status.

## Local independent OCR configuration

Make a COPY of the existing verified native preserve_defer YAML into profiles/cv_corpus_deferred_ocr_v1.yml, without overwriting the source profile, and add this optional top-level mapping:

~~~yaml
deferred_ocr:
  workers: 4
  batch_images: 8
  max_batch_bytes: 6000000
  render_dpi: 200
  max_render_pixels: 50000000
  max_attempts: 3
  max_api_retries: 4
  timeout_seconds: 90
  quota_wait_seconds: 600
  lease_seconds: 3600
  retry_delay_seconds: 60
  confidence_min: 0.60
  bbox_detail: word
  rpm: 1500
  ipm: 1500
~~~

**Values are examples, not quotas granted by GCP.** Confirm the actual shared Google Project Vision quotas across all nodes before any cloud run. GCP credentials use the existing config.ini or GOOGLE_APPLICATION_CREDENTIALS. Source extraction must remain extraction.visual_processing=preserve_defer and extraction.ocr_enabled=false. All settings are validated.

## CLI sequence

~~~powershell
$Python = ".\.venv-validation\Scripts\python.exe"
$Profile = ".\profiles\cv_corpus_deferred_ocr_v1.yml"
$DocId = "2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86"

# Safe: no schema changes and no GCP calls
& $Python deferred_ocr.py validate --profile $Profile
& $Python deferred_ocr.py discover --profile $Profile --doc-id $DocId

# Only after checking output table, code and CI:
& $Python deferred_ocr.py init --profile $Profile --apply
& $Python deferred_ocr.py enqueue --profile $Profile --doc-id $DocId --apply
& $Python deferred_ocr.py status --profile $Profile

# This explicitly sends actual source images to your configured GCP Vision:
& $Python deferred_ocr.py run --profile $Profile --doc-id $DocId --max-jobs 8 --workers 2 --apply
~~~

Read-only discovery **confirmed exactly 323 source-linked candidates** for the 778-page PBRER, with 0 PostgreSQL inserts and 0 GCP calls. `run --apply` now requires `--doc-id` (explicit single-PDF scope) or `--all-docs` (explicit fleet-wide scope), preventing accidental OCR of unrelated queued documents. The small pilot command above is intentionally limited to 8 jobs / 2 workers. Multiple workers share PostgreSQL quota enforcement and idempotent source IDs.

## Operational & quality policy

- Cloud requests are never implied by native extraction, profile validation, discovery or job enqueueing.
- Retry allowances are bounded. An expired lease can be reclaimed; a worker that loses its lease cannot commit a stale result.
- A cloud call that succeeds but is followed by a process crash may be called again after the lease expiry; exactly-once remote RPCs cannot be guaranteed. Asset IDs prevent duplicate committed results and support billing audits.
- The original PDF must remain accessible; moving/renaming it changes the native doc ID in the existing pipeline.
- OCR does not by itself constitute a complete clinical chart, table or diagram interpretation. Future enrichment must read the full original figure or table with verified page/cell citations, suppress hallucinated values, and preserve the original source.
- The OCR table is deliberately separate from native pages. A future versioned overlay must reconcile duplicate native/OCR text, resolve rotated-page coordinates, and expose source-linked content to semantic materialization. This is **NOT implemented yet**; do not claim end-to-end OCR-to-chunks completion.
- Do not open new approval queues. VISUAL_PENDING is an automated retry/enrichment classification, not a request for a person to transcribe each source graphic.

## Current recovery — original 778-page OCR enqueue (2026-11-24)

- Read-only discovery found exactly 323 assets, no writes or cloud calls.
- init --apply succeeded and created the isolated OCR job/result table; original pages were not changed.
- First enqueue --apply failed with PostgreSQL pool max=2 exhaustion. Cause: two suspended Python generators held leases and the insert requested a third; source-scoped status was {}. **Do not increase the pool cap or rerun native PDF extraction.**
- Fix in deferred_ocr.py closes read leases before yielded document and page batches. Regression covers 70 pages/140 assets, a forced two-lease cap, idempotent 128-row insert batches, and source-scoped status. **PASS**: [Windows/Linux/PostgreSQL CI 45341046132](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45341046132) explicitly ran the two-lease regression successfully; [rich-document CI 45341046181](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45341046181) also passed. Original 778-page enqueue retry pending.
- Next: copy corrected deferred_ocr.py from feature branch and rerun only enqueue for the already-verified doc ID, then status. Expected candidates=323 and PENDING=323, with no GCP calls.

## Latest original-document milestone — 2026-11-24

The corrected OCR enqueue was successfully retested in the user's original PostgreSQL validation schema. `documents=1`, `candidates=323`, `inserted=323`, `writes=true`; source-scoped `status` reports `PENDING=323` and `writes=false`. This resolves the earlier 30-second PostgreSQL pool exhaustion **without increasing max=2**. The OCR table already exists and no repeat init/enqueue is necessary. There have been **no cloud OCR calls**.

**Next pilot command (only after verifying actual GCP project quotas, credentials and permitted transfer of regulatory pages to Vision):**

~~~powershell
$Python = ".\.venv-validation\Scripts\python.exe"
$Profile = ".\profiles\cv_corpus_deferred_ocr_v1.yml"
$DocId = "2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86"
$Timer = [System.Diagnostics.Stopwatch]::StartNew()
& $Python deferred_ocr.py run --profile $Profile --doc-id $DocId --max-jobs 8 --workers 2 --apply
$ExitCode = $LASTEXITCODE
$Timer.Stop()
Write-Host "OCR pilot elapsed seconds:" ([math]::Round($Timer.Elapsed.TotalSeconds,2))
Write-Host "OCR pilot exit code:" $ExitCode
& $Python deferred_ocr.py status --profile $Profile --doc-id $DocId
if ($LASTEXITCODE -ne 0) { throw "OCR status query failed" }
~~~

Expect at most eight attempts, with text-ready / visual-pending / retry-wait / terminal-error outcomes determined by the real OCR results. OCR text is **not** an approved reconstruction of chart/table meaning. Do not automatically enqueue more assets or run all 323 until the first results and quotas are evaluated.

## First real OCR pilot: eight immediate retries — STOP (2026-11-24)

Original PBRER source-scoped run: **processed=8, RETRY_WAIT=8, duration=1.91 s, exit=0**; subsequent OCR queue: **315 PENDING + 8 RETRY_WAIT**. No successful OCR text or visual completion. Exit code 0 is currently misleading and cloud_calls_possible=true does not establish that Vision actually received calls.

**Do not rerun the job or reset statuses.** Use a read-only query on the existing OCR table by document ID and OCR hash for grouped error_code, error_message, attempts and next_attempt_at. Avoid disclosing PDF text or credentials. Once the cause is identified and fixed, resume only a small bounded retry batch. OCR throughput and 15-day fleet OCR capacity are still unmeasured.

## 2026-11-24 — OCR error diagnosis and allowed GCP capabilities

Original pilot error from stored PostgreSQL records: eight RETRY_WAIT assets with ModuleNotFoundError / No module named 'google', attempts=1 each. This is local missing Python SDK, before proof of any successful Vision API call. The source-scoped queue has 315 PENDING and eight RETRY_WAIT. Do not reset or retry jobs yet.

The only available credential source is gcpkeys.json. Only Google Cloud Vision OCR and Cloud Translation may be considered, subject to actual project API enablement, IAM, quotas and data-governance authorization. No Document AI, Vertex AI, Azure or other provider assumptions. Install google-cloud-vision, google-cloud-translate, google-auth into .venv-validation. Verify package imports and the exact key path, then optionally test both APIs using a small synthetic sample; redact credentials/tokens and avoid private source content. A tiny live API call may incur charges.
## GCP capability probe PASSED; safe retry-only pilot (2026-11-24)

Using gcpkeys.json in the user's active validation environment, project **rehdocreader** returned Vision OCR AVAILABLE with exact text 'VISION TEST 123' and Cloud Translation AVAILABLE with 'Bonjour le monde' for a synthetic French translation sample. Google Document AI, Vertex AI, Azure and other keys/services are not available and must not be used. Cloud project quotas and permission to process regulatory document content are separate from the successful synthetic API probe.

Original eight-PBRER OCR attempts remain RETRY_WAIT=8, with 315 unattempted PENDING. New flag **--retry-only** requires --doc-id and filters PostgreSQL claims to eligible RETRY_WAIT jobs only, so the recovery pilot cannot accidentally consume eight additional PENDING source assets. A retry-only run returns a nonzero exit if any job remains retryable or terminal, even if DB status updates succeeded. After copying the new deferred_ocr.py from the feature branch and confirming CI, use:

~~~powershell
$Python = '.\.venv-validation\Scripts\python.exe'
$Profile = '.\profiles\cv_corpus_deferred_ocr_v1.yml'
$DocId = '2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86'
& $Python deferred_ocr.py run --profile $Profile --doc-id $DocId --retry-only --max-jobs 8 --workers 2 --apply
$PilotExitCode = $LASTEXITCODE
& $Python deferred_ocr.py status --profile $Profile --doc-id $DocId
Write-Host 'OCR pilot exit code:' $PilotExitCode
~~~

**This is a billable Vision API call against original regulatory content.** It must not run before the user verifies allowed data processing and configured project quota limits. Do not re-enqueue, clear statuses or rerun native extraction. If processed=0, check retry eligibility; never force queue state. Send the result and status counts for measured performance/accuracy.
**SAFE RETRY-ONLY FIX TESTED** (2026-11-24): [Windows/Linux Python 3.11/3.13 and real disposable PostgreSQL CI 45343815239](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45343815239) **PASS**, and [rich-document CI 45343815324](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45343815324) **PASS**, commit 1d96adff. Specifically verified test_retry_only_claim_does_not_consume_pending_ocr_assets executed successfully in PostgreSQL CI. Production requirements.txt now explicitly lists google-cloud-vision, google-cloud-translate, and google-auth (commit 4b028a40). New retry-only CLI exists only in updated deferred_ocr.py; older local copies DO NOT support it. Real original PDF retry after package repair not yet executed.
## Step 64 — Live source-linked OCR recovery SUCCESS (user-verified 2026-11-24)

Using the existing gcpkeys.json credential for project rehdocreader, source-scoped retry-only OCR processed all eight previously failed regions: **TEXT_READY=6**, **VISUAL_PENDING=2**, **RETRY_WAIT=0**, **elapsed 3.98 s**, **exit code 0**. Current source-scoped queue totals: **PENDING=315**, TEXT_READY=6, VISUAL_PENDING=2. OCR stage and database result persistence are operational; original pages unchanged. Visual-pending regions are deliberately not declared reconstructed charts or structured tables. This is not a validated clinical accuracy score or sustainable load rate.

Next controlled extension (actual cloud calls, charges possible; only if original regulatory document transfer to the project is permitted):

~~~powershell
$Python = '.\.venv-validation\Scripts\python.exe'
$Profile = '.\profiles\cv_corpus_deferred_ocr_v1.yml'
$DocId = '2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86'
$Timer = [System.Diagnostics.Stopwatch]::StartNew()
& $Python deferred_ocr.py run --profile $Profile --doc-id $DocId --max-jobs 32 --workers 4 --apply
$ExitCode = $LASTEXITCODE
$Timer.Stop()
Write-Host 'OCR elapsed seconds:' ([math]::Round($Timer.Elapsed.TotalSeconds,2))
Write-Host 'OCR exit code:' $ExitCode
& $Python deferred_ocr.py status --profile $Profile --doc-id $DocId
~~~

Do not pass --retry-only for this fresh-batch test: it would correctly find no previously failed eligible assets. Do not repeat enqueue/init. Expect 283 PENDING if 32 new assets are claimed and completed (regardless of TEXT_READY versus VISUAL_PENDING), absent concurrent workers. Record actual retry/status/error counts, confidence distributions and elapsed time before scaling or presenting throughput estimates. Do not print source OCR text to shared logs.
## Step 65 — Original 32-asset Vision batch completed successfully (user-verified 2026-11-24)

Original PBRER run: --max-jobs 32, --workers 4, --apply, elapsed **4.58 sec**, **processed 32** = TEXT_READY 21 + VISUAL_PENDING 11; no retry failures, exit 0. This is **6.99 OCR assets/sec** on a tiny warm-up batch, not demonstrated sustained throughput or a clinical transcription quality score. Cumulative 778-page OCR status: **PENDING 283; TEXT_READY 27; VISUAL_PENDING 13; RETRY_WAIT 0**, exactly 323 original candidates. Do not re-init or re-enqueue.

**Read-only QA before scaling:** aggregate output OCR word-region counts, word confidence, languages, status/error by source kind and error_code, with source-version filtering. Do not print OCR text, credentials or individual clinical values. The 13 VISUAL_PENDING source regions must remain pending until a grounded separate visual-enrichment strategy is available (Google Vision and Google Translation only; no Document AI/Vertex AI/Azure). OCR text is not automatically a truthful reconstruction of clinical graphs or multi-page tables.

Next performance step after QA: a controlled **64–128 fresh PENDING jobs / 4 workers** against the same document, with measured wall-time, transient failures, project-wide quota limits and observed source readability; do not extrapolate 3.2M/15d capacity from the 4.58-sec pilot alone.

## What to verify next

1. CI for cloud-free unit tests, Windows/Linux and disposable PostgreSQL transactional tests.
2. **DONE 2026-11-24**: read-only discovery identified 323 candidates and isolated OCR table initialization succeeded. The first enqueue hit pool exhaustion; corrected module passes CI. Next: repeat enqueue only for that document and check PENDING=323. Do NOT repeat init.
3. Cloud pilot on a **very small, explicitly authorized** document-scoped 8-asset/2-worker batch; confirm real GCP project caps, confidence/word polygons, latency, retries and cost. Do not run all 323 before validating observed quality.
4. Implement optional source-versioned OCR overlay into retrieval chunk generation (while preserving original pages), then independent grounded visual/table summaries.
5. Prove fleet OCR throughput, peak RSS, per-document retries and completed-source/annotation coverage against 3.2M/15 days.

Every meaningful outcome and linked commit/test must also be recorded in [PIPELINE_PROJECT_STATE.md](PIPELINE_PROJECT_STATE.md), so a new session can resume from Git alone.
