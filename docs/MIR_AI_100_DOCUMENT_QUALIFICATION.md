# MIR-AI 100-document qualification runbook

Branch: `feature/MIR-AI-2026-09-12`

This runbook defines how to execute a controlled 100-document qualification against NAS or S3 and how to distinguish **successful ingestion** from **measured business accuracy**. It is intentionally conservative: do not infer compliance from a green run alone.

## 1. Purpose

Use a representative set of 100 real documents to answer two separate questions:

1. Does the MIR-AI pipeline complete end-to-end on realistic documents and infrastructure?
2. Does the output meet the extraction/chunking/metadata accuracy requirements when compared with accepted ground truth?

A document reaching `ACTIVE` proves the generation completed and was activated. It does **not** prove that table extraction, metadata extraction, chunk boundaries, or BBOX values are >=99% accurate.

## 2. Source-backed qualification targets

The supplied MIR-AI requirements define the following relevant targets:

- PDF and DOCX ingestion.
- Text, tables, images, charts/graphs, and OCR for scanned/image content.
- Page references and table/image BBOX where the source format provides reliable coordinates.
- Tables extracted in full and table + generated summary retained as one logical chunk.
- Heading/hierarchy-aware semantic chunking.
- Normal chunk target: 1200-1500 tokens.
- Chunk overlap: 100 tokens.
- Avoid splitting semantic units such as sentences, paragraphs, tables, lists, and code blocks.
- Repeated header/footer removal.
- `text-embedding-3-large`, 3072 dimensions, cosine similarity, Azure OpenAI API version `2025-04-01-preview`.
- RimDocs metadata is authoritative.
- Requested fields already supplied by RimDocs must not be re-extracted with the LLM.
- Missing requested metadata is extracted from target sections, with the first 20 pages as fallback when those sections are not found.
- Missing/null values are explicitly flagged.
- Target table/metadata extraction accuracy >=99% and chunking/metadata error <=1%, measured against an accepted labelled/golden dataset.

## 3. Test-environment rule

Do not use existing production output tables for the first 100-document qualification.

Use a dedicated PostgreSQL schema, for example:

```ini
[database]
schema = mirai_uat_100
```

or the corresponding `[POSTGRES]` section if that is what the environment already uses.

MIR-AI creates additive `mirai_*` objects in the configured schema. Keep the legacy pipeline and production data untouched during qualification.

## 4. `config.ini`

Continue using `config.ini`. Preserve the existing sections and credentials. Add a dedicated MIR-AI section rather than rewriting the rest of the file.

Recommended qualification baseline:

```ini
[MIR_AI]
page_workers = 4
max_inflight_pages = 8

doc_workers = 2
max_inflight_docs = 4

vision_concurrency = 16
chat_concurrency = 6
embedding_concurrency = 8

lease_seconds = 900
heartbeat_seconds = 60

ocr_render_dpi = 200
scanned_text_threshold = 100

embedding_model = text-embedding-3-large
embedding_dim = 3072
embedding_api_version = 2025-04-01-preview
embedding_batch_size = 16

chunk_target_min_tokens = 1200
chunk_target_max_tokens = 1500
chunk_overlap_tokens = 100

table_summary_workers = 4

vector_index_mode = exact
enable_embeddings = true

log_level = INFO
```

Do not commit `config.ini`; it may contain credentials or environment-specific values.

`vector_index_mode = exact` is the accuracy-first qualification default. `halfvec_hnsw` must not be enabled merely for speed before retrieval-quality qualification.

## 5. Initialize the UAT schema

Run once:

```bash
python mir_ai_main.py --config config.ini --init-db
```

Do not proceed to 100 documents if schema initialization fails.

## 6. Required authoritative RimDocs handover for batch mode

Batch mode requires `--rimdocs-jsonl`.

This is intentional. The requirements state that RimDocs is authoritative and that LLM extraction is only for requested fields absent from RimDocs. Treating all requested fields as missing because the authoritative source was unavailable would invalidate the test.

