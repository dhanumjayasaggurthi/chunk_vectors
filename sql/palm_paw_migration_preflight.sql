-- Review in a DBA session before executing against production.
-- Index build can consume substantial I/O, disk, and time.
-- CREATE INDEX CONCURRENTLY must run outside a transaction block.
--
-- 1. Source quality checks (the distinct document count may be expensive).
SELECT COUNT(*) AS source_rows,
       COUNT(*) FILTER (WHERE chunk_id IS NULL) AS null_chunk_ids,
       COUNT(*) FILTER (WHERE doc_id IS NULL) AS null_doc_ids,
       COUNT(*) FILTER (WHERE chunk_text IS NULL OR btrim(chunk_text) = '') AS blank_text_rows,
       COUNT(*) FILTER (WHERE chunk_vector IS NOT NULL) AS already_embedded
FROM palm_paw.palm_pah_jnj_search_results_doc_chunks_clinical;

-- Duplicate chunk IDs undermine safe migration and existing-vector backfill.
SELECT chunk_id, COUNT(*) AS duplicates
FROM palm_paw.palm_pah_jnj_search_results_doc_chunks_clinical
WHERE chunk_id IS NOT NULL
GROUP BY chunk_id HAVING COUNT(*) > 1
ORDER BY duplicates DESC LIMIT 100;

-- 2. Inspect existing indexes before creating additional indexes.
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'palm_paw'
  AND tablename = 'palm_pah_jnj_search_results_doc_chunks_clinical'
ORDER BY indexname;

-- 3. Recommended keyset/document-read index. Execute only if no equivalent
--    index already exists. The source migration does not mutate source rows.
CREATE INDEX CONCURRENTLY IF NOT EXISTS
    palm_paw_clinical_migration_doc_order_idx
ON palm_paw.palm_pah_jnj_search_results_doc_chunks_clinical
    (doc_id, chunk_index, chunk_id);

-- 4. If the current table is not already unique by chunk_id, first resolve
--    duplicates. Then add a unique index before using --embed-existing.
-- CREATE UNIQUE INDEX CONCURRENTLY palm_paw_clinical_chunk_id_unique
-- ON palm_paw.palm_pah_jnj_search_results_doc_chunks_clinical (chunk_id);

-- 5. New profile output is created in DB_SCHEMA.profile_chunks_FOLDER.
--    After a bulk load, create a pgvector index on the chosen output table.
--    A large HNSW build is expensive: schedule it outside peak ingestion.
--    Example after replacing schema/table placeholders:
-- CREATE INDEX CONCURRENTLY output_profile_vector_hnsw_idx
-- ON your_schema.profile_chunks_your_folder
-- USING hnsw (chunk_vector vector_cosine_ops)
-- WITH (m = 16, ef_construction = 64)
-- WHERE chunk_vector IS NOT NULL;
