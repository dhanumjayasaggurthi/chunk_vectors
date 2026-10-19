"""Environment-backed configuration; `config.ini` is supported for existing JnJ clients."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def setting(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


MODE = setting("APP_MODE", "demo").lower()
DATABASE_URL = setting("DATABASE_URL", f"sqlite:///{ROOT / 'evidenceflow.db'}")
SF_TABLE = setting("SF_METADATA_TABLE", "DEV_RAW.RIMDOCS_CONTROL_STG.ALL_METADATA_JOINED_WITH_IA_CII")
MAX_OCR_PAGES = int(setting("MAX_OCR_PAGES", "40"))
OCR_ENABLED = setting("ENABLE_GOOGLE_OCR", "false").lower() == "true"
LLM_KIND = setting("LLM_KIND", "gpt4o").lower()
PROFILE_VERSION = setting("EXTRACTION_PROFILE_VERSION", "pbrer-v1")
ALLOWED_S3_PREFIXES = tuple(x.strip() for x in setting("ALLOWED_S3_PREFIXES").split(",") if x.strip())
ALLOWED_S3_BUCKETS = {x.strip() for x in setting("ALLOWED_S3_BUCKETS").split(",") if x.strip()}
LEASE_SECONDS = int(setting("DOCUMENT_LEASE_SECONDS", "1200"))
RETRY_LIMIT = int(setting("DOCUMENT_RETRIES", "3"))

# Enterprise service limits (set with measured quotas and document size distribution).
MAX_FILE_MB = int(setting("MAX_PDF_MB", "1200"))
MAX_PAGES = int(setting("MAX_PDF_PAGES", "20000"))
CHUNK_PAGES = int(setting("CHUNK_PAGES", "64"))
OCR_ENGINE = setting("OCR_ENGINE", "textract_async")  # none | textract_async
CHECKPOINT_BUCKET = setting("CHECKPOINT_BUCKET")
CHECKPOINT_PREFIX = setting("CHECKPOINT_PREFIX", "evidenceflow/checkpoints/")
SQS_TASK_URL = setting("SQS_TASK_URL")
TASK_LEASE_SECONDS = int(setting("TASK_LEASE_SECONDS", "300"))
TASK_MAX_ATTEMPTS = int(setting("TASK_MAX_ATTEMPTS", "5"))
TASK_REPUBLISH_SECONDS = int(setting("TASK_REPUBLISH_SECONDS", "90"))
OIDC_ISSUER = setting("OIDC_ISSUER")
OIDC_AUDIENCE = setting("OIDC_AUDIENCE")
OIDC_JWKS_URL = setting("OIDC_JWKS_URL")

SF_ENTITLEMENTS_TABLE = setting('SF_ENTITLEMENTS_TABLE')  # Optional corporate ACL mapping table.

SQS_INTERACTIVE_URL = setting('SQS_INTERACTIVE_URL') # Optional priority lane for jobs <=5k IDs