Expected JSONL shape for NAS:

```json
{"canonical_path":"/nas/MIR_AI_UAT_100/study001.pdf","metadata":{"Study ID":"ABC-001","Compound Number":"CMP-123","Species":"Rat"}}
{"canonical_path":"/nas/MIR_AI_UAT_100/study002.pdf","metadata":{"Study ID":"ABC-002","Compound Number":"CMP-456"}}
```

Expected JSONL shape for S3:

```json
{"canonical_path":"s3://your-bucket/MIR_AI_UAT_100/study001.pdf","metadata":{"Study ID":"ABC-001","Compound Number":"CMP-123"}}
```

`canonical_path` must match the path produced by the NAS/S3 batch source exactly.

## 7. Qualification sequence

Do not start with all 100 documents immediately.

Use this progression:

1. One real document.
2. Ten representative documents.
3. All 100 qualification documents.
4. Inspect failures and incorrect outputs.
5. Fix measurable defects without weakening evidence/provenance checks.
6. Re-run the same qualification corpus.

This avoids wasting API quota and makes infrastructure/configuration errors easier to isolate.

## 8. Single-document smoke test

Example NAS/PDF smoke test:

```bash
python mir_ai_main.py \
  --config config.ini \
  --file "/nas/MIR_AI_UAT_100/study001.pdf" \
  --canonical-path "/nas/MIR_AI_UAT_100/study001.pdf" \
  --rimdocs-json "/test-data/study001.rimdocs.json"
```

Before continuing, inspect the resulting page records, chunks, metadata, embeddings, logs, and generation state.

## 9. NAS 100-document run

Prefer a dedicated directory containing exactly the qualification set:

```text
/nas/MIR_AI_UAT_100/
    study001.pdf
    study002.pdf
    study003.docx
    ...
```

Run:

```bash
python mir_ai_main.py \
  --config config.ini \
  --root "/nas/MIR_AI_UAT_100" \
  --max-files 100 \
  --rimdocs-jsonl "/test-data/rimdocs_uat_100.jsonl"
```

A dedicated directory is preferred to pointing at the full archive with `--max-files 100`, because the first 100 discovered files may not be representative.

## 10. S3 100-document run

Prefer a dedicated S3 prefix containing exactly the qualification set.

Example configuration:

```ini
[S3]
bucket = your-bucket
prefix = MIR_AI_UAT_100/
region = us-east-1
profile = your-profile
temp_dir = /large/local/scratch
scratch_reserve_mb = 2048
```

Run:

```bash
python mir_ai_main.py \
  --config config.ini \
  --s3 \
  --max-files 100 \
  --rimdocs-jsonl "/test-data/rimdocs_uat_100.jsonl"
```

S3 documents are downloaded to controlled scratch space, processed from the local copy, SHA-256 hashed before generation creation, and cleaned up after processing.

## 11. Recommended 100-document corpus composition

The 100 documents should be deliberately representative rather than convenient. Include, where available:

- native/digital PDFs,
- scanned PDFs,
- mixed native + scanned pages,
- PDF reports with dense tables,
- multi-page tables,
- raster images,
- vector charts/graphs,
- image-heavy pages,
- DOCX files,
- long reports,
- documents where RimDocs contains nearly all requested fields,
- documents where many requested fields are absent from RimDocs,
- difficult numeric values and units,
- reports with repeated headers/footers,
- at least a few malformed/corrupt or known-problem files for fault handling.

A convenient or homogeneous 100-document set cannot establish general quality for the 69k-document corpus.

## 12. Initial database checks

Examples below assume schema `mirai_uat_100`.

### Generation status

```sql
SELECT status, COUNT(*)
FROM mirai_uat_100.mirai_generations
GROUP BY status
ORDER BY status;
```

For a completely successful run, all 100 intended documents should ultimately have an active generation. Investigate every failure rather than excluding it from the denominator without an agreed reason.

