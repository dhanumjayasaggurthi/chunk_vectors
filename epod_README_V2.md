# EPOD Document Ingestion Pipeline

End-to-end pipeline for ingesting EPOD pharmaceutical PDFs into a PostgreSQL + pgvector RAG system.
Designed for archives of **2 million+ documents** with full data capture (text, tables, images, charts)
and GPT-4o summaries on every table and chart.

---

## Architecture

```
\\jnj.com\...\EPOD\Archive   (UNC folder)
        │
        ▼
  [crawler.py]           Walk subfolders, hash-based change detection
        │  skip unchanged / already-running / max-retries-exceeded
        ▼
  ┌─────────────────────────────────────────────────┐
  │  ThreadPoolExecutor  (--doc-workers N)          │
  │  Each thread processes one full document:       │
  │                                                 │
  │   [pdf_processor.py]  Layout analysis per page  │
  │         · PyMuPDF: text blocks + bboxes         │
  │         · Detect image/table regions            │
  │         · TOC extraction                        │
  │         · Reading order reconstruction          │
  │                ↓  (--workers threads/doc)       │
  │   [vision_ocr.py]    Google Cloud Vision OCR   │
  │         · Scanned pages → full-page OCR         │
  │         · Image regions → region OCR            │
  │         · Chart regions → OCR + GPT-4o desc.   │
  │                ↓  (thread pool per doc)         │
  │   [table_extractor.py] pdfplumber extraction   │
  │         · Full table structure preserved        │
  │         · → Markdown table + key=value prose   │
  │         · GPT-4o summary per table (always ON) │
  │                ↓                               │
  │   [chunker.py]    Three-level chunking          │
  │   [embedder.py]   Azure text-embedding-3-small  │
  │   [db.py]         Upsert to Postgres pgvector   │
  └─────────────────────────────────────────────────┘
                    │
        ┌───────────▼────────────────────────────┐
        │  Global API Semaphores                  │
        │  (shared across ALL doc threads)        │
        │   Vision OCR   → max 16 concurrent      │
        │   GPT-4o       → max  6 concurrent      │
        │   Embeddings   → max 12 concurrent      │
        └───────────┬────────────────────────────┘
                    │
        ┌───────────▼────────────────────────────┐
        │  ThreadedConnectionPool (Postgres)      │
        │  Shared pool — no per-doc reconnect     │
        └────────────────────────────────────────┘
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
| `vision_ocr.py` | Google Cloud Vision OCR + GPT-4o chart description |
| `table_extractor.py` | pdfplumber table → Markdown + GPT-4o summary |
| `chunker.py` | Section → page → size-split chunking |
| `embedder.py` | Batched embedding with retry and back-off |
| `main.py` | High-throughput orchestrator + CLI |
| `azure_client.py` | *(existing)* Azure OpenAI calls |
| `config.ini` | *(from template)* Runtime configuration |
| `RAG_epod.py` | Streamlit RAG UI for EPOD archive |

---

## Setup

### 1. Python environment

```cmd
py -3.11 -m venv venv
venv\Scripts\activate
pip install pymupdf pdfplumber pillow google-cloud-vision psycopg2-binary pgvector openai markdown
```

### 2. Configure

```bash
cp config.ini.template config.ini
# Edit config.ini — fill in DB credentials, Azure keys, GCP path
```

Key settings in `config.ini`:

```ini
[PATHS]
docs_root = \\jnj.com\grausdfsroot\ERIS\EPOD\Archive

[POSTGRES]
host     = your-db-host
port     = 5432
database = your-db
user     = your-user
password = your-password

[AZURE_OPENAI_EMBED]
api_key     = ...
api_base    = https://your-instance.openai.azure.com
api_version = 2024-02-01
deployment  = text-embedding-3-small

