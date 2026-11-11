# Single YAML intake and pipeline contract

Use one profile per use case: profiles/document_pipeline_use_case.yml

The file captures business ownership, source volumes, layout preservation,
document metadata, example search questions, acceptance targets, security,
native extraction, chunking, and future embedding/target-table requirements.

## Workflow

1. Copy the example to an approved project profile named for the use case.
2. Business owner and data steward provide the requirements, evidence examples,
   access constraints, representative sample files, and acceptance metrics.
3. Engineering reviews the extraction, chunking, metadata rules, and future
   embedding/output strategy. Do not place credentials in the file.
4. Validate without performing any work:

    ./.venv/bin/python -m ingestion_v2.cli validate --profile profiles/your_case.yml

5. The supplied example starts in draft status. Plan/read operations are allowed,
   but extraction and chunk export reject an unapproved versioned YAML.
6. After approval, record the business and data approvers and an ISO date,
   set pilot_approved, and run an isolated two-range native PDF pilot:

    ./.venv/bin/python -m ingestion_v2.cli extract --pdf /approved/sample.pdf --output /tmp/v2-pilot --profile profiles/your_case.yml --max-ranges 2

Only execute this in a separate worktree/virtual environment. The production
ingestion entrypoint and active worker processes are not modified.

## Division of responsibility

Business owners provide use_case, source, content_requirements, metadata field
definitions and sample values, search_requirements, acceptance and security.
Engineering reviews extraction/chunking technical defaults, embedding_request
and output_request and owns validation, evaluations, approvals and deployment.

## Current support vs planned

CURRENT: bounded native PDF page ranges, checkpointed exports, exact token-aware
chunking, deterministic evidence-backed label/regex metadata extraction.

PLANNED: GCP OCR worker pool, cross-page structural stitching, table/figure
reconstruction, LLM metadata, cross-host leases, versioned PostgreSQL writes,
embedding generation and retrieval indexing.

The requested_future_features section is an intake wish list. It does not
execute work. Turning on unsupported execution flags fails validation.

## Safety and governance

- This configuration is never a storage location for passwords/API keys.
- Sample values are format guidance, not extracted results or fallback values.
- The approval field is a business workflow checkpoint, not IAM authorization.
- Validate versioned schema and refuse unknown keys and duplicate YAML keys.
- Preserve source document and vector-index versions across changes.
- Evaluate model-specific tokenization and retrieval quality before accepting
  consumer embedding-text settings.
- Do not execute external AI processing without independent policy approval.

See docs/INGESTION_V2_FOUNDATION.md for the isolation and quality gates.