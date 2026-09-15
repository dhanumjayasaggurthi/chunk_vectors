# MIR-AI Production Acceptance Gates

This document defines evidence-based production acceptance for MIR-AI. A capability is not called production-qualified merely because code exists or CI passes.

## Contractual requirements

Production qualification requires all of the following:

- PDF and DOCX parsing of text, tables, charts and images; scanned content uses OCR only when required.
- Semantic chunking preserves headings and atomic semantic units, removes recurring headers/footers, targets 1200–1500 tokens with 100-token overlap, and never truncates a table merely to meet the token target.
- Tables retain the complete extractor-returned row/column matrix, page-specific BBOX provenance, original table text and a factual generated summary. Multi-page stitching must be evidence-based and must not fabricate cells.
- Chunk consumers must be able to obtain the contractual fields: `id`, `created_at`, `content`, `full_doc_id`, `file_path`, `sourcefile_url`, `bbox`, `original_text`, `page_label`, `_node_content`, `_node_type`, `report_title`, `study_id`, `artifact_name`, `compound_number`.
- The 50 study metadata fields are compared with authoritative RimDocs first. Existing authoritative values are not re-extracted. Only missing fields are extraction candidates, with raw evidence, extraction status, normalization status and provenance retained.
- Embeddings remain Azure OpenAI `text-embedding-3-large`, API `2025-04-01-preview`, 3072 dimensions, cosine unless an approved requirements change and retrieval benchmark authorizes another contract.
- Failures preserve exact service, operation, status/error code when supplied, request ID when supplied, retryability, observed message, traceback location and resolution guidance. Unobserved root causes are never invented.

## Accuracy gate

The stated acceptance target is >=99% table/metadata extraction accuracy and <=1% chunk/metadata error. These numbers MUST NOT be claimed until measured on the Business-approved golden set. CI/unit tests are necessary engineering evidence but are not a substitute for the golden-set accuracy measurement.

For every golden-set run retain: source version/hash, processing fingerprint, expected values, observed values, field/table/page provenance, mismatch classification and reviewer disposition. Any unsupported or ambiguous value is a failure/flag, not a guessed success.

## Infrastructure gate

Before production release, run `--plan`, `--preflight-only`, then representative 1-document, 10-document and 100-document qualification runs against the actual authorized S3/NAS, RimDocs input, Google Vision, J&J Azure/APIM and PostgreSQL/pgvector environment. Permanent dependency HTTP 4xx responses must be resolved from the returned dependency evidence; concurrency tuning must not be used to mask contract errors.

## Large-document gate

Qualification must include representative large and table/OCR-heavy documents, including the largest available class. Capture pages/sec, native pages/sec, OCR pages/sec, table pages/sec, chunks/sec, embedding tokens/sec, API latency/retries/429s, DB latency/retries, CPU/RAM/scratch utilization and end-to-end p50/p95. Throughput settings are promoted only after observed stability; no unmeasured throughput figure is an SLA.

## Release rule

A release is production-qualified only when: exact-head CI is green; schema/compatibility checks pass; real dependency preflight passes; representative ingestion succeeds with resumability/error diagnostics verified; and the Business-approved golden-set accuracy gate passes. Until then the branch is a production candidate, not a certified production release.