[AZURE_OPENAI_CHAT]
api_key     = ...
api_base    = https://your-instance.openai.azure.com
api_version = 2024-02-01
deployment  = gpt-4o
```

### 3. Initialise the database

```bash
python db.py
```

Creates `epod.ingestion_log`, `epod.doc_chunks`, and the HNSW vector index.

### 4. Verify GCP credentials

```bash
python vision_ocr.py path/to/a/scanned_page.png
```

---

## Running the Pipeline

### Quick setting to update first

In `config.py`, increase the embedding batch size before any run:

```python
EMBEDDING_BATCH_SIZE = 32   # default 16 — safe to double, halves API round trips
```

---

### Commands

#### Always test on a small batch first

```cmd
python main.py --max-files 50 --doc-workers 4 --workers 4 --force
```

#### Full production run

```cmd
python main.py --doc-workers 8 --workers 2
```

#### Preview what would run — no DB writes

```cmd
python main.py --dry-run
```

#### Force re-ingest everything

```cmd
python main.py --force --doc-workers 8 --workers 2
```

#### Single file

```cmd
python main.py --file "\\jnj.com\...\EPOD\Archive\folder\document.pdf"
```

#### Retry only documents that previously failed

```cmd
python main.py --retry-errors --doc-workers 8 --workers 2
```

#### Show current DB state and error list

```cmd
python main.py --stats
```

---

## Parallelism Guide

Two independent parallelism levels controlled by two flags:

| Flag | What it parallelises | Default |
|---|---|---|
| `--doc-workers N` | Documents processed simultaneously | 1 |
| `--workers N` | OCR page threads within each document | 4 (config) |

**Critical rule: `doc-workers × workers ≤ 16`**

This keeps total concurrent Vision API calls within the safe limit.
The global semaphores enforce this automatically even if you exceed it,
but staying within the formula avoids unnecessary queuing.

### Recommended combinations

| Command | Vision threads | Best for |
|---|---|---|
| `--doc-workers 4 --workers 4` | 16 | Balanced — docs with many pages |
| `--doc-workers 8 --workers 2` | 16 | Large archive, medium docs |
| `--doc-workers 16 --workers 1` | 16 | Huge archive, small/simple docs |

### Semaphore limits (tunable in `main.py`)

```python
_SEM_VISION  = threading.BoundedSemaphore(16)   # Google Cloud Vision
_SEM_GPT4O   = threading.BoundedSemaphore(6)    # GPT-4o — table/chart summaries
_SEM_EMBED   = threading.BoundedSemaphore(12)   # Azure embedding calls
```

Raise these values if your API tier has higher rate limits.

---

## Speed Estimates — 2 Million Documents

Baseline: ~30 seconds/document (mixed native text + scanned pages, tables with summaries).

| Configuration | Docs/day | Days to finish |
|---|---|---|
| `--doc-workers 1 --workers 4` (original) | ~2,880 | ~694 days |
| `--doc-workers 4 --workers 4` | ~11,520 | ~174 days |
| `--doc-workers 8 --workers 2` | ~11,520 | ~174 days |
| `--doc-workers 16 --workers 1` | ~11,520 | ~174 days |
| **3 machines × `--doc-workers 8 --workers 2`** | ~34,560 | **~58 days** |
| **6 machines × `--doc-workers 8 --workers 2`** | ~69,120 | **~29 days** |

### Multi-machine scaling — no code changes needed

Point multiple machines at the same `config.ini` database credentials and run
the same command. The `ingestion_log` hash check guarantees **no document is
ever processed twice** — each machine atomically claims a document by writing
`RUNNING` status before starting, so other machines automatically skip it.

```
Machine 1:  python main.py --doc-workers 8 --workers 2
Machine 2:  python main.py --doc-workers 8 --workers 2   ← same DB, different docs
Machine 3:  python main.py --doc-workers 8 --workers 2   ← same DB, different docs
```

---

## Progress Tracking

Live status line printed every N docs (auto-scaled to ~20 updates per run):

```
── Progress: 12,400/2,000,000 (0.6%)  ✅12,310 ❌90  rate=2.14 doc/s  ETA=10h42m
```

Final summary:

```
── DONE: 2,000,000 docs in 10h44m  ✅1,997,412 ❌2,588  avg=2.14 doc/s
── Database state ──────────────────────────────────
  SUCCESS       docs=1,997,412  chunks=24,312,890  pages=89,421,300
  ERROR         docs=    2,588  chunks=          0  pages=          0
  2,588 documents still in ERROR state
  Run with --retry-errors to reprocess them
