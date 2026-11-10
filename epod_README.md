# EPOD Document Ingestion Pipeline

End-to-end pipeline for ingesting EPOD pharmaceutical PDFs into a PostgreSQL + pgvector RAG system.

---

## Architecture

```
\\jnj.com\...\EPOD\Archive   (UNC folder)
        │
        ▼
  [crawler.py]           Walk subfolders, hash-based change detection
        │  skip unchanged / already-running / max-retries-exceeded
        ▼
  [pdf_processor.py]     Layout analysis per page
                          · PyMuPDF: text blocks + bboxes + font info
                          · Detect image regions → flag for Vision OCR
                          · Detect table regions → flag for pdfplumber
                          · TOC extraction (drives section-level chunking)
                          · Reading order reconstruction (2-col aware)
        │
        ▼  (parallel, PAGE_WORKERS threads)
  [vision_ocr.py]        Google Cloud Vision DOCUMENT_TEXT_DETECTION
                          · Scanned pages → full-page OCR
                          · Image regions → region OCR
                          · Chart regions → OCR labels + GPT-4o description
        │
  [table_extractor.py]   pdfplumber table extraction (sequential)
                          · Cell cleaning, merged-cell fill, header detection
                          · → Markdown table + key=value prose
                          · Optional GPT-4o summary per table
        │
        ▼
  [chunker.py]           Three-level chunking strategy
                          1. SECTION  — TOC bookmarks (≥3 entries)
                          2. PAGE     — heading detection, page groups
                          3. SPLIT    — sliding window, paragraph-aware
                          · Deduplicates running headers/footers
                          · Prefixes section title for better retrieval
        │
        ▼
  [embedder.py]          Azure OpenAI text-embedding-3-small
                          · Batch size 16, exponential back-off retry
                          · 429 Retry-After header honoured
                          · Oversized chunks auto-truncated
                          · Failed chunks → zero vector (stored, not searchable)
        │
        ▼
  [db.py]                PostgreSQL + pgvector
                          · epod.ingestion_log  (per-document status)
                          · epod.doc_chunks     (vectors + metadata)
                          · HNSW index (cosine distance)
```

---

## File Inventory

| File | Purpose |
|---|---|
| `config.py` | Central settings — reads `config.ini` |
| `db.py` | DB schema init, ingestion log, chunk upsert |
| `logger.py` | 3-channel logging (file, JSONL, colour console) |
| `crawler.py` | Folder walk, hash-based skip logic |
| `pdf_processor.py` | Layout analysis, TOC, reading order |
| `vision_ocr.py` | Google Cloud Vision OCR + chart description |
| `table_extractor.py` | pdfplumber table → Markdown text |
| `chunker.py` | Section → page → size-split chunking |
| `embedder.py` | Batched embedding with retry |
| `main.py` | Full orchestrator + CLI |
| `azure_client.py` | *(existing)* Azure OpenAI calls |
| `config.ini` | *(from template)* Runtime configuration |

---

## Setup

### 1. Install dependencies

```bash
pip install pymupdf pdfplumber pillow google-cloud-vision psycopg2-binary pgvector openai
```

### 2. Configure

```bash
cp config.ini.template config.ini
# Edit config.ini — fill in DB password, Azure OpenAI key
```

### 3. Verify DB connectivity

```bash
python db.py          # runs init_db() — creates epod schema + tables
```

### 4. Verify GCP credentials

```bash
python vision_ocr.py path/to/a/scanned_page.png
```

---

## Running the Pipeline

### Full crawl (normal operation)

```bash
python main.py
```

Crawls `\\jnj.com\...\EPOD\Archive`, skips unchanged files, processes new/modified.

### Preview what would run (no DB writes)

```bash
python main.py --dry-run
```

### Force re-ingest everything

```bash
python main.py --force
```

### Single file (testing / manual fix)

```bash
python main.py --file "\\jnj.com\...\EPOD\Archive\somefolder\document.pdf"
```

### Test on a small batch first

```bash
python main.py --max-files 10 --force
```

### Increase parallel OCR workers

```bash
python main.py --workers 8
```

### Check DB status

```bash
python main.py --stats
```

---

## Database Schema

### `epod.ingestion_log`