### Document count

```sql
SELECT COUNT(*)
FROM mirai_uat_100.mirai_documents;
```

### Total active pages/chunks

```sql
SELECT
    COUNT(*) AS generations,
    SUM(pages_total) AS pages,
    SUM(
        (
            SELECT COUNT(*)
            FROM mirai_uat_100.mirai_chunks c
            WHERE c.generation_id = g.generation_id
        )
    ) AS chunks
FROM mirai_uat_100.mirai_generations g
WHERE status = 'ACTIVE';
```

### Missing embeddings

```sql
SELECT COUNT(*) AS missing_embeddings
FROM mirai_uat_100.mirai_chunks
WHERE embedding IS NULL;
```

With embeddings enabled, active production-quality chunks should not silently carry missing embeddings.

### Metadata source/status distribution

```sql
SELECT
    source,
    status,
    COUNT(*)
FROM mirai_uat_100.mirai_metadata
GROUP BY source, status
ORDER BY source, status;
```

### Failed or incomplete generations

```sql
SELECT
    d.canonical_path,
    g.status,
    g.stage,
    g.error_message,
    g.pages_total
FROM mirai_uat_100.mirai_generations g
JOIN mirai_uat_100.mirai_documents d
  ON d.doc_id = g.doc_id
WHERE g.status <> 'ACTIVE';
```

## 13. Requirement-by-requirement qualification matrix

| Area | Can the 100-document run test it? | Qualification method |
|---|---:|---|
| PDF ingestion | Yes | Compare page/content output with source |
| DOCX ingestion | Yes | Compare logical content with source |
| Native text | Yes | Source vs persisted page/chunk text |
| Scanned OCR | Yes | Compare OCR with labelled/manual truth |
| Images | Yes | Verify required evidence extraction/provenance |
| Charts/graphs | Yes | Compare extracted visible facts with source |
| Tables | Yes | Compare every expected table/cell |
| PDF table BBOX | Yes | Compare page/coordinates with labelled truth |
| DOCX rendered BBOX | No, not from XML alone | Requires approved rendered-DOCX service |
| Multi-page tables | Yes | Verify continuation and complete content |
| Table + summary one logical chunk | Yes | Inspect chunks |
| Heading hierarchy | Yes | Inspect `section_path`/semantic structure |
| Header/footer removal | Yes | Compare source and chunks |
| 1200-1500 token normal chunks | Yes | Token-count generated chunks |
| 100-token overlap | Yes | Inspect adjacent normal chunks |
| Semantic-unit preservation | Yes | Label/check split boundaries |
| 3072-dimensional embeddings | Yes | DB/vector validation |
| Cosine retrieval | Yes | Retrieval qualification queries |
| RimDocs precedence | Yes | Confirm business values are not overwritten |
| Only missing fields extracted | Yes | Metadata audit |
| 50-field registry | Yes | Schema/completeness audit |
| Evidence/page provenance | Yes | Inspect metadata/chunk evidence |
| Missing/null flags | Yes | Inspect status fields |
| Crash/restart recovery | Yes | Intentionally stop/restart worker |
| Very-large-document scaling | Separate stress test | Use 12k+ page representative/synthetic documents |
| >=99% table/metadata accuracy | Only with accepted ground truth | Automated/manual comparison |
| <=1% chunking/metadata error | Only with accepted ground truth | Automated/manual comparison |
| Multilingual production policy | Not fully | Business decision remains open |
| PGVector vs Elastic final selection | No | Deployment/business decision |

## 14. Successful ingestion is not an accuracy result

Do not report `100 ACTIVE` as `100% accurate`.

`ACTIVE` means the pipeline completed its guarded stages and activated the generated output. Accuracy must be calculated by comparing that output with accepted expected results.

Examples:

