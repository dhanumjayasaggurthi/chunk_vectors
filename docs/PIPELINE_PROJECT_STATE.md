# Regulatory extraction — living engineering state

**Current branch**: [feat/rich-document-multilingual-v2](https://github.com/dhanumjayasaggurthi/chunk_vectors/tree/feat/rich-document-multilingual-v2)
**Repository**: [chunk_vectors](https://github.com/dhanumjayasaggurthi/chunk_vectors)
**Updated**: 2026-11-24. Check branch HEAD and CI; never assume a particular commit remains HEAD.

**This is the canonical handoff link for a new conversation.** Read this document and [native_throughput_semantic_v2.md](native_throughput_semantic_v2.md), inspect branch HEAD, then work from the P0 section below. Update this document whenever implementation, validation, benchmarks or priorities change.


## P0 UPDATE — derivative-only retention confirmed; 15-candidate real-document baseline

User explicitly confirmed originals must be purged after 15 days: **do not retain original PDF/DOCX copies or rely on source-archive fallback**. Only policy-permitted extracted/stored content remains. Full-page rendered derivatives can be sensitive substitutes and require separate explicit policy approval; no original archives have been authorized. Prior archive-option discussion below is historical, not the selected design.

Real 10-page article rerun on corrected inventory: **15 caption candidates (previously 28), unique continuous figures 1–15; 13 mixed raster/vector, 2 embedded raster; physical pages 3–9, printed labels 1342–1348; page-spanning images unassigned; no writes/cloud calls.** Accepted as caption-discovery baseline only. Screenshots confirm Figures 7–12 include SEM panels, logarithmic profiles, multi-panel plots, and the separate PBRER Figures 1/3 have error bars and statistical narrative. No complete visual association or numerical interpretation established.

New opt-in `visual_evidence.py` pilot renders bounded physical page ranges into RGB PNG derivatives and immutable per-page manifests (source SHA, page/label/rotation/geometry, DPI, renderer version, image SHA). Verify by capture ID without original source. Explicit full-page derivative permission and policy ID required; no original PDF/DOCX copy, no cloud/DB calls. It renders all selected pages deliberately for validation, not as a production storage recommendation. Local verification never implies production durability or purge safety. Synthetic tests added to existing figure CI; latest CI and real-document validation must be checked, not assumed.

Next: benchmark this pilot on authorized 10-page PDF derivatives, then 778-page PBRER page ranges. Production must retain original-resolution extracted raster/inline images, vector/plot evidence, native text, source table grids, captions and figure/panel links, with page-render fallback for scanned/ambiguous pages only where allowed. Add source-independent OCR and multimodal LLM enrichment from stored evidence, separately versioned and evidence-grounded; never send source data to a provider without approval. DOCX evidence adapter, streaming integration, durability gate, storage policy, throughput sizing and production scheduling remain open. **Do not rerun native extraction or outstanding cloud OCR just to validate this pilot.**

## Objective and safety contract

Extract **3.2 million regulatory PDFs/DOCXs, approximately 4 TB, within 15 calendar days** into PostgreSQL. Preserve original native text, rich formatting, table-cell grids, page/bbox/source version, images/figures and language provenance. Support independent OCR/vision, multilingual translations, semantic chunks and optional embeddings; keep all knobs and destination names configurable.

Non-negotiables: **no silent data loss, no fabricated numbers/table cells, no per-document manual approval, no gratuitous services/tables**. Native extraction must not wait on cloud OCR, translation or vector DB. Source file/version and extraction profile hashes must stay traceable.

Math: 3.2M/(15*86400) = **2.47 docs/sec sustained** fleet-wide. Plan >=5 docs/sec aggregate headroom; this is NOT measured performance. Average 4TB/3.2M = approximately 1.25 MB/file; measure actual document page-count distribution, % scanned, tables, retries, CPU/RAM and GCP quotas before sizing hardware. For separate OCR capacity, 3.2M/15d is approximately **148 documents/minute**, so required Vision image quota is approximately **148 × average OCR image-candidates per document images/minute** before retries/headroom; actual fraction needing OCR, per-asset size and quota must be measured.

## P0 CRITICAL — 15-day source purge / future RAG must remain reconstructible

**New user requirement (2026-11-24):** the original regulatory PDF/DOCX inputs are available only for ~15 days and purged when extraction completes; AFTER deletion, future teams must still be able to generate new RAG applications, including figure/graph, confidence interval, multi-panel microscopy, full-table and section-based summaries. This changes the definition of done: successful native page publication or OCR completion does NOT authorize source deletion.

**Architecture/contract:** [Source Evidence Retention and Purge-Safe Gate](SOURCE_EVIDENCE_RETENTION.md). If policy permits, byte-identical immutable PDF/DOCX source storage with SHA-256 checksum/restore verification is the best future-proof evidence. Store source blobs in an explicitly authorized durable, encrypted, replicated destination (approx 4 TB raw corpus before replicas and derivatives); native page and OCR status records reference that source version. If the purge policy prohibits retaining original copies, capture all legally retainable complete source-derived evidence *before purge* (native spans/grids and full figure rasters/vector evidence, panels, legends, captions, page renderings, etc.) and explicitly flag what cannot be reconstructed later. The proposition 'all imaginable future use cases' cannot be guaranteed with summaries/OCR labels alone, even with perfect extraction of written text.

**Current original 778-page PBRER is NOT purge-safe:** 778/778 native pages published, source table cells retained and 3,901 native semantic chunks previewed only. Live GCP Vision: 104/323 source visual candidates attempted; 78 TEXT_READY and 26 VISUAL_PENDING, with **219 PENDING**; vector-only plot inventory and figure associations are not comprehensively published; independent source-backed figure/full-table interpretation summaries are not yet persisted. No verified durable source archive exists, and original-path-based deferred OCR cannot continue after deletion unless an authorized archive is provisioned.

**Implemented but opt-in/not automatically deployed:** `source_evidence_archive.py` accepts explicit authorization and filesystem root; copies original PDF/DOCX bytes to content-addressed storage with atomic manifest and full SHA-256, idempotent verify after original disappears. Its output always says `durable_storage_verified=false` and `purge_safe=false` because verified filesystem bytes alone are NOT policy approval, replicated storage, or proof of completeness. `deferred_ocr.py` can now resolve missing intake originals from the **explicit** `SOURCE_EVIDENCE_ARCHIVE_ROOT` only after verified source SHA matches. No GCP Storage permission is implied: user only established GCP Vision OCR and Cloud Translation access through gcpkeys.json. Source archive/copy operations are NOT triggered automatically and must not occur without user/organizational approval.

**CI verified:** [Windows/Linux Python 3.11/3.13 and disposable PostgreSQL 45349296665 PASS](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45349296665), [rich-document + synthetic figures 45349296673 PASS](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45349296673), implementation 57a19c1d. Includes source archive/checksum/authorization tests and post-purge OCR source resolution from archived verified bytes. These are disposable synthetic tests, not a live original-source archive or successful external purge.

**Local page-range incident:** user ran `figure_recovery.py inventory --file <downloaded article PDF> --page 117 --end-page 129` and received combined 'Encrypted PDF or invalid inventory page range'; screenshot of the scientific article viewer shows page 7 of 10, likely a 10-PDF-page article whose *printed* page labels are ~1345–1347. Code now reports encryption separately and actual physical page count when requested range is invalid. Verify actual source PDF count before retrying; use 1..N on short article, printed labels are not physical indexes.

**Next P0 decisions:** verify allowed post-purge storage of byte-exact originals and choose a durable managed destination; if disallowed, explicitly design an unavoidable derivative fidelity gap and complete figures/summaries inside 15 days. Add an automated per-document purge-safe integrity gate tied to approved archive + visual/full table coverage, not arbitrary OCR completion. Then run original-file caption/vector/multipanel inventory against physically valid pages and implement source-linked figure/table summaries plus retrievable evidence sidecars. Do not resume full 219 source OCR requests as a substitute for securing source evidence.
## 2026-11-24 — Live 10-page scientific article inventory and corrective refinement

**User-verified source PDF:** exactly 10 physical pages, unencrypted, first printed page label 1340. Read-only original figure_recovery.py inventory --page 1 --end-page 10 returned **28 caption candidates** (NOT 28 confirmed independent figures). Original output included e.g. Figure 1 on physical page 3, Figure 2 on page 4 and Figure 10 on page 9. It also associated the same near-full-page source image (bbox covering almost the complete PDF page) with several captions and counted vector paths around them. This reveals **false/ambiguous caption-to-image associations**; the 28 count cannot be used as true recall or successful figure interpretation.

**Updated figure_recovery.py**: native caption matching now requires a figure-number delimiter (:, period, dash), reducing false matches from prose like "Figure 10 shows..."; page-spanning images (>=70% page coverage) are still reported as original page-level evidence but **not** blindly attached to every nearby figure; PDF vector paths remain retained as spatial *hints*, including zero-thickness plot strokes. Reads page image/vector inventory once per page. Each figure now includes printed PDF page label (e.g. physical 1 = printed 1340). New --compact output prints one short record per caption and counts rather than huge repeated XREF/bbox arrays; default detailed JSON remains accessible, no text printed unless --include-caption. All associations retain GEOMETRY_NEAR_CAPTION_UNVERIFIED status. No source text or image bytes are modified, no DB writes, cloud calls or new schemas.

**Testing:** new synthetic 10-page article with full-page background + genuine chart panel + prose "Figure 1 shows" + printed page label; asserts full-page image is not falsely assigned to individual figures, but preserved in page_spanning_images, and compact report remains read-only. **CI PASS for implementation c0c20e0c**: [cross-platform + disposable PostgreSQL 45350117637](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45350117637) and [rich-document figure unit run 45350109666](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45350109666); verified new full-page-background regression executed and passed. Real original-PDF compact rerun is **pending**. Do NOT quote the eventual compact candidate count as validated semantic RAG figure completeness; it still needs physical figure evidence, panel and caption accuracy review.

**User action after CI passes:** update only figure_recovery.py from the same feature-branch ZIP, keep $OriginalPdf pointing at the verified original 10-page article, and run figure_recovery.py inventory --file $OriginalPdf --page 1 --end-page 10 --compact. Share the short JSON output rather than more screenshots. Do not repeat OCR or native extraction. The hard 15-day purge-safe archive/evidence decision remains unresolved and is higher priority than further OCR volume.

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

CI: **PASS** [cross-platform ingestion and disposable PostgreSQL workflow 45338875585](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45338875585) and [rich-document/100 synthetic PDF workflow 45338875427](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45338875427). Cloud-free tests cover source hash/geometry, Vision fake-response mapping, retry/terminal states, idempotent PostgreSQL enqueue/claim, lease renewal and output versioning. Do not claim original private PDF OCR has run; **NO GCP CLOUD CALLS HAVE BEEN MADE**. Code safety: explicit run scope is now mandatory: `--doc-id` or `--all-docs`.

**Original 778-page read-only OCR discovery PASSED (user-reported 2026-11-24)**: OCR profile validated; exactly 1 document, **323 source-linked visual candidates**, `inserted=0`, `writes=false`; no GCP calls and no schema changes. Next concrete action: use the existing OCR profile to `init --apply` the isolated OCR table, then `enqueue --doc-id <verified doc-id> --apply`; verify 323 candidates and queue statuses. **Final scoped-OCR code CI passed**: [Windows/Linux Python 3.11/3.13 and disposable PostgreSQL 45339898781](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45339898781), [rich document and 100 mixed synthetic PDFs 45339898776](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45339898776). Scoped claim/status tests prevent a single-PDF pilot consuming other documents' queued jobs. No GCP cloud calls. **No `run --apply` until project quota caps/credentials and small pilot are agreed.**

## 2026-11-24 — Real OCR enqueue connection-pool failure and correction

Original 778-page OCR discovery passed with **323 candidates** and zero writes/GCP calls. User then ran OCR init --apply: isolated table **cv_validation_v1.cv_bench_pages_v1_visual_ocr** was created with native_source_unchanged=true. First enqueue --apply failed before asset insert: **TimeoutError: PostgreSQL pool exhausted: max=2 timeout=30s**; source-scoped status returned {}. **Do not rerun init or increase the pool cap**.

Root cause in deferred_ocr.py: iter_documents() and iter_assets() held PostgreSQL connections across Python generator yields; _insert_assets() requested a third connection. Implemented: fetch/read, rollback/close in each iterator *before* yield; preserves 64-document / 32-page bounded pagination and 128-asset upserts with no nested read leases. No changes to source provenance, OCR version, GCP quotas or native page rows.

Regression in tests/test_deferred_ocr.py: test_z_large_enqueue_releases_reads_before_insert_under_pool_cap_2 creates 70 published pages/140 visual assets, forces an effective two-lease cap, asserts 140 first inserts and 0 repeat inserts, and no leases remain. **CI PASSED** for implementation commit 22bfd40a: [Windows/Linux/Python and disposable PostgreSQL 45341046132](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45341046132), [rich-document and synthetic smoke 45341046181](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45341046181). Verified the exact 70-page/140-asset pool-cap-2 regression executed successfully in PostgreSQL CI. Original 778-page retry has not yet been run.

Next local action after downloading the new deferred_ocr.py: no re-extraction and no init. Rerun deferred_ocr.py enqueue for document 2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86, with the same OCR profile and --apply, then scoped status. Expect documents=1, candidates=323, inserted=323 and PENDING=323 on first successful enqueue; if a prior run inserted rows, lower insert count is valid if status count reconciles. No GCP calls until a separate explicit run --apply.

## 2026-11-24 — Original-PDF OCR enqueue recovery completed (USER-VERIFIED)

**Original 778-page PBRER real database outcome:** following the DB lease lifetime fix, the user copied the corrected deferred_ocr.py and reran the isolated queue enqueue. Result exactly: `documents=1`, `candidates=323`, `inserted=323`, `writes=true`. Subsequent source-scoped `deferred_ocr.py status`: `PENDING=323`, `writes=false`. Previous `pool exhausted max=2` failure is resolved on the real original document, not only in synthetic CI. Native PDF content, published pages, source tables and semantic chunks were not modified. **Zero GCP OCR calls to date.**

**Next action — P0 small GCP pilot:** inspect the verified local profile's actual GCP project quota and data-processing approval. With `cv_corpus_deferred_ocr_v1.yml`, run at most 8 jobs / 2 workers with explicit `--doc-id 2721ac39536ef7b439091e3018506b426bda3616c5a76e4271fd29e0b8abfb86` and `--apply`. This **sends source PDF clips to Google Cloud Vision and may incur charges**; do not execute automatically. Collect command statuses, elapsed seconds, source-scoped queue status, confidence/word-polygon coverage and any retry error codes before increasing throttle. The configured 1,500 rpm/1,500 ipm are *not evidence* that GCP grants those quotas. For fleet 3.2M/15d SLA, measure candidate-count distribution and pilot mean/p95 Vision latency before forecasting OCR workers.

Do not rerun native extraction/queue init/enqueue. Existing PENDING=323 and the project-state URL are the handoff checkpoints.

## 2026-11-24 — First live Vision OCR pilot BLOCKED (user-verified)

The original 778-page PBRER OCR queue was at PENDING=323 after successful enqueue. User ran a source-scoped eight-asset/two-worker deferred_ocr.py run --apply. Output: **processed=8, RETRY_WAIT=8, elapsed=1.91 seconds, exit code=0**. Scoped queue afterward: **PENDING=315, RETRY_WAIT=8, TEXT_READY=0, VISUAL_PENDING=0**. These timings are not valid Vision throughput evidence and cloud_calls_possible=true does not prove any API call reached Vision.

**STOP: Do not rerun OCR, re-enqueue or increase concurrency.** Rapid failures may share a local configuration/source/credential error, but cause is unverified. Production observability defect: exit code 0 despite zero successful OCR recognition. Next immediate action is a read-only query of existing cv_validation_v1.cv_bench_pages_v1_visual_ocr rows filtered by doc_id and OCR semantic fingerprint, grouping error_code/error_message/attempts without printing PDF text. Determine whether the failure is pre-provider (source rendering, local dependency, quota limiter, credentials) or provider-side. There is no basis to choose a code fix until the underlying stored error is inspected.

**Current blocker: waiting for OCR error_code/error_message.** Retain retry state and source provenance; no blanket retries. Record the observed error, corrective commit, CI and retest outcome here.

## 2026-11-24 — OCR root cause and credential boundary (USER-VERIFIED)

The first eight-asset Vision OCR pilot returned RETRY_WAIT=8 in 1.91s. Read-only PostgreSQL error query isolated ModuleNotFoundError: No module named 'google' for all eight assets, one attempt each. This is a missing Python SDK in the local validation virtual environment, NOT evidence that Google Cloud rejected the credentials or that OCR inputs were invalid. Queue remains PENDING=315 and RETRY_WAIT=8; no retries yet.

Actual permitted external scope: the user has one gcpkeys.json service account, and ONLY Google Cloud Vision OCR and Cloud Translation are eligible if the service account, project and enabled APIs allow them. Do NOT introduce Document AI, Vertex AI, Azure Document Intelligence, new API keys or assumed cloud providers. A key does not prove service enablement or quota.

Next: install google-cloud-vision, google-cloud-translate and google-auth in the existing .venv-validation interpreter (requirements.txt already lists google-cloud-vision). Verify SDK imports and auth without transmitting any private PDFs; an optional tiny synthetic text/image call can establish API-level permissions, subject to minor possible charges. Never print private keys, access tokens or service-account email. Do not retry queued jobs before local SDK and access checks pass.
## 2026-11-24 — Authorized GCP capability probe PASS; recovery pilot scoped

User executed a small synthetic live probe using existing gcpkeys.json. Project: **rehdocreader**; **Google Cloud Vision OCR AVAILABLE** and detected the exact synthetic text 'VISION TEST 123'; **Google Cloud Translation AVAILABLE** and translated 'Hello world' to 'Bonjour le monde'. This proves both SDK paths can authenticate and call these two APIs with the current key. No Document AI, Vertex AI or any other cloud providers/keys are available; do not suggest or implement them. No private PDF was sent during this probe.

Root cause of earlier eight RETRY_WAIT jobs was local missing google Python package. With SDK installed and Vision probe passing, **do not repeat enqueue or OCR init**. Existing original-PBRER queue retains 315 PENDING and 8 RETRY_WAIT with attempts=1. Prior run --max-jobs 8 without a retry filter would consume *new PENDING jobs first*, because claim sorts by next_attempt_at. Production safety fix in deferred_ocr.py: opt-in --retry-only is only allowed for run with explicit --doc-id; claim filters to eligible RETRY_WAIT rows and leaves PENDING untouched. Run exit code is nonzero when any RETRY_WAIT or FAILED_TERMINAL remains, fixing misleading exit=0 for full failure. Regression test creates one retry and one pending job and asserts only failed job is leased/processed.

Next execution after updated deferred_ocr.py and passing CI: run only the original eight RETRY_WAIT assets with --doc-id ... --retry-only --max-jobs 8 --workers 2 --apply. The call **sends original regulatory source clips to Google Vision**, is subject to the existing project's data-processing authorization and may incur charges. Measure actual wall-clock seconds, success statuses, confidence/word polygon quality, and errors. Do not scale to all 323 before evaluating results and actual quotas. If zero processed because retry timer has not matured, use read-only next_attempt_at to determine readiness.
**SAFE RETRY-ONLY FIX TESTED** (2026-11-24): [Windows/Linux Python 3.11/3.13 and real disposable PostgreSQL CI 45343815239](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45343815239) **PASS**, and [rich-document CI 45343815324](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45343815324) **PASS**, commit 1d96adff. Specifically verified test_retry_only_claim_does_not_consume_pending_ocr_assets executed successfully in PostgreSQL CI. Production requirements.txt now explicitly lists google-cloud-vision, google-cloud-translate, and google-auth (commit 4b028a40). New retry-only CLI exists only in updated deferred_ocr.py; older local copies DO NOT support it. Real original PDF retry after package repair not yet executed.
## 2026-11-24 — Original 778-page Google Vision retry-only pilot SUCCESS (user-verified)

User executed newly tested --retry-only run with --doc-id, --max-jobs 8, --workers 2 and --apply after fixing local Google SDK and confirming project rehdocreader Vision/Translation support. Actual output: **processed=8; TEXT_READY=6; VISUAL_PENDING=2; elapsed=3.98 seconds; exit_code=0**. Subsequent status: **PENDING=315, TEXT_READY=6, VISUAL_PENDING=2, RETRY_WAIT=0**. Thus all eight previously failed assets were processed; the 315 fresh PENDING assets were left untouched. Original source pages/geometry/table grids are unaffected. Credential scope remains gcpkeys.json with ONLY GCP Vision OCR and Cloud Translation, no Document AI/Vertex AI/Azure.

Interpretation: six source regions yielded OCR text; two returned no suitable/adequately confident text and retain automatic VISUAL_PENDING status and source references. This is NOT a 75% clinical correctness score or evidence of complete chart/table interpretation. 8/3.98=~2.0 assets/sec wall-clock is only one tiny pilot, not sustained production throughput; 15-day 3.2M-document deadline remains unproven.

Next: run a **bounded fresh PENDING 32-asset/4-worker batch** for this document, without --retry-only, after confirming data-processing authorization/project quota (previous real source request succeeded). Measure total elapsed and resulting status counts; expect exactly 283 PENDING if 32 are claimed successfully and no other worker runs; TEXT_READY/VISUAL_PENDING distribution is empirical. Do not rerun init/enqueue/extraction. Do not OCR VISUAL_PENDING again with the same model without a distinct enrichment strategy. Keep original PDF source and OCR evidence; implement versioned native/OCR retrieval overlay separately. Compare sample confidence and OCR region provenance before full-scale launch.
## 2026-11-24 — Real GCP Vision 32-asset controlled batch PASSED (user-verified)

The source-scoped original 778-page PBRER batch ran with **--max-jobs 32, --workers 4**, using the existing GCP Vision credentials for project rehdocreader. Real output: **processed=32**, **TEXT_READY=21**, **VISUAL_PENDING=11**, zero retry/terminal errors, **wall time=4.58 seconds**, exit_code=0. Observed short-batch throughput **32/4.58=6.99 assets/second** (NOT sustained fleet throughput). The Vision client initialized four times across threads; check lazy client initialization if optimizing throughput later, but no evidence this causes incorrect output.

The cumulative original document OCR table reconciles exactly **323 assets**: **PENDING=283, TEXT_READY=27, VISUAL_PENDING=13, RETRY_WAIT=0**. First 8-asset pilot contributed 6 text and 2 visual-pending. The 32-asset batch contributed 21 text and 11 visual-pending. The 40 processed assets have 27/40=67.5% OCR text-ready; this is NOT a measure of numerical/clinical accuracy, chart understanding, or OCR recall. VISUAL_PENDING is an honest explicit unresolved-visual state and should not be silently converted to TEXT_READY.

**Next engineering gate:** a zero-write quality/distribution check on the 27 TEXT_READY and 13 VISUAL_PENDING OCR records: count languages, confidence histogram, source bbox/word-region coverage, error_code by kind; do not print private transcription. Identify whether visual-pending reflects empty image, figures, low confidence, or other category, and confirm word regions preserve page/asset provenance. Keep the 283 pending queued, do not re-enqueue or rerun native extraction. After the source-grounded QA gate, a larger bounded source-scoped 64–128-job run using existing four workers can establish more representative latency and quota behavior; verify shared GCP quotas rather than extrapolating from 4.58 s. OCR-to-semantic-chunk versioned overlay, grounded table/chart summaries and the 3.2M/15d sustained throughput proof are still outstanding.

## 2026-11-24 — Real OCR quality aggregate: OCR success != interpreted visual (user-verified)

The source-scoped read-only SQL quality query on the original 778-page PBRER reconciles all 323 assets: PENDING image=261, PENDING vector_region=22; TEXT_READY image=27, mean confidence=0.967, mean word-level OCR regions=1.0; VISUAL_PENDING image=9 and vector_region=4, all NO_OCR_TEXT with zero confidence and zero OCR regions. Among 27 text-ready assets, detected language is unknown for 24 and en for 3. No source document text or secrets were printed.

Source-code interpretation verified against vision_ocr._parse_batch_ocr: with bbox_detail=word, each ocr_regions element represents a recognized word, so 1.0 mean is a **sparse label-level OCR result**, not evidence of complete charts, tables, annotations, nearby native text or clinical meaning. Mean word confidence=0.967 measures confidence on those sparse recognized words only. Language is currently obtained from page.property.detected_languages; missing metadata does not prove the 24 instances are non-English or inaccurately recognized. Nothing indicates 27 trustworthy complete-text figures.

Decision: native page/table source remains immutable; do not try to reconstruct 4-page clinical tables from one-word OCR or claim an LLM visual-summary service that the user cannot access. Preserve OCR word polygons and confidence as citation-level sidecars; integrate TEXT_READY labels into retrieval only when linked to their original image/page, without duplicating native text, and keep VISUAL_PENDING opaque for later explicit evidence-based vision handling. External integrations permitted: existing gcpkeys.json Google Cloud Vision OCR and Cloud Translation only.

Next practical engineering step: continue source-scoped bounded OCR with 64 *fresh* PENDING assets and four workers (NO --retry-only), subject to existing GCP quota and regulatory data-processing authorization. This will produce another more meaningful batch-duration/status sample without broad expansion. Measure p50/p95 across multiple batches before claiming throughput. In parallel prioritize versioned source-cited OCR sidecar/semantic overlay; avoid another round of global chunk tuning or re-extraction. Remaining 283 PENDING (261 images, 22 vector regions) as of this report. 15-day corpus SLA is still unproven.
## 2026-11-24 — Live 64-asset Vision OCR run plus uploaded regulatory-figure review

**Real source-scoped 64-asset run** on the original 778-page PBRER using 4 OCR workers: **processed=64, TEXT_READY=51, VISUAL_PENDING=13, elapsed=6.51 seconds, exit=0**, with no retry failures. Observed short-batch average 64/6.51 = **9.83 source assets/sec**, not sustained throughput or a measure of interpretation accuracy. The verified cumulative original-source queue is **PENDING=219, TEXT_READY=78, VISUAL_PENDING=26**, exactly **323/323** accounted for. Across 104 processed assets, 78 returned some OCR text and 26 legitimately remained visual-pending; this 75% transcription presence is NOT graph completeness or numeric correctness. Native source unchanged.

**User-supplied figure examples** show a coverage gap beyond OCR labels: PBRER Topiramate Figures 1 and 3 reporting-rate time series with error bars, Figure 2 regression with confidence boundaries and clinical safety narrative (printed pages 117, 118, 129); scientific-glass-vial article pages printed 1345–1347 with multi-panel plots, pH/particle series, log-scale D-SIMS spectra, SEM micrographs and dense figure captions (Figures 3–13). Need preserve caption, source page, panel boundaries, axes/legend/units, figure numbering, discussion links, source raster/vector evidence, and any explicit statistical conclusions from NATIVE text. Never estimate values or confidence limits by eyeballing PDFs. OCR high-confidence 1-word labels do not reconstruct these.

**Original extraction coverage risk:** pdf_processor._preserve_visuals() enumerates PDF embedded images and groups raster strips; unrelated PDF vector path charts may have *no image XREF* and therefore may be absent from the deferred visual queue. The 323 OCR jobs DO NOT establish 100% figure recall. Existing empty-table vector_region candidates are not a comprehensive vector-chart inventory.

**Implemented independent read-only figure evidence inventory** in existing figure_recovery.py (`inventory` CLI), no new tables/cloud calls/re-extraction: native caption-driven Figure/Fig. labels with source caption SHA256, figure number, exact PDF byte-hash/page provenance and source geometry evidence; detects nearby embedded raster images AND PDF vector drawing path counts (including degenerate horizontal/vertical plot strokes); labels associations as UNVERIFIED, no inferred curves/numerics, masks caption text by default. At most 50 pages per inventory call. Handles caption-above PBRER vector plots and caption-below multi-panel scientific images on synthetic PDF regression tests, without mutating original bytes. Corrected vector-line detection; [full Windows/Linux/PostgreSQL CI 45346942488 PASS](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45346942488) and [rich document/figure synthetic CI 45346942391 PASS](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45346942391), commit 1005093c.

**Critical limitation**: inventory is candidate evidence only, NOT a persisted figure RAG index or confirmed figure/graphic association; scanned caption-only pages without native text are not yet discovered by this caption-first route. The original PDF remains authoritative. Before claiming lossless visual RAG, verify original-PDF caption/vector/raster figure coverage, physical-vs-printed page mapping, two-column figure association and multi-panel grouping; then add a versioned retrieval overlay linking original figure/page/caption/narrative evidence to existing semantic chunks and OCR sidecars without duplicating text or fabricating plot values. For queries needing numeric chart data beyond printed text, return source figure with uncertainty rather than infer graph coordinates.

Next user action: download latest feature-branch ZIP and use `figure_recovery.py inventory --file <original_778_page_pdf> --page <physical_pdf_page> --end-page <physical_pdf_page_plus_up_to_49>` as a **read-only, no-cloud** figure source audit around user-shown PBRER figures; actual physical PDF page indices may differ from printed footer labels 117/118/129. No rerun of native page extraction, OCR init/enqueue or previous OCR jobs. Hold remaining PENDING=219 while verifying source-visual coverage.
## What is NOT done

- **Deferred OCR live provider operation is confirmed** (104 assets processed; 78 text-ready, 26 visual-pending, 219 pending on original 778-page PBRER). **Still not retention-ready**: native/OCR text is not merged into versioned semantic retrieval, chart summaries and complete multi-panel figure evidence are missing, and current local-source paths will disappear after purge without an explicitly authorized durable archive.
- **No full semantic summaries of reconstructed multi-page tables** yet. Existing table_structure_summary is a source-grounded structural description (columns, row counts/pages), not a clinical/numeric conclusion. Later optional async grounded summary stage must use whole logical table, cite cells/pages and undergo accuracy evaluation.
- **No streaming large-document materialization** yet: extracted_source.load_extracted_document collects all source pages with rows.extend, then prepare_document/save_document hold all chunks. Implement bounded sliding-window/two-pass or spool processing before 25k-page stress. Preserve deterministic neighbor IDs and transaction atomicity.
- The 15-day SLA is **not validated**. Stage parse dominates the pilot. Next original-document run should use substage timing and A/B profiling before switching table detector or reducing strictness. Scale nodes/workers based on fleet file/page distributions and p95 throughput, not a single 778-page PDF.

## Execution priorities — no detours

1. **P0 quality and throughput**: confirm latest HEAD CI and integrated real-document preview quality report, validate source coverage/section links/4-page table relationships. Fix actual fragmentation or boundary error in code; keep source pages untouched.
2. **P0 performance**: profile PDF table detection vs cell extraction using new counters, compare process workers/CPU/RSS/SQL with same original-PDF fixtures and profiles. Optimize measured bottleneck without loosening source fidelity.
3. **P0 source durability before purge**: confirm lawful source-archive retention, approved durable replicated storage or required complete derivative evidence, and per-document PURGE_SAFE integrity gate; then finish 219 remaining OCR candidates plus figure/table semantic summaries and retrieval overlay. Do not re-init/enqueue or rely on local intake paths after purge.
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
