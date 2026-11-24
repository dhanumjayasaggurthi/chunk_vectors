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
- Benchmark corpus: 11 mixed regulatory PDFs / 11,584 pages, includes 2,548 and 3,082-page outliers. Synthetic 100-document CI is not original-document stress evidence.

## Existing implementation

- pdf_processor.py/page_stage.py: bounded windows, process-based native workers, optional rich formatting, original image/graphical source references, strict table source preservation and configurable defer-on-empty-candidate behavior, checkpoint recovery, versioned atomic PostgreSQL publication. Native success explicitly does not imply OCR/visual interpretation.
- db_pool.py/config.py: configurable process-local bounded connection pooling and safe session reset. Process workers need >=2 PostgreSQL leases per process because document advisory lock + checkpoint transaction.
- configurable_chunking.py/semantic_chunking_v2.py: opt-in semantic_v2. Within max_tokens: keep intact paragraphs; oversized paragraph => split by sentences; exceptionally long single sentence/row => word/Unicode fallback marked in exceptional_splits, never silently truncate original text. min_tokens is a soft target. Preserve original page text separately.
- table_continuity.py: source-table fragments on adjacent pages share logical table ID only with page-edge positions, matching column geometry and **verified repeated header**. Independent source page grids stay intact. Row chunks repeat actual detected headers. Ambiguous table links remain separate.
- Cross-page paragraphs: require same logical section, adjacent source pages, unfinished sentence, lowercase continuation **and source page-edge geometry**; configurable page_join_edge_fraction (default .20). Structural section ID and previous/next chunk ID connect related retrieval units across pages.
- reuse_materialize.py: explicit read-only preview now includes quality metrics directly (token percentiles, short chunks, table group counts, exceptional splits, structural link integrity) without another audit script. Original document/chunk metadata fields remain distinguishable. No embeddings when disabled.
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

Note: changing semantic chunk rules changes its profile fingerprint, and the preview chunk count may change. **Do not overwrite a previously versioned output without explicit isolation.**

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

## What is NOT done

- **No independent deferred OCR enrichment worker** yet. Existing GCP OCR and quota primitives are available in vision_ocr.py/ocr_quota.py. Must build independent source-referenced rendering, batching, global quotas, retries/checkpoints and idempotent rejoin. Avoid synchronous OCR blocking native extraction.
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

## How to resume from this file

Open branch URL, then this exact file. Read latest Git commits/CI, inspect source and tests before modifying. Continue from priority 1 without repeating setup, approval gates, new schemas or separate audit scripts. **Update this file with each meaningful change, test evidence, current blocker, next action and SHA.** Always distinguish 'implemented', 'CI-tested', 'validated on original PDFs', and 'SLA proven'.