```text
Table extraction accuracy = correctly extracted expected tables / expected tables
Metadata accuracy         = correct evaluated metadata values / evaluated metadata values
Metadata precision        = correct extracted values / extracted values
Metadata recall           = correct expected values found / expected values
Chunk boundary error      = incorrect semantic boundaries / evaluated boundaries
BBOX accuracy             = correctly localized expected objects / expected objects
```

Define exact scoring semantics with Business/GRA before certifying the contractual target.

## 15. Minimum golden/labelled data needed for formal accuracy scoring

For each qualification document, record enough accepted truth to evaluate relevant requirements, for example:

- expected document/page count,
- expected tables and page ranges,
- canonical table cells/content,
- expected PDF table BBOX where BBOX is being scored,
- expected metadata field values,
- fields expected to come from RimDocs,
- fields expected to remain missing/null,
- evidence page(s) for extracted metadata,
- accepted semantic/chunk boundaries for a representative subset,
- representative retrieval queries and accepted relevant chunks.

The golden data must be independent of the pipeline output. Do not create the expected answer by copying what MIR-AI produced and then score MIR-AI against itself.

## 16. Formal 100-document report

The desired qualification report should eventually provide metrics similar to:

```text
Documents intended:              100
Documents ACTIVE:                100
Documents failed:                  0

Expected tables:                 387
Correct tables:                  384
Table extraction accuracy:      99.22%

Metadata values evaluated:      4267
Correct metadata values:        4241
Metadata accuracy:              99.39%

Semantic boundaries evaluated:  1326
Boundary errors:                   8
Chunk boundary error rate:       0.60%

Embedding dimension:             3072 PASS
Missing active embeddings:          0 PASS
RimDocs precedence violations:      0 PASS
Unsupported/fabricated evidence:    0 PASS

OVERALL REQUIREMENT GATE:        PASS / FAIL
```

Do not invent these numbers. They must come from the actual qualification dataset and scorer/review.

## 17. Restart/failure test

The 100-document qualification should include at least one controlled interruption:

1. Start a document/batch.
2. Stop the worker while a large document is being processed.
3. Restart the same command.
4. Verify previously persisted page progress is reused.
5. Verify no active prior generation was destructively deleted.
6. Verify no pages/chunks become silently skipped because completions arrived out of order.

Also test, where practical, transient API errors, unavailable DB connection, insufficient scratch space, and duplicate claims.

## 18. Qualification completion rule

A 100-document run is **software/infrastructure successful** when all intended documents are accounted for, generation failures are understood, required output objects exist, and the pipeline does not silently lose data.

A 100-document run is **requirements-qualified** only when the generated output has been compared with an accepted independent golden set and the measured metrics satisfy the agreed acceptance criteria.

Until that comparison exists, report the result as `ingestion completed / qualification pending`, not `>=99% accurate`.

## 19. Known qualification boundaries

- The live production RimDocs API/table mapping was not supplied; batch qualification therefore uses the explicit authoritative JSONL provider boundary.
- Exact rendered DOCX page/BBOX geometry cannot be derived reliably from DOCX XML alone and must not be fabricated.
- The requirements leave PGVector versus Elastic open. The branch supports PGVector; `exact` full-precision cosine is the qualification default.
- The multilingual production decision remains open in the supplied requirements.
- CI/software tests do not replace Business/GRA golden-set accuracy measurement.

## 20. Next automation recommended

Add a dedicated qualification scorer that accepts the golden dataset plus a generation/schema identifier and emits one reproducible machine-readable + human-readable report containing:

- document completion/failure counts,
- page-count mismatches,
- table precision/recall/content/BBOX metrics,
- metadata per-field accuracy/precision/recall,
- RimDocs precedence violations,
- chunk-size and overlap statistics,
- semantic-boundary violations,
- missing embeddings / dimension validation,
- provenance/evidence completeness,
- retrieval quality metrics,
- overall PASS/FAIL against configured acceptance thresholds.

Until that scorer and accepted golden data exist, manual/SQL review can validate behavior but must not be presented as a measured >=99% certification.
