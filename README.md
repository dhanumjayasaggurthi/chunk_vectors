# EvidenceFlow Enterprise

**Document Intelligence Console for 1 to millions of RIMDOCS IDs, including 1,600+ page PDFs.** Built on Snowflake, source S3, JnJ Azure OpenAI, PostgreSQL, SQS, AWS Textract and S3 checkpoints. One codebase; API, dispatcher and executor deployment roles.

## Quick preview (no corporate credentials)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
APP_MODE=demo python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Demo sample facts are explicitly `SCREENSHOT_ONLY`; it never contacts real Snowflake/S3/Azure. Demo SQLite and background threads are **not** an enterprise runtime.

## Production roles

All use the same Docker image:

```bash
# Apply schema once to a controlled PostgreSQL environment (DBA/change approval):
APP_MODE=live alembic upgrade head

# Stateless API (behind private TLS ALB / corporate SSO):
APP_MODE=live uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 2

# Durable coordinator(s): Snowflake resolution, outbox publication and crash reconciliation:
APP_MODE=live python -m backend.enterprise_worker --role dispatcher

# Horizontally scaled workers; one active stage per task container:
APP_MODE=live python -m backend.enterprise_worker --role executor
```

**Required production services:** existing authorized Snowflake table, versioned S3 source bucket, PostgreSQL Multi-AZ app database, two SQS queues + DLQ, private S3 checkpoint bucket, Textract access, corporate OIDC, secrets store, approved Azure OpenAI endpoints and rate limits. Deploy with the template in `infra/terraform` after adapting it to your VPC and security controls. Terraform does not create your corporate RDS/IDP/network; it expects these pre-provisioned systems.

## Example environment variables

```env
APP_MODE=live
DATABASE_URL=postgresql+psycopg://app:<credential>@db.internal:5432/evidenceflow
SF_METADATA_TABLE=DEV_RAW.RIMDOCS_CONTROL_STG.ALL_METADATA_JOINED_WITH_IA_CII
SF_ENTITLEMENTS_TABLE=SECURITY.APP_RIMDOCS_DOCUMENT_ACCESS
ALLOWED_S3_BUCKETS=itx-bjv-rddlx-prod
ALLOWED_S3_PREFIXES=landing/regulatory/rimdocs/Rimdocs_Extract_Prod/Safety/
CHECKPOINT_BUCKET=<private-approved-bucket>
CHUNK_PAGES=64
MAX_PDF_MB=1200
MAX_PDF_PAGES=20000
OCR_ENGINE=textract_async
SQS_TASK_URL=<bulk-queue-url>
SQS_INTERACTIVE_URL=<interactive-queue-url>
OIDC_ISSUER=<corporate-issuer>
OIDC_AUDIENCE=<api-audience>
OIDC_JWKS_URL=<corporate-keys-url>
LLM_KIND=gpt4o
EXTRACTION_PROFILE_VERSION=pbrer-v2
```

`SF_ENTITLEMENTS_TABLE` is a **suggested schema contract**, not a known existing JnJ table. It must have `TENANT_ID`, `PRINCIPAL_ID`, `R_OBJECT_ID`. Access is denied to non-admin users if no entitlement source is configured. Privileged users require a **cryptographically validated** `rimdocs:read_all` scope. Never ship a shared hardcoded token to the browser.

The Azure adapter uses the exact payload distinction in your supplied JnJ sample: GPT-4o sends `max_tokens` (and may send `temperature`); GPT-5 sends `max_completion_tokens` and no unsupported temperature/top-p params. Credentials can be sourced from individual secret-injected env variables, or an approved mounted `config.ini` path. Snowflake's private key can be loaded from a mounted file or secret-injected `SNOWFLAKE_PRIVATE_KEY_PEM`. Use managed task roles for AWS.

## UI / API

- Paste up to 5,000 IDs, or upload CSV. For live uploads above 8MB, the browser uses resumable 8MB presigned S3 multipart parts; the job ingest continues in the background. Small uploads still use streamed FastAPI CSV.
- Jobs, per-document status, review/error states, cancellation, retries, activity endpoint, and CSV/JSON exports.
- Over 50,000 documents, exports become background S3 artifacts. UI polls their completion and obtains a short-lived download URL.
- View normalized critical dates/product fields, page number, original extracted excerpt, and proof status. Open the exact source S3 **VersionId** via an authorized 5-minute URL.
- UI and API are protected by OIDC in live mode; a secure SPA/host corporate SSO token bridge must be supplied by your organization's identity team.

Main endpoints:

```text
POST /api/v1/jobs
POST /api/v1/jobs/upload
POST /api/v1/uploads/init
GET  /api/v1/jobs/{job_id}/uploads/{upload_id}
GET  /api/v1/jobs/{job_id}/uploads/{upload_id}/parts/{part_no}
POST /api/v1/jobs/{job_id}/uploads/{upload_id}/complete
GET  /api/v1/jobs/{job_id}
GET  /api/v1/jobs/{job_id}/activity
GET  /api/v1/jobs/{job_id}/results
GET  /api/v1/jobs/{job_id}/documents/{r_object_id}
GET  /api/v1/jobs/{job_id}/documents/{r_object_id}/source-url
POST /api/v1/jobs/{job_id}/retry
POST /api/v1/jobs/{job_id}/cancel
POST /api/v1/jobs/{job_id}/exports
GET  /api/v1/jobs/{job_id}/exports/{export_id}
```

## Verification

```bash
pip install -r requirements-dev.txt
python -m pytest -q
node --check frontend/app.js
```

Automated tests include a generated **1,601-page PDF** with chunk checkpointing/replay, OCR source-page remapping, tenant isolation, cancellation, task lease fencing, duplicate delivery protection and API/UI smoke tests. They **do not** demonstrate actual million-ID throughput or certified OCR accuracy; those require integration/load test environments and a labeled gold set.

## Before go-live

See `ARCHITECTURE.md` for design and limits, `OPERATIONS.md` for SLOs / failure runbooks, and `infra/terraform/README.md` for cloud prerequisites. Corporate secrets, per-document access, Textract permissions, correct Azure endpoint deployment, source S3 versioning, backup/restore, VPC connectivity, genuine PDF accuracy evaluation, SQS scaling, quotas and million-ID load tests must all pass before regulated production use.
