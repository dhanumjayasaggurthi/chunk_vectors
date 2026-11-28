-- EXAMPLE ONLY. Replace example_schema/regulatory_chunks with the approved existing output.
-- Run by DBA outside transaction blocks. No new table or view.
-- Keep existing primary key(chunk_id) and doc_id B-tree.
-- Run after initial bulk chunk load, or concurrently on live target with capacity monitoring.
CREATE INDEX CONCURRENTLY IF NOT EXISTS regulatory_chunks_english_fts_idx
    ON example_schema.regulatory_chunks USING gin (to_tsvector('simple', chunk_text));

-- Only after correct model/dimensions and vector population are verified.
-- Index build consumes DB CPU/RAM/I/O and must not compete uncontrolled with capture.
CREATE INDEX CONCURRENTLY IF NOT EXISTS regulatory_chunks_vec_cos_idx
    ON example_schema.regulatory_chunks USING hnsw (chunk_vector vector_cosine_ops);

-- Check existing names/definitions and indisvalid; IF NOT EXISTS is not definition validation.
-- ANALYZE example_schema.regulatory_chunks; -- DBA after bulk load.
