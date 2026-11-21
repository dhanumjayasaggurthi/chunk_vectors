# Main-branch ingestion improvements: existing content first

**Review status: draft, staging only.** Based directly on `main`. No
code changes to `fast_embedding_runtime.py`, `existing_embeddings.py`,
production embedding profiles or the `embedding_runtime` database schema.

## What is already implemented

The current `main.py --rechunk-extracted` profile route and
`extracted_source.py` can load previously extracted text from PostgreSQL.
This change extends that existing approach rather than building another OCR
service:

- Read rows from an intake-configured, existing source table (e.g. columns
  `doc_id`, `chunk_index`, `page_start`, `page_end`,
  `section_title`, `chunk_text`, `file_path`, `file_name`).
- Reconstruct pages/sections in stable source order. Do not invent geometry:
  bbox values cannot be recovered if the source table lacks them.
- Apply existing tokenizer-aware chunking, including configured min/max/
  overlap and source page-range provenance.
- Extract typed, enabled/disabled metadata via deterministic label/regex
  policies. Sample values are configuration guidance, not filled-in data.
  `required: true` fails the affected document when evidence is missing.
- Promote configured source columns to document-level metadata, **only**
  if the value is consistent across that document's rows.
- Create embedding text by concatenating only explicitly selected
  `document.*`, `source.*`, `chunk.*`, `section_title`, and
  `chunk_text` values with labels. Reject composed text over the
  selected tokenizer budget.
- Prepare a **new table**, leaving the original text in
  `source_chunk_text`, and the composed embedding-ready input in
  `chunk_text`. The existing fast embedding engine can read
  `chunk_id`, `chunk_text`, `chunk_vector`, and
  `embedding_processing_note` without modification.
- Record document completion hashes and independent failure events;
  atomic per-document replacement preserves earlier successful output
  when a reprocessing attempt fails.

### Targeted improvements to the main-branch PDF path

The existing PDF parser and OCR provider remain in place:

- **Table batches:** `table_extractor.extract_tables_for_pages` opens one
  pdfplumber handle per bounded group of candidate table pages, rather than
  opening the same document for every page behind a file-level lock.
  Configure `[PIPELINE] table_batch_pages=32` (allowed 1..256).
- **No duplicate PDF parse:** existing GPT table summaries reuse extracted
  Markdown rather than calling PDF table extraction a second time.
  Set `[PIPELINE] table_summaries_enabled=false` to disable the
  optional, potentially costly summary requests. This is a new tuning choice;
  do not change it for approved production use cases without evaluation.
- **Explicit incomplete-content failure:** unresolved page extraction, OCR
  and table extraction mark the **document** failed instead of returning
  success with missing content. Other documents continue; the existing
  retry mechanism can revisit the failed document.
- **OCR strict mode:** only the main ingestion entrypoint opts in to
  `raise_on_error=True`, detecting low-confidence error flags as
  incomplete. Other old callers retain their legacy behavior.

These changes reduce redundant PDF work but **do not yet demonstrate
high throughput on 15k/25k-page documents**. The current PDF processor
still materializes whole-document page/image structures in memory.
A separate bounded-page extraction implementation and performance
tests are required before claiming that large-document bottleneck solved.

## Source mapping / consumer contract

Edit `profiles/intake_reuse_immbot_example.yml` and use **a new target
table name for each approved profile version**. Do not set target equal to
source, and do not specify columns that do not exist in the source.

These mappings are case-sensitive PostgreSQL identifiers. The example
uses only columns confirmed in the existing chunk table. It does not
assume your other requested metadata columns already exist; use
`metadata.document` extraction rules for fields found in text.

```yaml
extracted_content:
  schema: palm_paw
  table: doc_chunks_immbot_approved
  doc_id_column: doc_id
  position_column: chunk_index
  page_column: page_start
  page_end_column: page_end
  section_column: section_title
  text_column: chunk_text
  source_columns:
    source_file_name: file_name
embedding:
  input_fields:
    - document.protocol_number
    - section_title
    - source.source_file_name
    - chunk_text
  input_max_tokens: 6000
output:
  schema: palm_paw
  table: doc_chunks_immbot_semantic_v1
```

The actual profile contains additional validated chunking and
metadata rules. Neither this configuration nor `reuse_materialize`
modifies the existing source rows.

## Safe staged testing (Windows)

