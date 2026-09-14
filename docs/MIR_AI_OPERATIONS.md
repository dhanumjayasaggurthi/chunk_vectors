# MIR-AI operations

MIR-AI is additive. The legacy EPOD entrypoint remains available while the new pipeline is qualified. The new pipeline writes only `mirai_*` tables in the configured PostgreSQL schema.

## Large-document safety

- page work has hard `max_inflight_pages` backpressure
- first-pass elements contain source locators, not persistent rendered image bytes
- OCR renders only when consumed and drops bytes before returning
- page state is persisted immediately
- contiguous checkpoint tracking prevents an out-of-order page completion from skipping gaps after a crash
- renewable DB leases replace fixed-age stale assumptions
- completed `pages_total` is persisted with the generation and survives restarts
- header/footer aggregation runs in PostgreSQL
- metadata candidate pages are streamed and searched using persisted native + enriched OCR/table/chart/image text
- table summaries use bounded ordered concurrency
- PDF table detection operates directly on the requested PyMuPDF page rather than materializing pdfplumber Page wrappers for an entire 12k+ page document in every worker
- PDF vector line-art is inspected for evidence-backed chart regions in addition to embedded raster images
- DOCX XML is streamed and bounded into logical segments even when the source has no explicit page breaks
- old active generations are not deleted; replacement activation is atomic

## Configuration

`config.ini` remains uncommitted. MIR-AI accepts uppercase project sections and lowercase screenshot-style sections where applicable.

```ini
[MIR_AI]
source_auto_run = false
source_recursive = true
source_max_files = 0
metadata_mode = optional
rimdocs_jsonl_path =

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
vector_index_mode = exact
enable_embeddings = true
```

`exact` stores full 3072-d vectors and performs exact cosine search. `halfvec_hnsw` is explicit opt-in and creates a half-precision HNSW expression index; do not enable it without retrieval-quality qualification.

## Configurable source start behavior

When:

```ini
source_auto_run = false
```

an operator must explicitly choose `--file`, `--root`, or `--s3`.

When:

```ini
source_auto_run = true
```

a bare command uses `[PATHS] source_type = nas|s3` and the configured NAS root or S3 settings:

```bash
python mir_ai_main.py --config config.ini
```

This switch exists to prevent accidental broad ingestion while still supporting future scheduler/UI-driven execution.

## Configurable structured metadata policy

RimDocs is no longer an unconditional runtime dependency. `[MIR_AI] metadata_mode` controls the behavior:

```ini
metadata_mode = required
```

Use for formal MIR-AI requirements qualification or workflows where structured metadata is mandatory. A RimDocs source must be configured. In batch mode, missing per-document RimDocs rows are reported as `RIMDOCS_NOT_FOUND` rather than silently extracting all requested fields.

```ini
metadata_mode = optional
```

If RimDocs input is configured and a document has an authoritative row, the RimDocs-first overlap/extraction workflow runs. If no RimDocs input/row is available, document ingestion continues and the structured metadata stage is skipped. The system does not assume all 50 fields are missing.

```ini
metadata_mode = disabled
```

Skip structured metadata extraction entirely. This is useful for parsing/OCR/table/chunking/embedding/infrastructure tests.

Changing `metadata_mode` changes the processing fingerprint. Per-document RimDocs payload content is also included in the effective source version, so a real metadata change cannot be incorrectly skipped as `SKIPPED_UNCHANGED`.

## Setup

Initialize schema:

```bash
python mir_ai_main.py --config config.ini --init-db
```

### Single document with required metadata

```bash
python mir_ai_main.py --config config.ini --file /staging/report.pdf --canonical-path //nas/path/report.pdf --source-url "rimdocs://..." --rimdocs-json /staging/report.rimdocs.json
```

### NAS or S3 without mandatory RimDocs

Set:

```ini
[MIR_AI]
metadata_mode = optional
```

or:

```ini
metadata_mode = disabled
```

Then run:

```bash
python mir_ai_main.py --config config.ini --root /archive
```

or:

```bash
python mir_ai_main.py --config config.ini --s3
```

No `--rimdocs-jsonl` is required in these modes.

### Formal RimDocs-first batch

Set:

```ini
[MIR_AI]
metadata_mode = required
rimdocs_jsonl_path = /handover/rimdocs.jsonl
```

Then either run with the configured path:

```bash
python mir_ai_main.py --config config.ini --s3
```

or override it for one invocation:

```bash
python mir_ai_main.py --config config.ini --s3 --rimdocs-jsonl /handover/other.jsonl
```

Document and API concurrency are independently bounded. S3 source versions use ETag + LastModified + size only as a no-download resume key; downloaded content is still SHA-256 hashed before generation creation. S3 download scratch space is reserved before download and released on cleanup.

## DOCX provenance limitation

DOCX XML does not contain reliable rendered page coordinates. MIR-AI honors explicit Word page breaks when present and additionally creates bounded `logical-N` segments when necessary for memory safety. These logical segment identifiers are **not rendered page numbers**. DOCX elements store `bbox=null`; the pipeline does not fabricate coordinates. Exact rendered DOCX page/BBOX provenance requires an approved rendering service.

## Open deployment decisions

The supplied requirements leave PGVector vs Elastic open. This branch implements a PGVector path because current production already uses it, but does not represent that as a business selection. CI exercises the schema, generation lifecycle, 3072-d vector persistence, exact cosine retrieval, enriched metadata page selection, and atomic activation against a real PostgreSQL + pgvector service.

The supplied screenshots also do not establish the authoritative live RimDocs interface/table mapping. This branch therefore uses an explicit provider boundary and JSONL handover instead of inventing a Snowflake/table mapping.

## Quality gate

Do not claim the required >=99% table/metadata accuracy or <=1% chunking/metadata error until measured on a business-approved labelled golden set and reviewed by GRA. The automated test suite is a software-correctness gate; it is not a substitute for the business accuracy benchmark.
