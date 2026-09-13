# MIR-AI requirements traceability

| Requirement | Implementation |
|---|---|
| PDF and DOCX | `PDFPageStream`, `DOCXPageStream` |
| OCR scanned/image documents | `VisionOCR` + on-demand rendering |
| Charts/graphs | conservative chart heuristic; OCR + evidence-constrained multimodal chart description; DOCX chart XML values/labels |
| Tables with content/BBOX | raw rows and page BBOX retained; DOCX BBOX stays null rather than fabricated |
| Table + summary one chunk | continuation assembly before summary; table unit is atomic |
| Headings/hierarchy first | semantic units and section path before token grouping |
| 1200-1500 tokens, overlap 100 | validated settings + tokenizer |
| No semantic-unit splitting | oversize atomic units retained whole and flagged |
| Repeated headers/footers removed | persisted candidates + DB aggregation + semantic filtering |
| Chunk provenance | doc ID, pages/labels, chunk ID, URL, BBOX, section path, content types |
| text-embedding-3-large / 3072 / API 2025-04-01-preview / cosine | settings and embedding response validation; cosine retrieval |
| System-generated doc_id PK | deterministic SHA-256 ID and PK |
| RimDocs authoritative | overlap resolves authoritative values first |
| LLM only for absent RimDocs fields | extractor receives only missing canonical keys |
| Candidate sections + first 20 fallback | streamed DB candidate pages |
| 50-field metadata list | exactly 50 source-visible fields |
| Missing/null flagged | explicit missing/invalid/error status |
| Relational document metadata | `mirai_documents`, `mirai_metadata` |
| Chunk metadata in vector DB | `mirai_chunks` stores metadata with vectors |
| Vector DB open | PGVector path implemented, business selection not claimed |
| >=99% / <=1% | documented quality gate; no unmeasured claim |
