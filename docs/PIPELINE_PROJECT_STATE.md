# Regulatory extraction — living engineering state

**Current branch**: [feat/rich-document-multilingual-v2](https://github.com/dhanumjayasaggurthi/chunk_vectors/tree/feat/rich-document-multilingual-v2)
**Repository**: [chunk_vectors](https://github.com/dhanumjayasaggurthi/chunk_vectors)
**Updated**: 2026-11-24. Check branch HEAD and CI; never assume a particular commit remains HEAD.

**This is the canonical handoff link for a new conversation.** Read this document and [native_throughput_semantic_v2.md](native_throughput_semantic_v2.md), inspect branch HEAD, then work from the P0 section below. Update this document whenever implementation, validation, benchmarks or priorities change.

## Objective and safety contract

Extract **3.2 million regulatory PDFs/DOCXs, approximately 4 TB, within 15 calendar days** into PostgreSQL. Preserve original native text, rich formatting, table-cell grids, page/bbox/source version, images/figures and language provenance. Support independent OCR/vision, multilingual translations, semantic chunks and optional embeddings; keep all knobs and destination names configurable.

Non-negotiables: **no silent data loss, no fabricated numbers/table cells, no per-document manual approval, no gratuitous services/tables**. Native extraction must not wait on cloud OCR, translation or vector DB. Source file/version and extraction profile hashes must stay traceable.

Math: 3.2M/(15*86400) = **2.47 docs/sec sustained** fleet-wide. Plan >=5 docs/sec aggregate headroom; this is NOT measured performance. Average 4TB/3.2M = approximately 1.25 MB/file; measure actual document page-count distribution, % scanned, tables, retries, CPU/RAM and GCP quotas before sizing hardware.

## Environment

Repo branch above. Windows local validation directory: chunk_vectors_validation, venv executable ./.venv-validation/Scripts/python.exe. Host cannot clone GitHub; transfer changes by downloading the one feature-branch ZIP. No production credentials in Git. Database 'regulatory'; isolated schema 'cv_validation_v1'. Existing native source table 'cv_bench_pages_v1'. Proposed but NOT yet initialized semantic chunk output 'cv_bench_semantic_v2'. All names must be configured through profiles.

## Evidence from original regulatory PDFs (user-provided)

- 747-page pilot: **747/747 published**, page 104 seven image fragments as one source-linked group; 79 deferred visual groups.
- 778-page PBRER: initial strict extraction rejected 26 empty detected table regions (pages 117–171); five additional genuine page tables had source cells. New opt-in 'empty_table_policy=preserve_defer' completed 778 pages. Read-only PostgreSQL audit: **778/778 published, 26 unresolved source geometries persisted, five genuine tables retained, zero new version errors/checkpoints, no provenance issues**. Earlier 752 strict-version checkpoints isolated.
- 778-page fresh processing: ~111.77 sec; **417.7 pages/min** on one native worker; pdf_parse ~103.785s; checkpoint SQL ~5.31s; publish SQL ~0.75s. OCR and embeddings OFF.
- Semantic-v2 read-only preview of 778 pages: **5,601 chunks in ~10.96s** with 800 maximum tokens, 80 overlap, embeddings OFF. This proves preparation but NOT semantic quality or cost; avoid persisting chunks before review of quality distribution.
- **Latest real-document quality preview (user report)**: 5,601 chunks, token p50=66, p90=363, p95=430, max=798; 4,321/5,601 (77.1%) below min=120; 1,867 underfilled table-boundary and 2,453 structural-boundary; 2,195 distinct table IDs, six verified cross-page groups, 27 exceptional split events (four paragraph, 21 paragraph-word, two table-row-word); zero oversized chunks, zero broken structural links. **Quality gate: do not materialize/embeddings yet**. These are observed results, not a fleet benchmark.
- Benchmark corpus: 11 mixed regulatory PDFs / 11,584 pages, includes 2,548 and 3,082-page outliers. Synthetic 100-document CI is not original-document stress evidence.

## Existing implementation

- pdf_processor.py/page_stage.py: bounded windows, process-based native workers, optional rich formatting, original image/graphical source references, strict table source preservation and configurable defer-on-empty-candidate behavior, checkpoint recovery, versioned atomic PostgreSQL publication. Native success explicitly does not imply OCR/visual interpretation.
- db_pool.py/config.py: configurable process-local bounded connection pooling and safe session reset. Process workers need >=2 PostgreSQL leases per process because document advisory lock + checkpoint transaction.
- configurable_chunking.py/semantic_chunking_v2.py: opt-in semantic_v2. Within max_tokens: keep intact paragraphs; oversized paragraph => split by sentences; exceptionally long single sentence/row => word/Unicode fallback marked in exceptional_splits, never silently truncate original text. min_tokens is a soft target. Preserve original page text separately.
- table_continuity.py: source-table fragments on adjacent pages share logical table ID only with page-edge positions, matching column geometry and **verified repeated header**. Independent source page grids stay intact. Row chunks repeat actual detected headers. Ambiguous table links remain separate.
- Cross-page paragraphs: require same logical section, adjacent source pages, unfinished sentence, lowercase continuation **and source page-edge geometry**; configurable page_join_edge_fraction (default .20). Structural section ID and previous/next chunk ID connect related retrieval units across pages.
- iter_logical_table_rows() in table_continuity.py streams the complete linked multi-page table projection as (original_page, original_row_index, original_cells), skipping only verified duplicate header rows while leaving each source page grid untouched. This enables a future source-grounded semantic summary without reopening a PDF.
- reuse_materialize.py: explicit read-only preview now includes quality metrics directly (token percentiles, short chunks, table group counts, exceptional splits, structural link integrity) without another audit script. Original document/chunk metadata fields remain distinguishable. No embeddings when disabled.
- Materialized chunk IDs include profile fingerprint, unlike internal chunker IDs. prepare_document now rewrites previous/next links to **actual persisted output chunk IDs**, fixing downstream retrieval continuity. Integration regression test checks these links and the quality preview.
- page_stage profile_stage_timings now measures pdf_table_detection and pdf_table_cell_extraction separately from pdf_other_parse and PostgreSQL SQL, to pinpoint CPU bottlenecks.

### Semantic profiles (examples; LOCAL source mappings authoritative)

~~~yaml
chunking:
  strategy: toc_then_semantic
  boundary_mode: semantic_v2
  min_tokens: 120
  max_tokens: 800
  overlap_tokens: 80
  tokenizer: cl100k_base
  preserve_headers: true
  merge_cross_page_paragraphs: true
  page_join_edge_fraction: 0.20
  table_row_mode: rows
  table_continuation: conservative
  max_table_pages: 8
  table_edge_fraction: 0.25
  table_x_tolerance: 0.05
embedding:
  enabled: false
  input_fields: [section_title, chunk_text]
  input_max_tokens: 6000
~~~

Native profile uses visual_processing=preserve_defer, empty_table_policy=preserve_defer, capture_rich_text=true, capture_table_structure=true, worker_backend=processes, page_batch_size=32, profile_stage_timings=true, ocr_enabled=false and embeddings disabled. Keep local actual schema/table names and other metadata settings.

Semantic-v2 implementation identity is also versioned internally (revision 2), **even when YAML is unchanged**; this prevents stale rows being SKIPPED after boundary/neighbor logic changes. Legacy profile fingerprints are preserved. Verify versioned output target before any materialization; never silently mix old/new chunk content.

Note: changing semantic chunk rules changes its profile fingerprint, and the preview chunk count may change. **Do not overwrite a previously versioned output without explicit isolation.**

## Step 59 — configurable small-chunk packing, in development

**Cause verified in current code**: semantic_v2 previously never consulted chunking.small_section_policy or min_tokens for consolidation. It flushed on every new table ID and type boundary, which left thousands of undersized standalone table and text chunks. This does not imply the 2,195 table detections were invalid.

**New opt-in**: chunking.small_section_policy=merge_forward. The existing 'keep' option preserves old chunk boundaries. Merge compatible short retrieval chunks only while original section path remains the same, within configured max token limit, page-span <= small_chunk_max_pages (default 2, range 1..8), and distinct source tables <= small_chunk_max_tables (default 3, range 1..12). Unknown sections may merge only within the same page; across different section headings never merge. Do not fabricate or truncate source content.

**Source fidelity**: each packed chunk records source_components = original page spans, table IDs, content types and SHA-256 of its unmodified retrieval text; table_components references original distinct logical table IDs/geometry. Multiple source tables are never misrepresented by a single table_id. Original source_tables and page rows remain untouched. Derived previous/next IDs and section offsets are recalculated, with materialization mapping to the actual persisted profile-versioned IDs. Internal semantic implementation version **3** prevents stale output reuse.

**Preview quality**: reuse_materialize.py preview reports packed_chunks, packed_source_components, estimated_original_chunks, table_ids, page provenance validation and unchanged token percentiles/structural checks. No new CLI/scripts/tables.

**Original 778-page packed preview: ACCEPTED AS VALIDATION BASELINE (user-reported 2026-11-24).** Profile cv_corpus_semantic_v3_packed.yml, small_section_policy=merge_forward, max pages=2, max distinct tables=3. 3,901 vs 5,601 chunks (-30.4%); p50=133 vs 66; p90=405, p95=474, max=798; 1,485 vs 4,321 short chunks (-65.6%), 226 vs 1,867 short table-boundary chunks (-87.9%); packed_chunks=1,365; packed_source_components=3,065; estimated_original_chunks=5,601; exactly 2,195 logical tables and 6 verified multi-page groups; 0 oversized and 0 structural errors; 27 exceptional split events unchanged. **Read-only preview only; no packed chunk rows persisted. Stop tuning and proceed to OCR.**

**CI**: [Windows/Linux Python 3.11/3.13 + disposable PostgreSQL](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45335880576) **PASS** and [rich-document CI + 100 synthetic mixed PDFs](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45335880482) **PASS**, implementation commit [6e5e1a46](https://github.com/dhanumjayasaggurthi/chunk_vectors/commit/6e5e1a46f13ca6efef6c6c4c6eca725b891e2d54). Synthetic CI only: real-document packed counts not yet measured.

## CI and correctness gates

CI:
- .github/workflows/ingestion-reuse-tests.yml: Windows/Linux Python versions, disposable PostgreSQL tests, process workers, extraction replay, semantic chunk tests.
- .github/workflows/rich-document-tests.yml: rich PDF/format/table regression, semantic chunk tests, 100 mixed-content synthetic PDF smoke test.

Focused tests: tests/test_semantic_chunking_v2.py, tests/test_materialization_quality_summary.py, tests/test_empty_table_deferred.py, tests/test_db_pool.py, tests/test_native_process_workers.py. Verify latest HEAD CI; don't claim unobserved private-PDF testing.

**Required gates before writing chunks**: no token-size overruns, zero broken next/previous or section IDs, exact page and table-source lineage, credible tiny-chunk distribution, no dropped source cells, unresolved figures recorded rather than described as OCR-complete. Preview does not write to PostgreSQL; output init must be explicitly applied after destination check.

Commands:

~~~powershell
$Python = ".\.venv-validation\Scripts\python.exe"
& $Python -m unittest discover -s tests -p "test_semantic_chunking_v2.py" -v
& $Python -m unittest discover -s tests -p "test_materialization_quality_summary.py" -v
& $Python reuse_materialize.py validate --profile .\profiles\cv_corpus_semantic_v2.yml
& $Python reuse_materialize.py preview --profile .\profiles\cv_corpus_semantic_v2.yml --doc-id 2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86
~~~

## Independent deferred OCR — newly implemented, validation in progress

**Module**: deferred_ocr.py, [runbook](DEFERRED_OCR.md), tests/test_deferred_ocr.py. OCR source discovery is separate from native extraction and chunk materialization; one additive OCR job/result table in the existing extraction schema is created only with explicit init --apply. Read-only validate/discover never call cloud services. enqueue --apply is idempotent. run --apply alone issues batch GCP Vision calls under shared project-wide PostgreSQL request/image quotas. Bounded thread workers, per-thread PyMuPDF source PDF clip rendering, max image/pixel/byte budgets, DB SKIP LOCKED leases and explicit retry/terminal states.

Published pages remain immutable. Source file doc-id and SHA-256 must match original; current published document version is enforced on claim. Asset IDs incorporate source/extraction hash plus OCR semantic render/model fingerprint (DPI, min confidence, word/block bbox). No-text or low-confidence OCR => VISUAL_PENDING; it is NOT a reconstructed table or chart. Original OCR text/word polygons and confidence stored only in OCR output rows. DB lease renewal before each sub-batch, expired/terminal attempts recovered by the claim handler. **Full chart description, native/OCR text reconciliation, OCR overlay into semantic materialization and actual GCP pilot remain outstanding**.

CI: cloud-free tests on Windows/Linux and disposable PostgreSQL. Check latest [ingestion workflow](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/workflows/ingestion-reuse-tests.yml) and [rich-document workflow](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/workflows/rich-document-tests.yml). Do not claim original private PDF OCR has run; **NO GCP CLOUD CALLS HAVE BEEN MADE**.

Next immediate action: once CI green, create a COPY of the existing native preserve_defer YAML with optional deferred_ocr settings, then run deferred_ocr.py validate and discover for the verified 778-page doc ID (read-only, no DDL). Reconcile expected ~323 previously deferred visual candidates. Ask for explicit approval before any cloud OCR calls, even in the staging environment. Do not rerun the native extractor or packed chunk audits.

## What is NOT done

- **Deferred OCR processing implementation exists**, with explicit source-referenced rendering, bounded batches, global quotas and durable retries. It has **not** been invoked against original sources/GCP. Enrichment output is not yet rejoined with retrieval chunks; reconcile duplicates and original page/table geometry before exposing derived text.
- **No full semantic summaries of reconstructed multi-page tables** yet. Existing table_structure_summary is a source-grounded structural description (columns, row counts/pages), not a clinical/numeric conclusion. Later optional async grounded summary stage must use whole logical table, cite cells/pages and undergo accuracy evaluation.
- **No streaming large-document materialization** yet: extracted_source.load_extracted_document collects all source pages with rows.extend, then prepare_document/save_document hold all chunks. Implement bounded sliding-window/two-pass or spool processing before 25k-page stress. Preserve deterministic neighbor IDs and transaction atomicity.
- The 15-day SLA is **not validated**. Stage parse dominates the pilot. Next original-document run should use substage timing and A/B profiling before switching table detector or reducing strictness. Scale nodes/workers based on fleet file/page distributions and p95 throughput, not a single 778-page PDF.

## Execution priorities — no detours

1. **P0 quality and throughput**: confirm latest HEAD CI and integrated real-document preview quality report, validate source coverage/section links/4-page table relationships. Fix actual fragmentation or boundary error in code; keep source pages untouched.
2. **P0 performance**: profile PDF table detection vs cell extraction using new counters, compare process workers/CPU/RSS/SQL with same original-PDF fixtures and profiles. Optimize measured bottleneck without loosening source fidelity.
3. **P0 independent OCR**: implement config-driven deferred OCR worker pool using existing GCP primitives and source visual refs, separate retry budgets, no manual approvals. Reconcile new OCR-derived text into versioned content without blocking native ingestion.
4. **P0 bounded memory**: streaming materialization for pathological 10k–25k+ page documents, preserving full table/section continuity.
5. **P1 semantic table summaries + search/vector API**: downstream teams need one stable interface with document/page/chunk metadata, table references, optional embedding and explicit enrichment states.
6. **P1 fleet benchmark**: >=100 original regulatory documents of diverse layouts/languages, sustained multi-node load, retries, QA sampling, p95 processing time, peak RSS, PostgreSQL WAL, OCR quota, and projected 3.2M/15d feasibility.

## Latest validated automated evidence

- 2026-11-24, implementation commit [a0233637](https://github.com/dhanumjayasaggurthi/chunk_vectors/commit/a02336373f5a7d227ad0c7489832639b64be6865): [ingestion CI 45332875178](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45332875178) **PASS** (Windows/Linux Python 3.11/3.13, disposable PostgreSQL, integrated quality preview, semantic paragraph/table linking, native stage).
- [Rich document CI 45332875209](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45332875209) **PASS**, including 100 synthetic mixed-content PDF smoke.
- **These are CI/synthetic tests**, not evidence of 3.2M-file fleet throughput, private PDF retest of the new chunk-boundary heuristic, or production deferred OCR.
- Final tested implementation [2bfa4ace](https://github.com/dhanumjayasaggurthi/chunk_vectors/commit/2bfa4aceb6a6eedad1c9a5041344231562675f9f): [full ingestion CI 45333264633](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45333264633) **PASS**, including disposable PostgreSQL, source-cited 4-page table projection, versioned output chunk-link integrity and cross-platform tests; [rich document CI 45333264612](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45333264612) **PASS**, including 100-document synthetic smoke.
- **Final semantic-versioning revision** [c41c6082](https://github.com/dhanumjayasaggurthi/chunk_vectors/commit/c41c6082cc435238bdf327034c4b876976d5cc61): [ingestion CI 45333495281](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45333495281) **PASS** (Windows/Linux 3.11/3.13 + disposable PostgreSQL); [rich document CI 45333495279](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45333495279) **PASS**, including synthetic 100-document smoke. The new semantic-v2 implementation revision rotates profile fingerprint even when YAML stays unchanged, preventing reuse of old chunk identities.
- Latest feature-branch document changes do not modify extraction or chunk logic beyond the tested implementation; always re-check HEAD/CI before merge.

## How to resume from this file

Open branch URL, then this exact file. Read latest Git commits/CI, inspect source and tests before modifying. Continue from priority 1 without repeating setup, approval gates, new schemas or separate audit scripts. **Update this file with each meaningful change, test evidence, current blocker, next action and SHA.** Always distinguish 'implemented', 'CI-tested', 'validated on original PDFs', and 'SLA proven'.