Use a separate Git worktree: do not switch branches in a running
production checkout. Use a **staging PostgreSQL connection** in the
pilot's local `config.ini`; the credentials and source table permissions
must be approved by your DBA.

```powershell
cd C:\Users\DSaggurt\PycharmProjects\epod-chunk-vectors
git fetch origin
git worktree add ..\epod-intake-pilot origin/feat/main-ingestion-reuse-performance
cd ..\epod-intake-pilot

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "psycopg2-binary>=2.9,<3" "tiktoken>=0.7,<1" "PyYAML>=6.0.2,<7"

# No database access or writes:
.\.venv\Scripts\python.exe -m reuse_materialize validate --profile profiles/intake_reuse_immbot_example.yml

# Requires the staging DB connection in config.ini, but performs no writes:
.\.venv\Scripts\python.exe -m reuse_materialize preview --profile profiles/intake_reuse_immbot_example.yml --doc-id APPROVED_SAMPLE_DOC_ID

# Inspect planned table names; no writes without --apply:
.\.venv\Scripts\python.exe -m reuse_materialize init --profile profiles/intake_reuse_immbot_example.yml

# DBA-reviewed staging-only DDL for the dedicated output and status tables:
.\.venv\Scripts\python.exe -m reuse_materialize init --profile profiles/intake_reuse_immbot_example.yml --apply

# Explicitly bounded processing; each failed doc is recorded and isolated:
.\.venv\Scripts\python.exe -m reuse_materialize run --profile profiles/intake_reuse_immbot_example.yml --max-docs 20 --doc-workers 2
```

The `init --apply` command does not enable extensions, create embedding
jobs or modify any source table. It requires pgvector already installed,
and creates one new destination plus its independent status/failure
tables in the chosen schema. Do not run against production during
this review phase.

## SQL monitoring (read-only)

```sql
SELECT COUNT(*) AS prepared_chunks,
       COUNT(*) FILTER (WHERE chunk_vector IS NULL) AS waiting_for_embedding,
       COUNT(DISTINCT doc_id) AS prepared_documents
FROM palm_paw.doc_chunks_immbot_semantic_v1;

SELECT COUNT(*) AS completed_documents, MAX(updated_at) AS last_completion
FROM palm_paw.doc_chunks_immbot_semantic_v1_status;

SELECT doc_id,error_code,error_message,occurred_at
FROM palm_paw.doc_chunks_immbot_semantic_v1_failures
ORDER BY occurred_at DESC LIMIT 20;
```

## Use the existing fast embedding engine unchanged

Once the output is complete, stable, and approved in staging, configure
the existing engine against the **new** table using
`profiles/immbot_semantic_v1_embeddings_example.json`:

```powershell
python -u main.py --embed-existing --profile profiles/immbot_semantic_v1_embeddings_example.json --doc-workers 4 --max-files 1000
```

This command **does make Azure calls and database writes**. Do not run
it in production as part of an intake preview. The example RPM/TPM
values come from the existing embedding profile, not a new quota test.
Do not materialize into the same target table concurrently with a live
embedding backfill without separately validating source-change fencing.

## Release gates and known limitations

- Run the new code in a separate worktree; never move a running production
  checkout onto the feature branch.
- **Stable ordering requirement:** a multi-row source document must have a
  unique, non-null order key. Map `position_column: chunk_index` where
  available; repeated `page_start` alone is not sufficient.
  Ambiguous documents fail individually instead of silently producing
  nondeterministic new chunks.
- Benchmark representative **existing-table documents**, including
  thousands of source rows, under concurrent source updates. Verify
  index coverage (`doc_id`, `chunk_index` or position) and use keyset
  enumeration, not full-table loading.
- A previously flattened source text cannot recreate precise table
  cell structure, rotated region coordinates, figures or bbox evidence
  that were never persisted.
- Some documents are too large for one in-memory `DocumentStructure`
  even when DB fetching is paged. The next dedicated extraction milestone
  must introduce bounded native PDF page ranges with durable artifacts
  before claiming safe throughput on 25,000-page PDFs.
- The current OCR path still uses individual Vision calls; the 235.42
  successful synthetic images/minute benchmark validates batching as a
  promising next optimization but does not establish an applied quota.
  Cloud OCR and accuracy measurement remain independent release gates.
- Validate exactly one source row order, metadata conflicts, semantic
  retrieval quality, total processing time, provenance and reprocessing
  on a representative, approved regulatory corpus before promotion.