| Column | Type | Description |
|---|---|---|
| `doc_id` | text PK | sha256 of normalised file path |
| `file_path` | text | Full path (forward slashes) |
| `file_name` | text | Filename only |
| `file_hash` | text | sha256 of file bytes (change detection) |
| `file_size` | bigint | Bytes |
| `last_modified` | timestamptz | File mtime |
| `ingested_at` | timestamptz | Last pipeline run time |
| `status` | text | PENDING / RUNNING / SUCCESS / ERROR / SKIPPED |
| `error_message` | text | Last error (truncated to 2000 chars) |
| `retry_count` | int | Times attempted |
| `chunk_count` | int | Chunks produced |
| `pages_total` | int | PDF page count |

### `epod.doc_chunks`

| Column | Type | Description |
|---|---|---|
| `chunk_id` | text PK | sha256(doc_id + chunk_index) |
| `doc_id` | text FK | → ingestion_log |
| `file_name` | text | Source filename |
| `file_path` | text | Full path |
| `chunk_index` | int | 0-based position in document |
| `chunk_total` | int | Total chunks in document |
| `chunk_level` | text | `section` / `page` / `split` |
| `section_title` | text | Nearest heading |
| `page_start` | int | First page (1-based) |
| `page_end` | int | Last page (1-based) |
| `content_types` | text[] | e.g. `{text,table,image}` |
| `chunk_text` | text | Text sent to embedding |
| `chunk_vector` | vector(1536) | Embedding vector |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

**Index:** HNSW on `chunk_vector` with cosine distance operator class.

---

## Chunking Strategy Detail

### Level 1 — Section chunking (best quality)
- Requires: PDF has a Table of Contents with ≥ 3 entries
- Each TOC entry = one chunk (pages from this entry to the next)
- If section > 24,000 chars: sub-split at page boundaries first, then sliding window
- Chunk is prefixed with section title for retrieval

### Level 2 — Page-level chunking (fallback)
- No TOC or fewer than 3 TOC entries
- Detects heading-style text on each page (`1.2 Title`, `ANNEX A`, `Introduction`, etc.)
- Groups consecutive pages under same heading (max `MAX_PAGES_PER_GROUP` pages)
- Applies size-split if group text exceeds 24,000 chars

### Level 3 — Sliding window (always applied to oversized chunks)
- Target: 4,000 chars (~1,000 tokens)
- Overlap: 400 chars (~100 tokens) — preserves context across splits
- Split boundary preference: paragraph → sentence → hard cut

---

## Monitoring & Operations

### Log files (auto-rotating)

```
logs/epod_pipeline.log       Plain text, all levels, 10 MB × 5 files
logs/epod_pipeline.jsonl     Structured JSON, WARNING+, for log aggregators
```

### Check for failed documents

```bash
python main.py --stats
```

Output shows counts by status and lists ERROR documents with their error messages.

### Retry failed documents

```bash
python main.py --force
```

Or target a specific file:

```bash
python main.py --file "path/to/failed_doc.pdf" --force
```

Documents exceeding `MAX_DOC_RETRIES=3` are permanently skipped by `should_ingest()` until manually reset in the DB:

```sql
UPDATE epod.ingestion_log
SET status = 'PENDING', retry_count = 0
WHERE file_name = 'problem_document.pdf';
```

### Query the vector store (example)

```sql
SELECT
    chunk_id,
    file_name,
    section_title,
    page_start,
    chunk_level,
    chunk_text,
    1 - (chunk_vector <=> '[...your query vector...]'::vector) AS similarity
FROM epod.doc_chunks
ORDER BY chunk_vector <=> '[...your query vector...]'::vector
LIMIT 10;
```

---

## Performance Notes

- **OCR throughput**: Vision API calls are the bottleneck. `--workers 4` gives ~4x speedup on scanned documents. Don't exceed 8 workers to avoid 429s.
- **Embedding throughput**: ~16 chunks/call. For a 3,000-page doc with 300 chunks, expect ~20 embedding API calls.
- **Memory**: Pipeline processes one document at a time, one page at a time within each doc. Even 3,000-page PDFs stay well under 2 GB RAM.
- **First run estimate**: At ~30 seconds/document (mixed scanned/native text), 1,000 documents ≈ 8 hours.

---

## Extending the Pipeline

### Add a new content type

1. Add the type to `pdf_processor.ElementType`
2. Add detection in `PDFProcessor._analyse_page()`
3. Add a handler in `main._ocr_page()` or a new step
4. Add the type string to `content_types` in chunks

### Add a new chunking strategy

1. Add a method `Chunker._chunk_by_<strategy>()`
2. Add the condition check in `Chunker.chunk()`
3. Add the `chunk_level` string to `Chunk.chunk_level`
