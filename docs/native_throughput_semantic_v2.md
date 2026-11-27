# Native throughput and semantic chunking v2

**Always start from the [living project state and handoff](PIPELINE_PROJECT_STATE.md).** That file records verified original-PDF evidence, code changes, passing CI, 10-day capacity targets, outstanding OCR/streaming work and the next safe action.

## Scope and compatibility

Implemented as **explicit opt-in settings inside the existing Python pipeline**.

- PostgreSQL: bounded process-local pool, connection acquisition limits, session reset and automatic rollback on return.
- Native extraction: process-based PDF parsing, with bounded whole-pool recycling. The old thread backend remains available for synchronous OCR.
- Semantic chunks: paragraph-first, then sentence-aware overflow, with Unicode-safe fallback for indivisible text and an exact configured token limit.
- Table chunks: complete source rows grouped under repeated table headers. Consecutive page-edge table fragments link only with matching column geometry and repeated source headers.
- Cross-page paragraphs: optionally join when the source clearly continues the same sentence across adjacent pages.
- Continuity: section ID, previous/next chunk IDs and table source references stored in existing chunk_metadata JSONB.
- No manual reviewer or table-specific production database tables are required.
- Original pages, source cell grids and source image references remain immutable and available separately.

## PostgreSQL connection reuse

Configure these optional values within your existing [POSTGRES] section of config.ini, without overwriting any credentials:

~~~ini
pool_enabled=true
pool_min_connections=1
pool_max_connections=2
pool_acquire_timeout_seconds=30
connect_timeout_seconds=10
~~~

A pool is created per **process**, not per fleet. Four native processes with max=2
can use up to eight worker connections, plus parents and other services.
Budget the entire fleet against PostgreSQL connection limits. The native
worker holds one PostgreSQL advisory-lock lease for the entire document and
needs another lease for checkpoint writes; **pool_max_connections >= 2** is
required in process mode. Thread mode needs at least doc_workers + 1 pooled
connections per process to avoid lock starvation. Omitted or false
pool_enabled retains the existing unpooled behavior.

## Native PDF worker settings

Change only the runtime settings under extraction in the **existing** native
profile pointing to your validated source schema and tables:

~~~yaml
worker_backend: processes
worker_recycle_documents: 200
page_batch_size: 32
profile_stage_timings: true
~~~

This process backend requires visual_processing=preserve_defer and
ocr_enabled=false, chart_descriptions_enabled=false and
table_summaries_enabled=false. Synchronous OCR remains on the existing thread path. The production wrapper also supports the existing independent deferred OCR and retained-page visual processing, with bounded document-stage overlap. See the living project state for operational configuration and qualification limits.

These runtime-only settings do not change the native extraction semantic
fingerprint. Already published PDFs will SKIP on replay. Do not delete
checkpoints or published pages merely to generate a fresh benchmark.

## Chunk and metadata settings

In a separate **versioned validation chunk profile** retaining your existing
extracted_content and output configurations:

~~~yaml
chunking:
  strategy: toc_then_semantic
  boundary_mode: semantic_v2
  min_tokens: 120
  max_tokens: 800
  overlap_tokens: 80
  tokenizer: cl100k_base
  small_section_policy: keep
  preserve_headers: true
  table_row_mode: rows
  table_continuation: conservative
  max_table_pages: 8
  table_edge_fraction: 0.25
  table_x_tolerance: 0.05
  merge_cross_page_paragraphs: true
embedding:
  enabled: false
  input_max_tokens: 6000
  input_fields:
    - section_title
    - chunk_text
~~~

All new settings are type/range validated. Other document and chunk metadata
rules, embedding choices, input token limits, and destination schema/table
names remain configuration-driven. Existing profiles without these options
retain the previous chunking strategy.

Re-chunking with a different profile hash needs an isolated validation output
table because the current materializer deliberately rejects incompatible
profile versions in the same output. The production logical model remains
documents/pages/chunks.

## Quality policies

1. Original pages, source cells, bboxes, nulls and original source hashes stay
   untouched; only derived retrieval chunks include merged table/paragraph text.
2. Table continuation is **conservative**. It checks sequential pages,
   compatible columns and bounding boxes, matching source header text, and
   table proximity to page boundaries. If uncertain, it does not merge.
3. Each logical table shares table_id, table pages, row counts and verification
   flags in chunk metadata. Row-group chunks repeat the full source header.
4. Table summaries are currently **structural only** (pages, columns, row
   count). Semantic summaries and chart-value interpretation still need a
   separately evaluated enrichment stage; no values are fabricated.
5. Oversized paragraphs split by sentences first; oversized sentences or
   unusually long cells use a marked fallback to words/Unicode characters.
   No content is silently dropped or truncated.
6. Same-section chunks share section_id, and all chunks have previous/next
   chunk IDs. This enables adjacency-aware retrieval with one chunk API.
7. Existing PDF source-image references remain pending OCR/visual enrichment
   until an independent processor completes that work.

If profile_stage_timings is true, each successful document result also shows
pdf_parse, ocr, table_postprocessing, checkpoint_sql, publish_sql and
source_verification elapsed seconds, plus the initial source hash time.
Note that PDF table detection itself runs inside pdf_parse, not solely in
table_postprocessing. These counters enable bottleneck attribution without
extra database tables.

## Tests and rollout

Run from chunk_vectors_validation with the copied feature-branch files:

~~~powershell
$Python = ".\.venv-validation\Scripts\python.exe"
& $Python -m py_compile db.py db_pool.py page_stage.py processing_profile.py metadata_extractor.py configurable_chunking.py semantic_chunking_v2.py table_continuity.py
& $Python -m unittest discover -s tests -p "test_db_pool.py" -v
& $Python -m unittest discover -s tests -p "test_semantic_chunking_v2.py" -v
& $Python -m unittest discover -s tests -p "test_native_process_workers.py" -v
~~~

The live PostgreSQL tests are restricted to disposable CI PostgreSQL by
environment guards; they are deliberately skipped against your regulatory DB.

Before creating the versioned validation chunk destination:

~~~powershell
& $Python reuse_materialize.py validate --profile .\profiles\your_semantic_v2.yml
& $Python reuse_materialize.py preview --profile .\profiles\your_semantic_v2.yml --doc-id YOUR_DOC_ID
& $Python reuse_materialize.py init --profile .\profiles\your_semantic_v2.yml
~~~

Review the destination, then initialize explicitly using --apply. The existing
validated page content remains unchanged.

## 10-day rollout acceptance

1.3 million documents / 10 days = **1.505 documents/sec fleet-wide**, sustained (90.28 documents/minute).
A 50% throughput headroom target is 2.257 documents/sec, not a measured guarantee.
The completed 100-document native-only benchmark processed 80,312 physical pages at 18.451 pages/sec with four isolated process workers; it does not certify end-to-end capacity or semantic accuracy. Characterize corpus page-count distribution, OCR rate,
table density, encoding anomalies and retries; benchmark multiple nodes and
100+ representative original PDFs with p95 latency and peak RSS.

**Known remaining risks:** extracted_source.py still accumulates full-document
content in memory before chunking, chunk materialization still replaces chunks
per document, and deferred OCR/visual enrichment requires full provider, storage and publication throughput qualification. Benchmark outlier 25k-page PDFs, PostgreSQL WAL/commit latency,
memory, sustained parallel extraction and cloud quotas before treating the
10-day production SLA as validated.
