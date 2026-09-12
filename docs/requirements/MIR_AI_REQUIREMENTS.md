# MIR-AI ingestion requirement traceability

This branch evolves the existing EPOD pipeline surgically. It keeps the existing crawler, PyMuPDF layout analysis, Vision OCR, TOC/heading-first chunking, Azure integration, pgvector persistence, retry controls, and RAG flow where those are sound.

| Requirement | Implementation on this branch | Validation / state |
|---|---|---|
| PDF + DOCX | Existing PDF processor + `docx_processor.py` | DOCX preserves source order; rendered page numbers are explicitly unavailable rather than invented |
| OCR scanned documents | Existing Google Vision OCR | Retained |
| Complete tables | `table_extractor.py` removes cell truncation and value forward-fill | Unit tests for lossless cells / blanks |
| Table + summary one chunk | Atomic table atoms in `chunker.py` | Oversize tables remain atomic and are flagged |
| Cross-page tables | Conservative header-matched stitching | Ambiguous tables are not joined |
| BBOX/page provenance | Table/PageElement metadata propagated into Chunk JSONB | `metadata` column added |
| 1200–1500 token chunks, 100 overlap | Token-aware semantic chunker | Uses `tiktoken`, conservative fallback |
| No semantic-unit splitting | paragraphs/sentences; tables/images/charts atomic | pathological hard splits flagged |
| repeated headers/footers | cross-page positional frequency removal | PDF processor only removes repeated top/bottom margin text |
| text-embedding-3-large / 3072 / cosine | config = 3072; strict response dimension validation; pgvector cosine | DB fails fast if an existing vector column is not 3072 |
| RimDocs authoritative metadata | `metadata_pipeline.py` overlap/merge | LLM extractor invoked only for fields absent/null in RimDocs |
| relational document metadata | `document_metadata_<folder>` keyed by system `doc_id` | JSONB provenance + missing-fields |
| fault tolerance | existing retries retained; failed embeddings are NULL, never fake zero vectors | failed batches are explicit/auditable |
| long-document throughput | one pdfplumber open for all table pages | avoids per-page reopen/re-extract bottleneck |

## External dependencies still required for full acceptance

The repository cannot implement or certify the following without business/environment inputs: the real RimDocs metadata interface/export, the actual `study_metadata_list_u.xlsx`, a representative golden-set corpus, Azure deployment names/quotas, and the final PGVector-vs-Elastic decision. The code fails/flags missing data rather than fabricating it.

The ≥99% table/metadata and ≤1% chunk/metadata error targets are **acceptance metrics**, not claims. They must be measured against a business-approved golden set before production sign-off.
