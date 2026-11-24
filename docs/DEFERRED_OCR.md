# Deferred OCR stage — design, controls and usage

Read [PIPELINE_PROJECT_STATE.md](PIPELINE_PROJECT_STATE.md) first. This stage is implemented and **cloud-free CI passed**, including [Windows/Linux plus disposable PostgreSQL](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45338875585) and [rich-document tests](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45338875427). No actual GCP OCR calls or original 778-page OCR discovery have been executed. Perform a read-only discovery before authorizing cloud requests.

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
& $Python deferred_ocr.py run --profile $Profile --max-jobs 32 --apply
~~~

The read-only discover result should report the total pending visual candidates, expected to be comparable to the 323 original deferred visual assets on the 778-page pilot. The exact count must be checked rather than assumed. The run command has a per-invocation max_jobs safety bound. Multiple workers share the PostgreSQL quota, backpressure and idempotent asset IDs.

## Operational & quality policy

- Cloud requests are never implied by native extraction, profile validation, discovery or job enqueueing.
- Retry allowances are bounded. An expired lease can be reclaimed; a worker that loses its lease cannot commit a stale result.
- A cloud call that succeeds but is followed by a process crash may be called again after the lease expiry; exactly-once remote RPCs cannot be guaranteed. Asset IDs prevent duplicate committed results and support billing audits.
- The original PDF must remain accessible; moving/renaming it changes the native doc ID in the existing pipeline.
- OCR does not by itself constitute a complete clinical chart, table or diagram interpretation. Future enrichment must read the full original figure or table with verified page/cell citations, suppress hallucinated values, and preserve the original source.
- The OCR table is deliberately separate from native pages. A future versioned overlay must reconcile duplicate native/OCR text, resolve rotated-page coordinates, and expose source-linked content to semantic materialization. This is **NOT implemented yet**; do not claim end-to-end OCR-to-chunks completion.
- Do not open new approval queues. VISUAL_PENDING is an automated retry/enrichment classification, not a request for a person to transcribe each source graphic.

## What to verify next

1. CI for cloud-free unit tests, Windows/Linux and disposable PostgreSQL transactional tests.
2. Read-only discovery on original 778-page PDF; reconcile image groups vs unresolved vector regions.
3. Cloud pilot on a **very small, explicitly authorized** number of assets; confirm quota/project, confidence/word polygons, latency, retries and cost. Do not run all 323 before validating observed quality.
4. Implement optional source-versioned OCR overlay into retrieval chunk generation (while preserving original pages), then independent grounded visual/table summaries.
5. Prove fleet OCR throughput, peak RSS, per-document retries and completed-source/annotation coverage against 3.2M/15 days.

Every meaningful outcome and linked commit/test must also be recorded in [PIPELINE_PROJECT_STATE.md](PIPELINE_PROJECT_STATE.md), so a new session can resume from Git alone.