```

---

## Data Capture

| Content type | How captured | Notes |
|---|---|---|
| Native PDF text | PyMuPDF — every character on every page | ~100% |
| Scanned pages | Google Cloud Vision full-page OCR | ~98% |
| Embedded images | Vision OCR on image bounding boxes | ~95% |
| Tables | pdfplumber — every cell, merged cells, headers | ~99% |
| Table summaries | GPT-4o narrative summary per table | Always ON |
| Chart labels/numbers | Vision OCR on chart bounding boxes | ~95% |
| Chart descriptions | GPT-4o visual description | Always ON |
| Headers/footers | Captured then deduplicated (running headers removed) | ~95% |
| TOC / bookmarks | PyMuPDF TOC — drives section-level chunking | ~100% |

GPT-4o summaries are always enabled and rate-limited by `_SEM_GPT4O` (max 6
concurrent calls globally). Every chunk has full semantic richness — no silent
skips.

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
| `error_message` | text | Last error (truncated to 2,000 chars) |
| `retry_count` | int | Times attempted |
| `chunk_count` | int | Chunks produced |
| `pages_total` | int | PDF page count |

### `epod.doc_chunks`

| Column | Type | Description |
|---|---|---|
| `chunk_id` | text PK | sha256(doc_id + chunk_index) |
| `doc_id` | text FK | → ingestion_log |
| `file_name` | text | Source filename |
| `file_path` | text | Full UNC path (used for hyperlinks in RAG UI) |
| `chunk_index` | int | 0-based position in document |
| `chunk_total` | int | Total chunks in document |
| `chunk_level` | text | `section` / `page` / `split` |
| `section_title` | text | Nearest heading |
| `page_start` | int | First page (1-based) |
| `page_end` | int | Last page (1-based) |
| `content_types` | text[] | e.g. `{text,table,image,chart}` |
| `chunk_text` | text | Full text sent to embedding |
| `chunk_vector` | vector(1536) | Azure text-embedding-3-small |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

**Index:** HNSW on `chunk_vector` with `vector_cosine_ops` — sub-millisecond ANN search.

---

## Chunking Strategy

### Level 1 — Section chunking (highest retrieval quality)
- **Trigger:** PDF has a Table of Contents with ≥ 3 entries
- One chunk per TOC section (pages from entry to next entry)
- Oversized sections (> 24,000 chars) sub-split at page boundaries, then sliding window
- Chunk text prefixed with section title for better semantic retrieval

### Level 2 — Page-level chunking (fallback)
- **Trigger:** No TOC or fewer than 3 entries
- Detects heading-style text per page (`1.2 Title`, `ANNEX A`, `Introduction`, etc.)
- Groups consecutive pages under the same heading (max `MAX_PAGES_PER_GROUP` pages)
- Size-split applied if group exceeds 24,000 chars

### Level 3 — Sliding window (applied to any oversized chunk)
- Target: 4,000 chars (~1,000 tokens)
- Overlap: 400 chars (~100 tokens) — preserves context across split boundaries
- Split preference: paragraph → sentence → hard cut

---

## Monitoring & Operations

### Log files (auto-rotating)

```
logs/epod_pipeline.log       Plain text, all levels, 10 MB × 5 files
logs/epod_pipeline.jsonl     Structured JSON, WARNING+, for log aggregators
```

### Health check queries

```sql
-- Ingestion status summary
SELECT
    status,
    COUNT(*)                    AS docs,
    SUM(chunk_count)            AS total_chunks,
    ROUND(AVG(pages_total), 1)  AS avg_pages,
    MAX(ingested_at)            AS last_activity
FROM epod.ingestion_log
GROUP BY status
ORDER BY docs DESC;

-- Docs with suspiciously few chunks (possible data loss)
SELECT file_name, chunk_count, pages_total,
       ROUND(chunk_count::numeric / NULLIF(pages_total,0), 2) AS chunks_per_page
FROM epod.ingestion_log
WHERE status = 'SUCCESS'
ORDER BY chunks_per_page ASC
LIMIT 20;

-- Semantic similarity search
SELECT
    file_name, file_path, section_title, page_start, chunk_level,
    LEFT(chunk_text, 200)                                      AS preview,
    1 - (chunk_vector <=> '[...query vector...]'::vector)      AS similarity
FROM epod.doc_chunks
WHERE chunk_vector != array_fill(0, ARRAY[1536])::vector
ORDER BY chunk_vector <=> '[...query vector...]'::vector
LIMIT 10;
```

### Manually reset a stuck document

```sql
UPDATE epod.ingestion_log
SET status = 'PENDING', retry_count = 0
WHERE file_name = 'problem_document.pdf';
```

Documents exceeding `MAX_DOC_RETRIES=3` are permanently skipped by `should_ingest()`
until manually reset.

---

## RAG UI

```cmd
streamlit run RAG_epod.py
```

Features:
- Semantic search over `epod.doc_chunks` using text-embedding-3-small
- Sidebar filters: Document, Chunk level, Content type, Section title
- Similarity bar per result
- **Clickable document hyperlink** — opens source PDF directly in the browser
- GPT-4o grounded RAG answer with citations
- Download citations as `.txt`

### File:// link note

Chrome blocks `file://` links from `http://localhost:8501` by default.
Install the [Local Explorer](https://chrome.google.com/webstore/detail/local-explorer-file-manag/eokekhgpaakbkfkmjjcbffibkencdfkl)
Chrome extension, or deploy Streamlit over HTTPS on your intranet.

---

## Extending the Pipeline

### Change the ingestion root

In `config.ini` — no Python changes needed:

```ini
[PATHS]
docs_root = \\new-server\new-share\new-folder
```

### Raise API concurrency limits

In `main.py`:

```python
_SEM_VISION  = threading.BoundedSemaphore(24)   # if Vision quota allows
_SEM_GPT4O   = threading.BoundedSemaphore(10)   # if GPT-4o quota allows
_SEM_EMBED   = threading.BoundedSemaphore(20)   # if embedding quota allows
```

### Add a new content type

1. Add the type to `pdf_processor.ElementType`
2. Add detection in `PDFProcessor._analyse_page()`
3. Add a handler in `main._ocr_page()` or a new step function
4. Add the type string to `content_types` in the chunk dict

### Add a new chunking strategy

1. Add a method `Chunker._chunk_by_<strategy>()`
2. Add the condition in `Chunker.chunk()`
3. Set the new `chunk_level` string on the `Chunk` object
