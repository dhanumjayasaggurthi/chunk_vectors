# Fidelity storage and retrieval

The existing `doc_chunks_*` tables remain the semantic/RAG layer. They may normalize whitespace for embeddings.
The fidelity layer is separate and must never normalize the source representation.

## Stored fidelity data

- `doc_layout_manifest_*`: source SHA-256, metadata, TOC, page count.
- `doc_layout_pages_*`: exact character sequence, span font/style flags, line/block/page bounding boxes, images, vector drawings, tables/cell bounding boxes and links.
- `doc_layout_sections_*`: section hierarchy plus exact page clip rectangles.
- `doc_layout_source_*`: original PDF bytes (`BYTEA`) plus SHA-256 and byte length.

The source blob is what makes DB-only retrieval independently verifiable. A manifest alone cannot reproduce embedded fonts, vector objects and all PDF resource streams exactly.

## Ingestion hook

After the normal `process_pdf()` layout analysis succeeds, run:

```python
from layout_preserver import extract_document_layout
from layout_store import save_document_layout

layout = extract_document_layout(local_file)
save_document_layout(
    doc_id,
    file_path,
    file_name,
    layout,
    source_file=local_file,
)
```

This intentionally stores the untouched PDF bytes in the fidelity source table.

## Retrieval

Use `retrieval_service.py`:

```python
from retrieval_service import (
    retrieve_source_pdf,
    retrieve_section_pdf,
    retrieve_content_pdf,
)

retrieve_source_pdf(doc_id, "retrieved.pdf")
retrieve_section_pdf(doc_id, "II. Requirements", "requirements.pdf")
retrieve_content_pdf(doc_id, "mandatory requirement", "content.pdf")
```

`retrieve_source_pdf()` is byte-for-byte identical to the ingested PDF. Section/content PDFs are created by copying the requested vector page region from the DB-resident original PDF bytes; they are not re-typeset from Markdown or HTML.

## Verification

Against PostgreSQL:

```bash
python retrieval_verifier.py \
  --doc-id <doc_id> \
  --source /path/to/original.pdf \
  --section "II. Requirements" \
  --query "mandatory requirement" \
  --output-dir retrieval_results
```

The verifier checks source byte equality, SHA-256 equality, complete manifest equality, section render similarity and content-clip retrieval.

Regression suites:

```bash
python tests/generate_regulatory_fixtures.py
python tests/run_fidelity_suite.py
python tests/run_retrieval_db_suite.py
```

The SQLite DB suite is deliberately self-contained so it can run in CI without PostgreSQL. It tests the same persistence invariant: source bytes and the fidelity manifest are stored, read back from a database, rehydrated, and used for retrieval.