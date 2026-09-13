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

## Setup

```bash
python mir_ai_main.py --config config.ini --init-db
```

Single document:

```bash
python mir_ai_main.py --config config.ini --file /staging/report.pdf --canonical-path //nas/path/report.pdf --source-url "rimdocs://..." --rimdocs-json /staging/report.rimdocs.json
```

NAS batch:

```bash
python mir_ai_main.py --config config.ini --root /archive --rimdocs-jsonl /handover/rimdocs.jsonl
```

S3 batch:

```bash
python mir_ai_main.py --config config.ini --s3 --rimdocs-jsonl /handover/rimdocs.jsonl
```

Batch mode intentionally fails closed without `--rimdocs-jsonl`. The MIR-AI requirements define RimDocs metadata as authoritative and allow LLM extraction only after overlap analysis; treating all 50 requested fields as missing when the authoritative source is unavailable would violate that rule. The JSONL provider is an explicit handover interface until Business/Data Hub supplies the approved live RimDocs interface and mapping.

Document and API concurrency are independently bounded. S3 source versions use ETag + LastModified + size only as a no-download resume key; downloaded content is still SHA-256 hashed before generation creation. S3 download scratch space is reserved before download and released on cleanup.

## DOCX provenance limitation

DOCX XML does not contain reliable rendered page coordinates. MIR-AI honors explicit Word page breaks when present and additionally creates bounded `logical-N` segments when necessary for memory safety. These logical segment identifiers are **not rendered page numbers**. DOCX elements store `bbox=null`; the pipeline does not fabricate coordinates. Exact rendered DOCX page/BBOX provenance requires an approved rendering service.

## Open deployment decisions

The supplied requirements leave PGVector vs Elastic open. This branch implements a PGVector path because current production already uses it, but does not represent that as a business selection. CI exercises the schema, generation lifecycle, 3072-d vector persistence, exact cosine retrieval, enriched metadata page selection, and atomic activation against a real PostgreSQL + pgvector service.

The supplied screenshots also do not establish the authoritative live RimDocs interface/table mapping. This branch therefore uses an explicit provider boundary and JSONL handover instead of inventing a Snowflake/table mapping.

## Quality gate

Do not claim the required >=99% table/metadata accuracy or <=1% chunking/metadata error until measured on a business-approved labelled golden set and reviewed by GRA. The automated test suite is a software-correctness gate; it is not a substitute for the business accuracy benchmark.
