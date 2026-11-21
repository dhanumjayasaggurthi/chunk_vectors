# Regulatory PDF extraction: staged pilot and production gate

**Status: opt-in pilot, not an automatic change to production.**
The existing fast embedding runtime and its PostgreSQL task/quota tables
remain unchanged. This implementation reuses PyMuPDF, pdfplumber, GCP
Vision, the existing intake profile, the current rechunker and the
existing fast embedding worker. No additional broker, agent framework,
or orchestration service is needed.

## Why this path is faster

| Original path | Opt-in staged path |
|---|---|
| Parse all PDF pages and retain all raster images before doing OCR | Parse a bounded window, OCR it, persist success, release raster buffers |
| Re-open PDF per table-page detection | Hold one PyMuPDF and one pdfplumber handle per document; close per-page layout caches |
| One Vision RPC for each individual image | Up to 16 independent images per synchronous DOCUMENT_TEXT_DETECTION request |
| In-process-only fixed OCR semaphore | Separate OCR doc/image worker pools and PostgreSQL shared request + image quota across hosts |
| Restart a long PDF from page one | Reuse successfully checkpointed pages for an identical source SHA256 + profile fingerprint |
| Incomplete OCR may be lost in successful processing | Fail the affected page, append an audit event, never publish a partial document |
| Embed directly on main ingestion | Stage pages first, then reuse existing metadata/chunking, then run the already-proven embedding worker separately |

## Verified API constraints

References:
- https://docs.cloud.google.com/vision/quotas (last updated November 17, 2026)
- https://docs.cloud.google.com/python/docs/reference/vision/latest/google.cloud.vision_v1.services.image_annotator.ImageAnnotatorClient
- https://pymupdf.readthedocs.io/en/latest/recipes-images.html

Google publishes a **16-image** limit for synchronous `images:annotate`,
a **20MB** image file limit and a **10MB** JSON request limit. Google
also meters feature quota per **image**, not merely request. The
published default request and text-feature quotas are 1,800/minute,
but the effective authorized quota for the production project may
be lower. We cannot infer the effective applied quota from a successful
service-account OCR call.

The pilot defaults to **1,500 request/min and 1,500 images/min across
both servers**, with a safety margin and a shared adaptive 429 cooldown.
This is a configurable **ceiling**, NOT a guaranteed service entitlement
or measured throughput. Raise/lower only after observing sustained
requests, errors, retries, DB waits and OCR content quality.

## Pilot run: separate checkout, separate DB target

Do not switch the Git branch used by active production processes.
Create a separate worktree and copy the **non-secret** settings from the
current working configuration. The approved DB must already have the
privileges and extension required by the source pipeline.

```bash
git fetch origin
git worktree add ../epod-page-pilot origin/feat/main-ingestion-reuse-performance
cd ../epod-page-pilot

# Create or copy a secure staging config.ini with [POSTGRES] and [GCP]
# settings. The existing gcp_key.json credential path is honored.
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m pip install 'PyYAML>=6.0.2,<7'

PROFILE=profiles/regulatory_page_stage_example.yml

# Validate YAML settings and profile fingerprint without any DB or OCR:
./venv/bin/python -m page_stage validate --profile "$PROFILE"

# Inspect page count without making GCP requests or DB writes:
./venv/bin/python -m page_stage preview --profile "$PROFILE" \
    --file /path/to/approved/regulatory-sample.pdf

# DBA-approved staging only: create dedicated pages/work/docs/errors tables
# and one quota row shared by machines using the same GCP project and DB:
./venv/bin/python -m page_stage init --profile "$PROFILE" --apply

# Start with a consented representative 1-document sample, not millions:
./venv/bin/python -u -m page_stage run --profile "$PROFILE" \
    --file /path/to/approved/regulatory-sample.pdf --doc-workers 1

# After full stage publication, materialize independent consumer chunks:
./venv/bin/python -m reuse_materialize validate --profile "$PROFILE"
./venv/bin/python -m reuse_materialize init --profile "$PROFILE" --apply
./venv/bin/python -m reuse_materialize run --profile "$PROFILE" \
    --max-docs 20 --doc-workers 2
```

The staged output is source-compatible with the existing
`reuse_materialize.py` via `extracted_content.element_json_column`.
The data includes:
- Original page number and section title
- Page size and rotation
- Native PDF region types and bounding boxes
- OCR text, confidence and word-level polygon vertices (including rotation)
- Table markup and table bounding boxes
- Document content SHA256 plus processing profile fingerprint

For production embeddings, use an independently reviewed
`--embed-existing` JSON mapping with `existing_chunks.table`
pointing to the **new output chunk table**. Do NOT enable that
worker while rechunking is replacing rows in the same destination;
the existing engine intentionally has its own running-task contracts.

## Work distribution and throttle controls

```yaml
extraction:
  page_batch_size: 32
  max_window_raster_bytes: 160000000
  max_render_pixels: 50000000
  ocr_workers: 4
  ocr_batch_images: 8
  ocr_requests_rpm: 1500
  ocr_images_ipm: 1500
  ocr_max_batch_bytes: 6000000
  ocr_timeout_seconds: 90
  ocr_max_retries: 4
  ocr_confidence_min: 0.60
  ocr_bbox_detail: word
  chart_workers: 4
  chart_descriptions_enabled: true
  table_summaries_enabled: false
```

