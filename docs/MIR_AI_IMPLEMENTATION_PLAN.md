# MIR-AI implementation plan

## Baseline

- Production baseline commit: `299f9d3bfaf1518e451282e36d544b6ae6d9ada7` (`current working production code`).
- Implementation branch: `feature/MIR-AI-2026-09-12`.
- Preserve the current modular pipeline where it is strong. Do not rewrite the system wholesale.
- This document separates source-backed requirements from engineering decisions. Anything labeled **engineering decision** is not claimed to be a business requirement.

## Source-backed requirements

The supplied MIR-AI requirement material requires:

- Parse `.pdf` and `.docx` documents.
- Parse text, tables, charts/graphs, and images.
- OCR scanned/image documents.
- Preserve page references and bounding boxes for applicable extracted content.
- Extract tables in full and keep a table and its generated summary in a single logical chunk.
- Chunk by semantic hierarchy/headings before size-based splitting.
- Preserve semantic units and avoid splitting sentences, paragraphs, tables, lists, and code blocks.
- Target 1200-1500 tokens per normal chunk with 100-token overlap.
- Remove repeated headers/footers.
- Use `text-embedding-3-large`, 3072 dimensions, cosine similarity, Azure OpenAI API version `2025-04-01-preview`.
- Use a unique system-generated `doc_id` as the document-level primary key.
- Store document-level metadata in a relational database and chunk metadata in the vector database.
- Perform overlap analysis between requested metadata and authoritative RimDocs metadata.
- Do not LLM-extract requested metadata fields that are already supplied by RimDocs.
- Candidate LLM extraction regions are Summary, Study Administration, Protocol, Report Approval, with first 20 pages as fallback when those sections are not found.
- Process approximately 69k documents, with iterative review/handover.
- Target >=99% extraction accuracy for tables/metadata and <=1% chunking/metadata error rate.
- The vector database choice remains open between PGVector and Elastic in the supplied requirements.

## Workload characteristics supplied for architecture

The implementation must remain safe when a single document has 12,000+ pages and may contain thousands of images, tables, and charts. These are workload characteristics, not guaranteed counts.

The architecture must not require a document to fit comfortably in RAM. A larger document should primarily increase processing time and durable intermediate state, not cause approximately linear process-memory growth.

## Non-negotiable implementation invariants

These are **engineering decisions** derived from the workload and accuracy requirements:

1. Do not retain rendered image/page bytes for the lifetime of a document.
2. Do not enqueue work for every page/element in an unbounded in-memory collection.
3. Every stage must use bounded queues/backpressure.
4. Large-document restart must resume from durable stage/page checkpoints; it must not normally restart from page 1.
5. Re-ingestion must never delete the last known-good document generation before the replacement has passed validation and is atomically activated.
6. Long-running work must use renewable leases/heartbeats, not a fixed `RUNNING` age assumption.
7. Tables and other atomic semantic units must never be silently truncated or dropped to satisfy a target chunk size.
8. Missing/failed OCR, extraction, summarization, metadata, and embedding states must be explicit and retryable; they must not be converted into apparently successful data.
9. Raw source evidence and normalized values must remain distinguishable and traceable.
10. Credentials and secrets must not be written to repository files or logs.

## Current production findings driving Phase 1

Read-only inspection of the production baseline found the following scale/correctness risks:

- `PDFProcessor.process()` accumulates all `PageContent` objects in `DocumentStructure.pages`.
- image/chart `PageElement` objects can retain rendered PNG `image_bytes` until the document leaves memory.
- scanned pages can retain a full-page rendered image.
- page OCR/table work is built over the full document page collection.
- table detection/extraction repeatedly opens the PDF and table summarization currently performs a second extraction pass.
- chunking can assemble large section strings before splitting.
- embedding holds all chunks and vectors for the document before persistence.
- re-ingestion deletes existing chunks before the replacement document is fully successful.
- stale `RUNNING` recovery is age based, currently 60 minutes.
- the current embedding path and DB vector column are 1536-dimensional, while MIR-AI requires 3072.
- the current supported extension set is PDF-only.

## Delivery phases

### Phase 0 - Baseline protection and verification

- Branch from the exact production SHA.
- Add traceability documentation.
- Add regression and architecture tests before changing behavior.
- Keep production `main` untouched.

### Phase 1 - Bounded-memory document execution

- Introduce a lightweight page/element manifest that stores text/provenance/locators, not persistent rendered image bytes.
- Introduce durable local stage state/checkpoints for large documents.
- Change processing from `whole document -> next stage` to bounded page windows with backpressure.
- Render OCR/chart regions only when the corresponding worker consumes them; release bytes immediately after the call.
- Avoid full-document chunk/vector accumulation; chunk, embed, validate, persist in bounded batches.
- Preserve cross-window state needed for headings, repeated headers/footers, and multi-page tables.

### Phase 2 - Fault tolerance and safe re-ingestion

- Add document generation/version state (`BUILDING`, `ACTIVE`, `SUPERSEDED` or equivalent).
- Write replacement output to a new generation.
- Validate then atomically activate the new generation.
- Add renewable worker lease + heartbeat and stage/page progress.
- Make all stage writes idempotent and replay-safe.

### Phase 3 - Extraction accuracy

- Remove destructive table-cell truncation and unsafe inferred filling where it can alter source evidence.
- Preserve table BBOX/page provenance and raw cells.
- Extract tables once; summarize the extracted canonical representation without reparsing the page.
- Preserve numeric-only OCR evidence and record low-confidence output rather than silently discarding it.
- Add DOCX parser adapter while reusing downstream semantic models.
- Implement token-aware semantic chunking with atomic units.

### Phase 4 - MIR-AI metadata

- Define the requested 50-field canonical registry while retaining original source labels for auditability.
- Load authoritative RimDocs metadata first.
- Compute per-document overlap.
- Extract only requested fields absent from RimDocs.
- Require source evidence/provenance for every extracted value.
- Persist raw, normalized, status, confidence/provenance, and error state separately.

### Phase 5 - Embedding/vector migration

- Move to `text-embedding-3-large` / 3072 dimensions / required API version.
- Do not mutate the current 1536-dimensional production path in place.
- Benchmark the vector persistence/index option required for 3072 dimensions before selecting the production retrieval implementation.
- Migrate via versioned schema/index generation and atomic retrieval cutover.

### Phase 6 - Validation and performance qualification

- Golden-set tests for tables, metadata, chunking, OCR, and retrieval.
- Failure injection: API 429/5xx/timeouts, DB disconnect, worker crash, full scratch disk, duplicate worker claim, interrupted source read.
- Scale tests at increasing document sizes including 12,000+ page synthetic/representative documents.
- Verify bounded peak RSS and bounded queue sizes.
- Verify crash/restart resumes from checkpoints.
- Do not claim >=99% until measured against an accepted labelled evaluation set.

## Phase 1 acceptance criteria

Before moving to metadata/vector expansion:

- Peak process memory is bounded by configured worker/window budgets rather than total page count.
- No rendered page/image bytes survive outside the bounded worker lifecycle unless explicitly persisted to controlled scratch storage.
- Work queues are bounded.
- A worker crash after many thousands of pages resumes from durable progress.
- No successful prior generation is deleted before replacement activation.
- No page, table, image, chart, or extracted text is silently discarded because of memory controls.
- Existing small-document behavior remains regression-compatible unless a deliberate MIR-AI correctness change is documented and tested.
