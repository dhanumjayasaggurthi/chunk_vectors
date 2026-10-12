# chunk_vectors

Existing PDF ingestion and RAG storage, now with opt-in configurable processing profiles. The production path stays the default.

## Legacy (unchanged)

    python main.py --doc-workers 4 --workers 4
    python main.py --no-embed --max-files 100

## Opt-in profiles

    python main.py --profile profiles/regulatory.json --max-files 100 --doc-workers 4
    python main.py --profile profiles/regulatory.json --set chunking.max_tokens=600 --file /path/file.pdf

Process already-extracted text in PostgreSQL without PDF/OCR:

    python main.py --profile profiles/extracted_content.json --rechunk-extracted --max-files 100

Fill missing vectors in a differently structured chunk table:

    python main.py --profile profiles/existing_embeddings.json --embed-existing --max-files 1000

Read ARCHITECTURE.md, STATUS.md and docs/USER_GUIDE.md before deployment.

## Validation

    pip install -r requirements-profiles.txt
    python -m compileall -q .
    python -m unittest discover -s tests -p "test_*.py" -v

The profile-aware chunker needs tiktoken; legacy does not. The existing config.ini is still used for cloud credentials and database settings, and must not be committed.

New profiled output is isolated in profile_chunks_<folder> and profile_results_<folder>. It does not replace the legacy RAG query table. The embedder and profile vector column currently support 1536 dimensions.

Do not deploy until staging and scale validation gates in STATUS.md pass.

## 3072-dimensional HAQ question embeddings

For an existing `haq_query.question_embedding vector(3072)` column, use `profiles/haq_question_3072.json` and `python main.py --profile profiles/haq_question_3072.json --embed-existing`. Edit the JSON schema/column mapping to your actual identifiers. The profile sets `embedding.dimensions=3072` and `embedding.model=text-embedding-3-large`, and checks the destination pgvector column before API calls. Existing non-NULL embeddings are skipped. The selected Azure deployment must actually support 3072 dimensions.

For optional HAQ processing notes, run the reviewed migration `ALTER TABLE public.haq_query ADD COLUMN IF NOT EXISTS embedding_processing_note TEXT;` before using the example HAQ profile. It records short neutral explanations and clears them on success. The feature never creates user-table columns automatically. See the user guide for progress logging and note meanings.
