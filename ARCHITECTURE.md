# Architecture: configurable document processing

## Decision

Preserve production NAS/S3 crawlers, PyMuPDF, Google Vision, pdfplumber, GPT table processing, the legacy chunker, the Azure embedder, and PostgreSQL. When no profile is passed, use the existing pipeline. No premature microservices, job platform, or agent architecture.

## Opt-in profiled PDF flow

Validated JSON profile -> existing NAS/S3 discovery -> source/profile completion test -> existing PDF/OCR/table extraction -> legacy or token-aware chunker -> deterministic metadata enrichment -> optional existing embedder -> isolated atomic PostgreSQL storage.

Profiled discovery compares source and profile fingerprints. NAS keeps the mtime fast path; S3 uses ETag/LastModified. Only 2 x doc_workers futures are in flight. An advisory lock prevents simultaneous writes for the same document/profile.

## Reuse paths

1. Already-extracted content: read page/doc text from a table using configured names, reconstruct DocumentStructure, then chunk/enrich/embed/persist. Original PDF and OCR are not accessed. Text/page fingerprint detects changes.
2. Already-chunked content: SELECT mapped IDs/text where vectors are NULL, batch embed, UPDATE the mapped vector column. Keyset pagination and batch transactions bound memory.

## Metadata and chunking

JSON profiles declare document-level and chunk-level fields using deterministic label, regex, and constant rules. Document fields are computed once; chunk fields can override the inherited value. Unfound metadata stays missing. Headers and page ranges remain attached.

Advanced chunking accepts exact tokenizer-based minimum, maximum and overlap. Heading paths and content types are retained. Small sections may merge; short final sections remain. Oversized elements can split, so oversized tables need explicit quality review. Only cl100k_base and o200k_base are enabled.

## Persistence

Profile output uses PostgreSQL tables profile_results_<folder> (doc ID, profile hash, source hash, count) and profile_chunks_<folder> (chunk ID, text, JSONB metadata, headings, pages and optional vector(1536)). One transaction replaces the chunks and completion marker for a document/profile. Legacy chunks remain untouched.

Custom input tables are supported through column mapping. Arbitrary custom output columns, different embedding dimensions, automatic ANN indexes and retrieval UI remapping are NOT implemented. Profile tables should be adopted by the retrieval layer only after index/query planning.

## Scale and failure modes

Profile futures are bounded and API concurrency uses existing semaphores. Input table queries paginate by indexed document/chunk IDs. Real throughput depends on document sizes, network, API quotas, DB connections and HNSW indexing costs.

Legacy ingestion still builds its queue in memory, marks RUNNING during discovery, and can replace chunks non-atomically. These pre-existing issues are documented, not disguised as fixed.

All SQL identifiers in optional mapped-source paths are validated and quoted; values use bind parameters. Keep secrets out of profiles and enforce tenant/content permissions at the database.

## Rollout

Compile + unit tests; then staging SQL/provider integration, sampled metadata accuracy, source-change retries, kill/restart recovery, and million-document-scale throughput/memory benchmarking. Canary first. Rollback by removing profile flags. Do not merge or deploy before signoff.

## Dimension configuration

Embedding dimensions are selected by `embedding.dimensions` in a profile, or by `[AZURE_OPENAI_EMBEDDING] dimensions` in the existing config.ini for legacy/global defaults (1536 when unspecified for compatibility). Azure's request, embedding-vector shape validation and the mapped existing-table dimension check use the configured dimension. The HAQ embedding-only path supports `vector(3072)` without hardcoding a particular table or model. The isolated document-profile store still has a separate fixed `vector(1536)` destination: dimension-independent profile storage migrations and its retrieval index remain follow-up work, not a prerequisite for `--embed-existing`.
