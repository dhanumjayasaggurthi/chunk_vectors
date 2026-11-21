-- Ingestion v2 isolated work ledger. Apply only through the explicit
-- "init-ledger --apply" CLI command in a non-production database first.
-- Does not alter legacy ingestion or embedding_runtime tables.
--
-- PostgreSQL 13+; no extensions required.
CREATE SCHEMA IF NOT EXISTS ingestion_v2_runtime;

CREATE TABLE IF NOT EXISTS ingestion_v2_runtime.jobs (
  job_key           text PRIMARY KEY,
  document_id       text NOT NULL,
  source_uri        text NOT NULL,
  source_sha256     text NOT NULL,
  source_size       bigint NOT NULL CHECK (source_size >= 0),
  page_count        integer NOT NULL CHECK (page_count > 0),
  range_pages       integer NOT NULL CHECK (range_pages > 0),
  policy            jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT source_sha_valid CHECK (length(source_sha256) = 64)
);

CREATE TABLE IF NOT EXISTS ingestion_v2_runtime.ranges (
  job_key           text NOT NULL REFERENCES ingestion_v2_runtime.jobs(job_key),
  start_page        integer NOT NULL CHECK (start_page >= 0),
  end_page          integer NOT NULL,
  status            text NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','RUNNING','SUCCESS','FAILED')),
  attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  owner             text,
  claim_token       text,
  lease_until       timestamptz,
  next_attempt_at   timestamptz NOT NULL DEFAULT now(),
  artifact_uri      text,
  artifact_sha256   text,
  ocr_candidates    integer NOT NULL DEFAULT 0 CHECK (ocr_candidates >= 0),
  error_code        text,
  error_message     text,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_key, start_page),
  CONSTRAINT positive_range CHECK (end_page > start_page),
  CONSTRAINT active_claim_consistency CHECK (
    (status = 'RUNNING' AND owner IS NOT NULL AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (status <> 'RUNNING' AND owner IS NULL AND claim_token IS NULL AND lease_until IS NULL)
  ),
  CONSTRAINT success_artifact_consistency CHECK (
    status <> 'SUCCESS' OR (artifact_uri IS NOT NULL AND artifact_sha256 IS NOT NULL)
  )
);

-- Partial indexes let claims avoid scanning SUCCESS and FAILED rows.
CREATE INDEX IF NOT EXISTS ranges_ready_claim_idx
 ON ingestion_v2_runtime.ranges(job_key, next_attempt_at, start_page)
 WHERE status='PENDING';

CREATE INDEX IF NOT EXISTS ranges_expired_claim_idx
 ON ingestion_v2_runtime.ranges(job_key, lease_until, start_page)
 WHERE status='RUNNING';

CREATE TABLE IF NOT EXISTS ingestion_v2_runtime.range_failures (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_key           text NOT NULL,
  start_page        integer NOT NULL,
  attempt           integer NOT NULL,
  error_code        text NOT NULL,
  error_message     text,
  worker_id         text,
  occurred_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (job_key, start_page)
    REFERENCES ingestion_v2_runtime.ranges(job_key, start_page)
);
CREATE INDEX IF NOT EXISTS range_failures_job_time_idx
 ON ingestion_v2_runtime.range_failures(job_key, occurred_at DESC);
