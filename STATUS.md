# Feature status: configurable processing profiles

Branch: feature-configurable-processing-profiles. Target: main. Branch work is opt-in and not a production release.

## Implemented

- [x] Existing CLI defaults / legacy route preserved.
- [x] JSON profile loading, validation, allowlisted CLI overrides, fingerprints.
- [x] Deterministic document/chunk metadata with inheritance and overrides.
- [x] Opt-in tokenizer-limited, heading-aware chunking and page provenance.
- [x] Isolated profile PostgreSQL tables and per-document transactional replacement.
- [x] Profile-aware source skipping; bounded profiled worker futures.
- [x] Configurable SQL chunk table embedding-only backfill without PDF/OCR.
- [x] Configurable extracted document/page text table reuse.
- [x] Initial unit tests, CI workflow, architecture documentation and guide.

## Required validation before production rollout

- [x] GitHub Actions profile CI passes: Python syntax plus 14 unit/ephemeral PostgreSQL-pgvector tests, including native-PDF ingestion and extracted-content reuse (run 38192365882).
- [ ] Stage SQL mapping, transaction, permission, pgvector dimension tests.
- [ ] Regulatory document quality/coverage evaluation with ground truth.
- [ ] OCR, tables, TOC, no-TOC, overlapping headings and large-file fixtures.
- [ ] Crash recovery, parallel run contention, throttling/failure injection.
- [ ] Million-document throughput, memory, cost, connection pool load test.
- [ ] Security and production change approval.

## Verified scope

- Schema-mapped embedding backfill only updates missing NULL vectors and is idempotent in test DB.
- Profile replacement uses a single transaction and retains previous content on failed validation.
- Generated native PDF and previously extracted SQL text both create profiled chunks end-to-end.
- Bounded profile summaries retain accurate totals after the detail cap.
- These are synthetic CI fixtures; enterprise production workloads remain unverified.

## Known scope boundaries

- [ ] Arbitrary destination schema mappings (mapped input supported; profiled output uses separate fixed tables).
- [ ] Model-routing / alternate embedding dimension (current 1536).
- [ ] LLM metadata extraction with evidence/confidence gates.
- [ ] Printed-TOC recognition, high-fidelity tables, durable PDF extraction cache.
- [ ] Retrieval index/UI for profiled outputs.
- [ ] Legacy unbounded queue, early RUNNING transitions, non-atomic upserts.
- [ ] Shared database connection pool throughout profiled operations.

Do not interpret pushed commits or passing unit tests as proof of live production readiness.