**Different controls:** `--doc-workers` parallelizes separate PDFs.
`ocr_workers` sets a **process-wide concurrency ceiling** across all document
workers; the OCR thread pools may run inside individual documents but share
that one gate. `ocr_batch_images` controls images per GCP RPC and does not
change the image feature quota. `chart_workers` similarly caps chart
descriptions **across documents within a process**, separate from the OCR gate.
`page_batch_size` and `max_window_raster_bytes` bound in-memory
page/raster work. Azure chat quota is NOT coordinated across servers by the
OCR quota limiter; monitor GPT chart-description 429s before running multiple
chart-heavy hosts.

Several servers using the **same GCP project** must use the **same
PostgreSQL quota database** to obtain cross-host enforcement. Each
quota row is keyed by a SHA256 of the service-account project id, not
by a private key. Changing RPM/IPM on only one host is rejected.

A 429 sets a shared cooldown and reduces the adaptive rate factor.
Successful requests restore it gradually. When a cap is reached,
workers wait instead of hammering the API; the remaining work is not
marked successful. If a limit is too restrictive, fix the measured
limit, not the error-handling mechanism.

## SQL monitoring: read-only

Substitute the approved schema/table names used in the pilot.

```sql
-- Published documents and pages (only complete document snapshots):
SELECT COUNT(*) AS published_documents,
       SUM(page_count) AS published_pages,
       MAX(completed_at) AS latest_document
FROM palm_paw.regulatory_pages_pilot_v1_docs;

-- Durable page checkpoints for incomplete documents:
SELECT doc_id, source_hash, COUNT(*) AS completed_checkpoint_pages
FROM palm_paw.regulatory_pages_pilot_v1_work
GROUP BY doc_id, source_hash
ORDER BY completed_checkpoint_pages DESC LIMIT 30;

-- All failed pages, including pre-page document errors:
SELECT doc_id, page_number, error_code, error_message, occurred_at
FROM palm_paw.regulatory_pages_pilot_v1_errors
ORDER BY occurred_at DESC LIMIT 30;

-- Applied shared adaptive Vision quota:
SELECT rpm,ipm,round(multiplier::numeric,3) AS adaptive_multiplier,
       round(requests::numeric,2) AS request_budget,
       round(images::numeric,2) AS image_budget,
       cooldown_until
FROM ocr_runtime.quota;
```

## Release criteria

1. **Accuracy is a gate, not a throttle knob.** No document is
   published until *all* pages have succeeded, the input PDF SHA256
   is unchanged, and low-confidence/failed OCR, unresolved table
   detections and chart interpretation failures have been escalated.
   Failed pages are durable and retryable.
2. Use representative regulatory PDFs with rotated/upside-down labels,
   mixed native/scanned pages, sparse tables, borderless tables, images,
   charts, page-spanning structures and 500, 15,000 and 25,000 pages.
   Compare output to labeled source samples at both exact-text and
   field/table/region levels; review every unresolved critical field.
3. Measure windows/minute, successful *OCR images/minute*, attempts,
   429/5xx rates, p95/p99 RPC latency, memory high-water mark, DB
   commit latency, complete docs/hour and cross-host fairness.
4. Restart an extraction mid-document; verify only missing page
   numbers are processed, every output set is complete, old published
   output remains until the replacement commits, and source changes
   prevent stale publication.
5. No claims of 25k-page production throughput or perfect OCR accuracy
   until the true mixed-document corpus has passed a long-running load
   and ground-truth evaluation. Synthetic documents exercise
   correctness and bounded memory, not real-world text-recognition quality.

## Current known constraints

- The staging CLI supports local/NAS input files and a bounded
  directory walk. The existing S3 downloader may produce local input
  files, but a first-class S3 document-level staging/discovery adapter
  with stable canonical IDs is still a separate release requirement.
- Changing worker counts, OCR batch sizes, timeouts or RPM/IPM limits
  **does not change the extraction-content fingerprint** and therefore does
  not invalidate completed page checkpoints. Accuracy-affecting OCR/table
  policies deliberately create a new checkpoint identity.
- A significant non-chart image producing no OCR text is **quarantined** as
  requiring visual review. Do not treat it as a successfully extracted figure.
- An already flattened source table cannot reconstruct missing
  table-cell geometry or native page images. The new staged-page
  output retains whatever the actual PDF extractor detected.
- The existing `reuse_materialize` step currently reconstructs one
  complete document's source text in memory. For the very largest
  extracted documents, streaming rechunking must be load-tested (and
  further bounded if needed) before enabling a 25,000-page pilot.
- Page-spanning paragraph/table continuity is **not yet validated**.
  The page stage preserves source page numbers, headings, individual table
  regions and their cells, and the rechunker can group adjacent pages.
  That is not equivalent to deterministic repeated-header reconciliation,
  table row continuation or a guaranteed unbroken paragraph. These require
  labeled multi-page regulatory tests and explicit continuation handling
  before the production accuracy gate can pass.
- Rotated word polygons are preserved, but perfect character reading
  order or table reconstruction is not guaranteed by Vision OCR.
  Ambiguous critical content must be flagged for review.
- The current retry / rate limit logic handles transient errors; it
  cannot increase an actual cloud project's approved quota.
