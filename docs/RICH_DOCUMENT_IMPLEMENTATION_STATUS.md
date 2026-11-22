# Rich document v2 — implementation status and production gates

This branch is an **opt-in foundation**, not a production-certified end-to-end rollout.

## Implemented

- Native PDF font runs captured in the existing PyMuPDF text pass when `extraction.capture_rich_text: true`; no second PDF pass or cloud call for native styles.
- Rich runs, inferred heading hints and OCR language persisted in staged page `elements` JSONB, then reconstructed by `extracted_source`.
- Content-affecting rich-capture setting included in extraction fingerprint; previously staged legacy pages remain unchanged until the new profile is selected.
- Safe HTML projection of bold/italic and headings with escaped source content.
- Independent PostgreSQL translation work table, atomic SKIP LOCKED claims, leases, idempotent source/version keys, stale-worker fencing and bounded parallel translator execution.
- Original and English text composition for native, English and bilingual embedding inputs.
- Unit tests and a configurable, offline mixed-content synthetic PDF stress harness.

## Not implemented / production blockers

1. The translation table is a **building block**: no cloud translation provider, real provider quota controller, materializer output-column migration, or automatic stage wiring has been added. Do not assume translation runs as part of normal ingestion.
2. The existing page publication path still deletes/reinserts full-document rows. A versioned immutable manifest and atomic active-pointer swap are required for 25,000-page documents.
3. Existing rechunking still accumulates all extracted pages and chunks for one document in memory. Implement streaming bounded materialization before 25,000-page production processing.
4. Page extraction remains document-worker parallel, but page parsing within a single document is sequential. To parallelize large documents safely, use independently leased page ranges with separate PDF handles **per worker**; never share a PyMuPDF handle across threads.
5. The existing document-level worker scheduler is not a persistent distributed work queue. Add PostgreSQL task leases, heartbeats and source-version fences before a 3.2M-document rollout.
6. Tables, charts, image OCR, cross-page semantics, scanned typography, DOCX and image assets require further validation or new adapters.
7. Synthetic generated PDFs do **not** substitute for the requested **100 real large regulatory documents**. No such real corpus is available in this repository. Obtain an authorized representative corpus, including native/scanned/multilingual PDFs and DOCX, with figures, tables, images, and mixed same-page content.
8. No end-to-end production throughput, latency, RSS, OCR accuracy or translation-quality SLA has been established.

## Opt-in example

```yaml
extraction:
  enabled: true
  capture_rich_text: true
  page_batch_size: 32
  ocr_workers: 8
  chart_workers: 4
```

Add `capture_rich_text: true` to a valid existing extraction profile; all other required profile settings remain unchanged.

## Validation

Cloud-free unit tests:

```bash
python -m unittest discover -s tests -p test_rich_document.py -v
```

Synthetic stress (100 PDFs, 12 mixed-content pages each by default):

```bash
python tests/stress_rich_document.py --documents 100 --pages-per-doc 12 --workers 8
```

Large synthetic test (very expensive; explicitly opt-in):

```bash
python tests/stress_rich_document.py --documents 100 --pages-per-doc 500 --workers 8
```

The stress harness deliberately does not call paid OCR, translation, or embedding APIs and does not test the PostgreSQL stage's checkpoint/recovery path. The workflow dispatch job is manual to avoid consuming CI resources unexpectedly.

## Next engineering gates

- **Gate A — correctness:** PostgreSQL integration tests for stage persistence, rich-run round-trip, partial failure/resume, stale lease, translation retry, and atomic publication.
- **Gate B — throughput:** independent page-window leases, bounded process workers, persistent distributed scheduling, shared OCR/translation quotas, connection pooling, metrics.
- **Gate C — fidelity:** compare 100 real large regulatory documents with gold annotations; verify typography, native language, numbers/units, tables/charts and source provenance.
- **Gate D — production:** 25,000-page soak tests, peak RSS and DB WAL budgets, failure injection, per-stage costs, staged rollout and rollback.

Do not merge into the production ingestion path until these gates pass.

## Real-corpus benchmark command

After an authorized corpus of at least 100 substantial PDFs is mounted, run:

```bash
python tests/benchmark_regulatory_corpus.py --corpus /approved/regulatory --min-documents 100 --min-pages 100 --workers 8 --report benchmark_results.json
```

This reports per-document page extraction errors and native text/style/table/image counts. It explicitly fails if the minimum document/page requirements are not met, or if DOCX files are present without being tested. It is **not** an OCR, translation, source-fidelity, or 25,000-page acceptance test; those need separately instrumented full-pipeline validation.


## Verified public regulatory PDF benchmarks (2026-11-22)

- **100 authentic CFR PDFs, sampled:** 100 unique downloaded sources, each with at least 100 pages; combined source inventory **80,312 pages**. Sampled **12 per PDF (1,200 pages)**. **0 page errors**. This is explicitly NOT a 100 full-document test. [Source workflow](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45065699998).
- **5 authentic CFR PDFs, full:** **2,702/2,702 pages**, **0 page errors**, 205 detected table pages, 17 image pages, 2,691 pages with native style spans; elapsed time **245.844s including downloads**. Same [source workflow](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45065699998); its overall conclusion was failure because a separate illustrated-document source test returned incomplete results, not because the five CFR volumes failed.
- **NIST IR 8524, full:** originally 6 errors on pages 46/48/61/78/96/101, all due to tiny word-empty graphical subregions (1,034–2,460 pt²) nested inside successfully extracted larger tables. A guarded classifier now preserves a 144-DPI bounded PNG for each such region while keeping source bbox and a review flag. Updated **150/150 pages, 0 parser errors** in **15.896s**, 35 pages with detected tables, 78 pages with detected images or vector regions, and 20 pages with text+table+visuals. [Retest](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45066891902).

**Safety policy:** The page may be checkpointed with a recovered preview, but the PDF stage refuses to publish documents containing unreviewed \`vector_region\` elements by default (\`REVIEW_REQUIRED\`). Manual acceptance can opt into \`extraction.publish_unreviewed_vector_regions: true\`; this setting does not invalidate extraction fingerprints or re-extract the document. An automated real PostgreSQL integration test verifies checkpoint retention, default publication refusal, and approval without rerunning extraction.

**Full 100-source benchmark:** Four independent shards of 25 official CFR PDFs each have been launched with a spawned four-worker PDF process pool, worker recycling and complete-document extraction (\`max_pages=0\`). [Run 45067297563](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45067297563). Do not mark complete until all 100 per-document artifact reports and digests have been checked. Some other source hosts (FDA / GAO) block GitHub runner downloads; public GovInfo and NIST sources were verified.

### Additional throughput and translation improvements

- Benchmarks support disjoint 25-document shards, process isolation, worker recycling, source SHA-256, and per-worker peak RSS measurements.
- Translation stage now supports batched \`execute_values\` queue insertion, language-sensitive source hashes, bounded remote RPC concurrency, durable exponential retry cooldown, lease expiry recovery, stale-owner fencing and lease heartbeat; **no cloud translation provider has yet been integrated**.
- Checkpoint tests run against real isolated PostgreSQL in CI. Native PDF extraction benchmark success is not proof of complete regulated evidence fidelity or provider OCR quality.

**Known limitation:** A small PNG preview is raster evidence, not the original editable vector drawing or a complete image asset pipeline. Big page artifacts, OCR imagery, figures, charts, DOCX and all styles still need a dedicated immutable object-storage representation. The currently published page-table delete/insert and full-document in-memory materializer remain production scale blockers.
