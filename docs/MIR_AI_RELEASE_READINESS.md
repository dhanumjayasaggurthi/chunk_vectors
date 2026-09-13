# MIR-AI release readiness

## Scope

Branch: `feature/MIR-AI-2026-09-12`

Production baseline / merge base: `299f9d3bfaf1518e451282e36d544b6ae6d9ada7` (`current working production code`).

The implementation is additive: legacy production modules are not replaced. MIR-AI code is isolated under `mir_ai/` and writes to `mirai_*` database objects.

## Software gates implemented

- bounded page and document concurrency with backpressure
- page-by-page durable state rather than whole-document rendered-image retention
- direct page resume for PDF processing
- renewable database leases and deterministic idempotent generation/chunk identifiers
- safe generation activation: previous ACTIVE output remains available until replacement is complete
- explicit retry cap and error states
- PDF and DOCX ingestion
- native text, tables, images, charts/graphs, OCR
- PDF table BBOX/page provenance
- DOCX logical provenance without fabricated rendered coordinates
- table continuation handling and table+summary atomicity
- heading-aware semantic chunking with configured 1200-1500 token target and 100-token overlap
- no silent truncation of oversize atomic tables
- text-embedding-3-large / 3072 / 2025-04-01-preview validation
- RimDocs-first overlap processing across the 50 source-visible metadata fields
- evidence-grounded LLM metadata validation; unsupported fields remain missing/invalid
- native + enriched evidence used when locating target metadata sections
- structured logging and operational documentation
- PostgreSQL/pgvector CI integration coverage

## Scale evidence

During development an actual generated 12,000-page PDF was streamed through the lightweight PDF manifest path. The observed peak resident memory was approximately 139 MB for that run. This is a development stress measurement, not a guarantee for every document: OCR density, rendered page size, image resolution, table complexity, worker settings, and external API concurrency materially affect memory and throughput.

The architecture therefore relies on bounded queues, worker limits, immediate persistence, and dropping rendered bytes after use rather than relying on the measured number as a capacity assumption.

## CI gate

The GitHub Actions workflow compiles the MIR-AI package and runs pytest against a real PostgreSQL 16 + pgvector service. Integration coverage includes schema creation, generation claiming, page persistence, enriched metadata candidate selection, vector(3072) persistence, cosine retrieval, page totals, and atomic activation.

A release candidate must not be called software-green until the workflow for the exact branch HEAD reports success.

## External qualification gates — not software facts

The following remain dependent on information or approval not present in the supplied requirements and therefore are not invented by this implementation:

1. **Business accuracy certification.** The requirement is >=99% table/metadata extraction accuracy and <=1% chunking/metadata error, but the supplied material states the golden set is still open with Business. The branch must be benchmarked against the approved labelled set before those numbers are claimed.
2. **Live RimDocs mapping.** RimDocs is authoritative, but the approved production API/table/key mapping was not supplied. Batch mode therefore requires an explicit authoritative JSONL handover; the provider boundary can be replaced by the approved live interface without changing extraction semantics.
3. **Vector database final selection.** The supplied requirement leaves PGVector versus Elastic open. A PGVector implementation exists, with exact full-precision cosine retrieval as the accuracy-first default; this is not represented as a Business/Data Hub selection.
4. **Rendered DOCX BBOX.** XML does not contain reliable rendered page geometry. Exact DOCX rendered BBOX/page coordinates require an approved rendering service if Business confirms those coordinates are mandatory for DOCX.
5. **Multilingual handling.** The supplied requirement leaves translation/skip behavior and mapping open. No translation policy is invented in this branch.

## Production rollout sequence

1. Deploy branch code into a non-production environment with the production-compatible configuration and credentials.
2. Initialize only the additive `mirai_*` schema.
3. Run the Business sample dataset with the authoritative RimDocs handover.
4. Compare tables, chunks, metadata values, missing/null flags, and retrieval results against the accepted labelled/golden output.
5. Correct measurable discrepancies without weakening provenance/error handling.
6. Run subsequent review bundles and failure-injection/scale tests with production quotas.
7. Obtain GRA/Business sign-off on the measured quality gate and unresolved deployment choices.
8. Only then promote/cut over; do not replace the last known-good generation during re-ingestion.

## Release principle

A green software test suite demonstrates implementation correctness for covered cases. It does **not** establish the contractual >=99% business extraction accuracy. That claim is intentionally withheld until measured against the approved dataset.
