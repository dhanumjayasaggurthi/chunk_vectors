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
