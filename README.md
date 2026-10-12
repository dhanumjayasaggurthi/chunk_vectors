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

    python -m compileall -q .
    python -m unittest discover -s tests -p "test_*.py" -v

The profile-aware chunker needs tiktoken; legacy does not. The existing config.ini is still used for cloud credentials and database settings, and must not be committed.

New profiled output is isolated in profile_chunks_<folder> and profile_results_<folder>. It does not replace the legacy RAG query table. The embedder and profile vector column currently support 1536 dimensions.

Do not deploy until staging and scale validation gates in STATUS.md pass.
