# Processing profile user guide

## 1. Preserve the existing workflow

Commands without --profile are unchanged:

    python main.py --doc-workers 4 --workers 4
    python main.py --no-embed --max-files 100

Keep production config.ini unchanged; it contains database and cloud integration settings.

## 2. Use a metadata/chunking profile

Example: profiles/regulatory.json

    python main.py --profile profiles/regulatory.json --dry-run --max-files 10
    python main.py --profile profiles/regulatory.json --max-files 50 --doc-workers 4 --workers 4
    python main.py --profile profiles/regulatory.json --set chunking.max_tokens=600 --set embedding.enabled=false --file /path/to.pdf

The profile describes document fields (indication, application type/number, protocol) and local chunk references, as well as 200-800 token chunking, 100-token overlap and section heading preservation. Field rules are label, regex and constant. Missing fields are omitted, not guessed.

Overrides change the profile fingerprint; an explicit --no-embed is reflected in the fingerprint. Validated profile outputs are isolated from legacy output.

## 3. Create chunks from existing extracted content

Example: profiles/extracted_content.json

    python main.py --profile profiles/extracted_content.json --rechunk-extracted --dry-run --max-files 10
    python main.py --profile profiles/extracted_content.json --rechunk-extracted --max-files 100 --doc-workers 4

Replace the sample schema/table/column names with existing table identifiers. Required: schema, table, doc_id_column, text_column. Optional: page_column, position_column, file_path_column, file_name_column. Multiple rows per document require page or position ordering. Text must be a PostgreSQL textual value.

This route never opens PDFs, runs OCR or repeats table summarization. Index the source document ID and ordering field for performance. The output is saved to separate profile tables after a source-text fingerprint check.

## 4. Distributed backfill of existing vector columns

For the verified `regulatory.palm_paw.doc_chunks_immbot_approved` table,
use `profiles/immbot_approved_1536.json`. This maps
`chunk_id` (valid UNIQUE index), `chunk_text`,
`chunk_vector vector(1536)`, and `embedding_processing_note`.

The profile includes **conservative caps of 30 RPM and 750,000 TPM**.
The Azure gateway returned an observed 1,000,000-TPM limit header, but
the actual RPM allocation is still unknown. Override caps only when
coordinated across workers on the same Azure deployment.

```bash
python main.py --embed-existing \
 --profile profiles/immbot_approved_1536.json --dry-run

# Initial 1,000-row pilot:
python main.py --embed-existing \
 --profile profiles/immbot_approved_1536.json \
 --max-files 1000 --doc-workers 4

# Explicit full backfill: omit --max-files
python main.py --embed-existing \
 --profile profiles/immbot_approved_1536.json --doc-workers 4
```

`start_embed.sh` defaults to a 1,000-row **pilot**;
`EMBED_MODE=full EMBED_CONFIRM_FULL=YES ./start_embed.sh`
starts an uncapped run. `start_embed.ps1` supports
`-Mode pilot`, `-Mode dry-run`, and
`-Mode full -ConfirmFull` on Windows. See
[FAST_EMBEDDINGS.md](FAST_EMBEDDINGS.md) for configuration, results and SQL.

The completed first pilot produced **991 SUCCESS, 9 FAILED,
112.4 seconds, 529.2 successful rows/minute**. The nine
`INPUT_TOO_LONG` rows remained NULL and did **not** stop other rows.
The job ledger stores per-row errors and comments, and
`embedding_runtime.failure_events` retains historical errors across
retries. Source-table notes contain concise current failure reasons.
The oversized texts should be rechunked upstream rather than retried
unchanged or silently truncated.

Unlike legacy single-worker scanning, task assignments use durable
PostgreSQL claims/leases; all hosts with the same database and deployment
settings coordinate safely. A real UNIQUE index on the source row ID is
required. Only NULL vectors are updated when `reuse_existing=true`.
The input vector model must match the model of existing embeddings;
dimensions alone cannot establish compatibility.

## 5. Profile storage and rollback

Profile mode creates two tables in the configured PostgreSQL schema:

* profile_chunks_<folder>: text, headings, page ranges, JSONB metadata and optional vector
* profile_results_<folder>: committed source/profile fingerprints and chunk count

Each document/profile replacement is atomic. A failed operation retains the previously committed output. Different profile hashes can coexist.

Profile tables are not routed into rag_epod.py automatically and do not receive ANN indexes. Plan indexing/retrieval separately before making them query-serving tables.

For rollback, run the legacy CLI without profile flags. Never delete existing production tables as part of the rollback.

## 6. Validation

    pip install -r requirements.txt
    pip install -r requirements-profiles.txt
    python -m compileall -q .
    python -m unittest discover -s tests -p "test_*.py" -v

Do not treat local tests as a substitute for staging database/AI integration, source schema validation, quality evaluation, provider rate-limit tests and sustained large-archive benchmarking. See [FAST_EMBEDDINGS.md](FAST_EMBEDDINGS.md).

## 3072-dimensional question-only embedding

Configure `[AZURE_OPENAI_EMBEDDING]` in the existing private config.ini to point at your text-embedding-3-large deployment, then run:

    python main.py --profile profiles/haq_question_3072.json --embed-existing --set embedding.rpm=YOUR_HAQ_APPROVED_RPM --set embedding.tpm=YOUR_HAQ_APPROVED_TPM --dry-run --max-files 100
    python main.py --profile profiles/haq_question_3072.json --embed-existing

Do not use the immbot gateway limits for a different HAQ Azure deployment without verifying and coordinating its actual quota. The HAQ profile intentionally omits RPM/TPM, so production embedding requires both overrides (or equivalent approved values in that profile).

The example uses `public.haq_query(id, question, question_embedding)`; update schema/column names for your actual table. The target column must be `vector(3072)` already; no schema changes or PDF extraction are performed by `--embed-existing`. The existing 1536-dimensional pipeline remains the default when a dimension is not configured. A dimension mismatch is rejected before sending anything to Azure. A custom Azure deployment can be selected using `embedding.deployment` in the profile; if Azure's model is `text-embedding-3-large`, keep the profile's model selection. Only NULL vectors are incrementally processed; changed already-embedded questions still require a separate fingerprint-based refresh feature.

## Optional embedding processing notes

Configure `existing_chunks.note_column` only if the source table
already has a nullable text/varchar column. The immbot table has
`embedding_processing_note text`.

Distributed embedding failures are recorded in the separate,
durable `embedding_runtime.tasks` and
`embedding_runtime.failure_events` tables. The source note
contains a concise error code/message (for example,
`INPUT_TOO_LONG: 9972 tokens exceeds configured/model limit`).
A successful commit clears the source note. On requeue, the
append-only failure history remains queryable.

The `progress_every_batches` field applies to legacy
profile-driven processing, not the new distributed
existing-table engine; monitor task status in PostgreSQL instead.
For queries, retry semantics and operational safeguards, use
[FAST_EMBEDDINGS.md](FAST_EMBEDDINGS.md).
