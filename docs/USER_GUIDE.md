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

## 4. Backfill vectors in existing chunk tables

Example: profiles/existing_embeddings.json

    python main.py --profile profiles/existing_embeddings.json --embed-existing --dry-run --max-files 100
    python main.py --profile profiles/existing_embeddings.json --embed-existing --max-files 10000

Required existing_chunks mapping fields: schema, table, id_column, text_column, vector_column. The ID must support indexed ordering. Only NULL vectors are updated by default. Failed batches stay NULL and can be retried. This route does not inspect PDFs or change the source schema.

The current deployment writes 1536-dimensional vectors. Ensure the destination vector column has that dimension and the embedding deployment is compatible.

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

Do not treat local tests as a substitute for staging database/AI integration, source schema validation, quality evaluation, provider rate-limit tests and sustained large-archive benchmarking. See STATUS.md.
