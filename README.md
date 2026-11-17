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
EXTRACTION_PROFILE_VERSION=birth-dates-v1
SF_DOMAIN_COLUMN=<verified-column-name>
SF_COMPONENT_COLUMN=<verified-column-name>
SF_EDMS_COLUMN=<verified-column-name>
SF_PRODUCT_CODE_COLUMN=<verified-column-name>
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

## IBD / DIBD regulatory extraction profile

- PBRER -> IBD, scan first 25 PDF pages and stop at the first labeled birth-date block; multiple explicitly paired products yield multiple CSV rows.
- DSUR -> DIBD, use Section 1 / executive summary introduction. Never substitute IBD for DIBD.
- Only Safety-domain records with Approved or Superseded status are eligible; PBRER additionally requires Full Document / Published Output / Report Body. Current implementation also requires source ingestion STATUS=SUCCESS. Confirm actual Snowflake semantics with the data owner.
- Populate the four `SF_*_COLUMN` variables from **verified Snowflake schema**. Identifier validation rejects unsafe values. No corporate column names are assumed.
- CSV columns: RIMdocs Object ID, EDMS Number, Product Code, Product, IBD, DIBD, Page Number, Other Info. IBD or DIBD is populated according to artifact type. Each product/date pairing is its own row. JSON remains the raw document result with a `fields.birth_date_pairs` array.
- Missing labels, ambiguous product assignment, conflicting dates, or incomplete OCR yield REVIEW_REQUIRED. The program does not reconcile EU reference dates with first-registration dates without a signed-off business rule.
- LLM summarization is bypassed for this regulatory profile. Existing LLM code stays available for other profiles, but is not required for IBD / DIBD.
- Regulated production: supply approved source fixtures, reconcile PDF page numbering with document page labels, measure extraction precision/recall, and validate Snowflake data access, ACL, S3 versioning and OCR. The new profile has not been validated against corporate PDFs.

## Run the IBD / DIBD demonstration

All five examples are **synthetic PDFs reconstructed from the supplied slide scenarios**, not original RIMdocs files.

```bash
git clone --branch feature/ibd-dibd-extraction https://github.com/dhanumjayasaggurthi/chunk_vectors.git
cd chunk_vectors
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Validate PDF -> PyMuPDF -> product/date extractor -> CSV:
python -m scripts.validate_sample_pdfs
# Artifacts: demo_output/results.csv, preview.html and pdfs/

# Start real FastAPI demo, background worker and SQLite database:
APP_MODE=demo python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/?view=results**. The seeded job includes five synthetic sample PDFs and legacy examples. For an isolated check, create a new job with `DEMO-RISP-001`, `DEMO-CANA-002`, `DEMO-PALI-003`, `DEMO-MACI-004`, `DEMO-ERDA-005`. Wait for completion; inspect Product(s), IBD / DIBD, evidence pages, and download the CSV.

**Demo mode is not regulatory verification**. Do not set `APP_MODE=live` for synthetic fixtures.

### Inspect an authorized original PDF locally

```bash
python -m scripts.inspect_pdf /secure/path/report-pbrer.pdf --case IBD --output pbrer-check.json
python -m scripts.inspect_pdf /secure/path/report-dsur.pdf --case DIBD --output dsur-check.json
```

This is a native-text diagnostic without corporate credentials. Missing/unreadable text returns `REVIEW_REQUIRED` and scanned pages require approved OCR before regulatory use. Neither RIMdocs metadata nor source-document access is inferred. Do not commit confidential PDFs or extracted regulated content to this public repository.

### Pre-production checks

- Confirm Snowflake column mappings, approval/component semantics, and true source IDs.
- Evaluate source PDFs against a human-labeled gold set, including scans, tables, line wrapping, and multiple formulations.
- Resolve the Loperamide EU reference date versus first-registration discrepancy through an approved business rule.
- Confirm page number mapping, RBAC, source S3 version fencing, OCR permissions and deployment security before live corporate processing.

## Original PBRER / DSUR PDFs on Windows PowerShell

This mode extracts from **actual original PDFs on the operator's machine**,
not synthetic demo IDs. It is a local evidence review, not regulatory approval,
and requires no Snowflake, AWS, Azure, SQS or corporate database credentials.

```powershell
git checkout feature/ibd-dibd-extraction
git pull origin feature/ibd-dibd-extraction
python -m pip install -r requirements-dev.txt
$env:APP_MODE = "local"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/?view=local**. Choose *Original PDF review*,
select one or more real files (up to five per batch), choose the proper report
type (PBRER→IBD or DSUR→DIBD), and click *Extract original PDFs*.
For mixed report types, upload them separately. Inspect product-date pairs,
PDF page numbers, exact evidence snippets, and the open-original-PDF link.
Download the displayed records with *Export CSV*.

Local mode does **not** verify RIMdocs identifiers: optional Object ID,
EDMS number and product code are user-entered and labeled unverified.
For multiple PDFs, do not share a single set of identifiers. Local processing does not save PDF bytes or evidence in the application
database and makes no S3/LLM/OCR calls. FastAPI may spool multipart uploads to
temporary operating-system storage until the request closes. Use an approved
workstation with appropriately protected temp storage. Selected files remain available in the browser tab
temporarily via local object URLs for page viewing.

Only run the app on **127.0.0.1**, on an approved workstation that complies
with your organization's document classification and retention rules.
The endpoint is disabled in demo/live and refuses non-loopback clients.
The local upload has a 100 MB per-PDF limit. IBD scans only the first 25 PDF
pages; DSUR searches the first 150 pages for Section 1. Image-only pages,
missing dates and ambiguous pairings are flagged for human review; this mode
does not invoke OCR. For enterprise OCR, RIMdocs metadata resolution and
version-fenced source access, use the corporate `APP_MODE=live` architecture
after approved configuration.

Local endpoint and extraction tests:
```powershell
python -m pytest -q tests/test_local_pdf.py
```
