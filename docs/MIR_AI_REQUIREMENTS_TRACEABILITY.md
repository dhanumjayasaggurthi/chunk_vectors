# MIR-AI requirements traceability

This file maps only requirements supported by the supplied MIR-AI source material. Engineering decisions are documented separately in `MIR_AI_IMPLEMENTATION_PLAN.md` and `MIR_AI_OPERATIONS.md`.

| Requirement | Implementation |
|---|---|
| PDF and DOCX | `PDFPageStream`, `DOCXPageStream` |
| Text extraction | native PDF text blocks; streaming DOCX paragraph/table XML |
| OCR scanned/image documents | `VisionOCR` + on-demand rendering/read; low-confidence evidence remains explicit |
| Images | BBOX/page/source locator for PDF images; OCR plus evidence-constrained visual description; DOCX image provenance retained without fabricated BBOX |
| Charts/graphs | embedded and evidence-backed vector chart candidates; OCR + evidence-constrained multimodal chart description; DOCX chart XML values/labels |
| Tables with full content/BBOX | source rows are retained without the legacy 500-character cell truncation; PDF page BBOX retained; DOCX BBOX remains null rather than fabricated |
| Table + summary one chunk | continuation assembly before summary; table semantic unit is atomic |
| Headings/hierarchy first | semantic units and section path before token grouping |
| 1200-1500 tokens, overlap 100 | validated settings + tokenizer |
| No semantic-unit splitting | oversize atomic units retained whole and flagged; no silent truncation |
| Repeated headers/footers removed | persisted candidates + DB aggregation + semantic filtering |
| Chunk provenance | doc ID, pages/labels, chunk ID, source URL, table BBOX, section path, content types |
| text-embedding-3-large / 3072 / API 2025-04-01-preview / cosine | runtime validation, embedding response validation, vector(3072), cosine retrieval |
| System-generated doc_id PK | deterministic SHA-256 document ID and relational primary key |
| RimDocs authoritative | authoritative values are persisted first and take precedence |
| LLM only for absent RimDocs fields | overlap returns only missing canonical keys to the extractor; batch processing fails closed without an authoritative RimDocs handover |
| Candidate sections + first 20 fallback | DB-streamed candidate pages search native and enriched OCR/table/chart/image evidence; first 20 pages only when target sections are absent |
| 50-field metadata list | exactly 50 source-visible fields, preserving original labels such as lowercase `vehicle type` |
| Missing/null flagged | explicit missing / invalid / error extraction states |
| Relational document metadata | `mirai_documents`, `mirai_metadata` |
| Chunk metadata in vector DB | `mirai_chunks` stores provenance metadata with vectors |
| Large-document processing | bounded page/doc queues, durable page checkpoints, renewable leases, streaming chunk/embed persistence, no document-wide rendered-image retention |
| Safe re-ingestion | immutable generations + atomic activation; last known-good active generation is not deleted before replacement succeeds |
| Vector DB selection open | PGVector implementation path is available because current production already uses it; no claim that Business selected PGVector over Elastic |
| >=99% / <=1% | qualification gate is implemented/documented; no accuracy claim is made without the business-approved labelled golden set |

## Source limitations intentionally not filled by inference

- The supplied requirements do not identify the approved live RimDocs API/table mapping. The branch provides an explicit provider boundary and JSONL handover adapter rather than inventing that mapping.
- The supplied requirements leave PGVector versus Elastic open.
- The supplied requirements say the golden-set question is open with Business, so the contractual accuracy target cannot be truthfully certified from software unit/integration tests alone.
- DOCX XML does not provide reliable rendered page/BBOX geometry. Logical segment provenance is retained, but rendered DOCX BBOX values are never fabricated.
