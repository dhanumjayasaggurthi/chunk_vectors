# Ingestion v2 foundation — isolated pilot runbook

**Status: experimental / draft.** No production ingestion entrypoint, source
chunk table, or embedding worker is modified by this package. This is the
first correctness and native-PDF performance slice, not the full distributed
Vision/table/figure pipeline. It must not be merged as a production
replacement until performance, accuracy and recovery gates pass.

## Why the legacy path is slow on large PDFs

The legacy `main.py` calls `process_pdf` for **all pages** before it
launches OCR, table detection and chunking. Its
`pdf_processor._detect_table_regions` reopens the document through
pdfplumber for each page. It also retains rendered image bytes for later
OCR. A 15k–25k-page document consequently occupies one worker for
hours or days and may exhaust memory. PDF analysis/OCR/table errors may
be logged while the document is still reported as successful.

The v2 native pilot opens the PDF independently in each process,
extracts bounded page ranges, writes atomic per-range checkpoints,
and marks image-heavy text-poor pages for a *future* separate GCP OCR stage.
**No OCR is performed in this prototype.**

## Run on Linux or Windows

Use a standalone feature branch with `pymupdf` and `tiktoken`
installed. Existing embedding/inventory jobs can stay running.
Do NOT replace production `main.py` with this command.

```bash
git fetch origin
git switch feat/ingestion-v2-foundation
./venv/bin/python -m pip install pymupdf "tiktoken>=0.7,<1"

# Reads only page count; no database or cloud calls:
./venv/bin/python -m ingestion_v2.cli plan --pdf /path/to/sample.pdf --range-pages 32

# Safely extracts just the FIRST TWO ranges (up to 64 pages) into a new folder:
./venv/bin/python -m ingestion_v2.cli extract \
  --pdf /path/to/sample.pdf --output /tmp/ingestion-v2-pilot \
  --profile profiles/ingestion_v2_general.json --max-ranges 2
```

The result JSON reports a `job_key`, `SUCCESS`, `SKIPPED`,
`FAILED`, `ocr_candidates`, and `output_dir`. Repeat the command:
validated completed range files are skipped; failed ones are retried.

To finish extraction of *all* pages, use `--max-ranges 0` **only
after** a representative pilot and disk/memory measurements. This
command still does not call OCR or affect production tables.

```bash
./venv/bin/python -m ingestion_v2.cli extract \
  --pdf /path/to/sample.pdf --output /tmp/ingestion-v2-pilot \
  --profile profiles/ingestion_v2_general.json --max-ranges 0
```

On Windows replace `./venv/bin/python` with
`.\\venv\\Scripts\\python.exe` and use Windows file paths.

The native output contains per-page text elements, bounding boxes,
page geometry, rotation and an OCR-candidate flag. The pipeline does
NOT retain rendered PNG bytes in memory. Each range is stored in an
atomically replaced gzip/JSON file, under a source/version-specific
directory. Each worker validates file size and nanosecond mtime before
and after extraction. For a formal source-version guarantee, the
manifest additionally contains a streamed SHA-256 of the full PDF.

**Do not run multiple copies on the same PDF/output directory.**
This local checkpoint prototype has no cross-server lease coordination
yet; shared multi-server claims will be added before a production rollout.
Its artifacts must be stored in a location with adequate free space.

## Default reusable chunking policy

The policy is in `profiles/ingestion_v2_general.json`:

| Setting | Initial default | Behavior |
| --- | --- | --- |
| `target_tokens` | 600 | Typical target, not a requirement |
| `max_tokens` | 800 | Hard ceiling using exact tokenizer |
| `min_tokens` | 100 | Prefer combining small adjacent units |
| `overlap_tokens` | 80 | Best-effort repeated context, never above max |
| `tokenizer` | `cl100k_base` | Override when selecting a different model tokenizer |
| `split_oversized_tables` | `true` | Preserve whole tables if possible; split oversized elements explicitly |

For a 10-page section, the new chunker emits multiple chunks subject
to `max_tokens`; it does not truncate a section. Original element IDs
and bounding boxes remain attached. Explicit
`continuation_of` links can join paragraph fragments across pages
before chunking. **The native range extractor does not yet generate
those semantic links**: they require the layout/stitching stage.
Similarly, table/figure element extraction, multi-page table stitching,
and geometry are future work. Do not mistake native text blocks for
complete reconstructed tables.

When a document contains OCR-candidate pages, `chunk` refuses to
publish native-only chunks until OCR results are merged; it does not
silently accept missing pages.

For a fully native-text PDF with all page ranges extracted:

```bash
./venv/bin/python -m ingestion_v2.cli chunk \
  --artifacts /tmp/ingestion-v2-pilot/YOUR_JOB_KEY \
  --profile profiles/ingestion_v2_general.json \
  --output /tmp/v2-chunks.json.gz
```

This is an isolated export. It does not insert into production PostgreSQL
or generate embeddings. Consumers can copy and modify the JSON profile
for other models, document types, and retrieval goals.

## Configurable metadata rules

`profiles/ingestion_v2_general.json` contains opt-in,
per-field metadata definitions with `enabled`, `type`, `method`
(`regex` or `label`), `sample_values`, and `first_pages`.
Each extracted value includes the source element ID, page, bbox, method,
and exact evidence text. Examples are **format guidance only**;
they never fill a missing value or increase a made-up confidence score.
Unmatched and invalid fields have explicit statuses.

The future metadata-LLM fallback will require a separate quota-controlled
worker, evaluation samples, typed validation and human review for
low-confidence fields. It is **not enabled or implemented** in this pilot.

## GCP quota diagnosis (read-only)

Project quotas may differ from the published defaults. To inspect the
actual Vision and Document AI limits with the application's service
account credentials, use:

```bash
./venv/bin/python scripts/check_gcp_quotas.py
```

Or specify the project explicitly:

```bash
./venv/bin/python scripts/check_gcp_quotas.py --project YOUR_GCP_PROJECT_ID --json
```

This uses `GET` requests to Cloud Quotas, never enables APIs, submits
OCR jobs or changes quotas. The service account must have
`cloudquotas.quotas.get`. The Cloud Quotas API needs to be enabled
by a project administrator. A permissions failure is NOT evidence of
unlimited or zero quota.

Alternative for cloud administrators using `gcloud`:

```bash
gcloud beta quotas info list --service=vision.googleapis.com --project=YOUR_PROJECT_ID
gcloud beta quotas info list --service=documentai.googleapis.com --project=YOUR_PROJECT_ID
```

Inspect both the project **limit** and the actual **usage chart** in
Google Cloud Console → IAM & Admin → Quotas & System Limits. These are
distinct metrics; quota definitions alone cannot show current usage.

Current official references (verify again before assigning rates):

- <https://docs.cloud.google.com/vision/quotas>
- <https://docs.cloud.google.com/vision/docs/pdf>
- <https://docs.cloud.google.com/document-ai/limits>
- <https://docs.cloud.google.com/document-ai/quotas>
- <https://docs.cloud.google.com/docs/quotas/view-manage>
- <https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas/info/list>
- <https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html>

## Enterprise deployment gates

Before the first production pilot:
1. Separate page-range **PostgreSQL leases with fencing** and source-version
   checks to coordinate Linux and Windows servers; no concurrent local
   checkpoint writes to one document.
2. A **separate Cloud Vision worker pool** with a PostgreSQL-shared
   project/request/feature quota budget, bounded queue, 429 backoff and
   durable per-page errors. Use native extraction before OCR to reduce spend.
3. Layout/table/figure geometry, cross-page paragraph/table stitching,
   artifact provenance and quality regression corpus.
4. Versioned and atomic publication into configurable target tables;
   keep the original output untouched until validation passes.
5. Optional metadata-LLM extraction (with evidence and validation), chunk
   versioning, embedding text templates and model/dimension routing.
6. Production load tests for 500, 15k and 25k page PDFs, across
   Linux/Windows, including restart, WAN/NAS/S3 failures and 429 storms.
7. A DB/disk/API capacity model based on **pages per second** and the
   proportion of scanned pages, not documents/hour alone.

No measured GCP quota, OCR quality or sustained native-page throughput
has been provided yet. This repository does not currently claim a
production throughput SLA or a full distributed v2 release.
