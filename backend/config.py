"""Environment-backed configuration; `config.ini` is supported for existing JnJ clients."""
from __future__ import annotations
import os
import re
from pathlib import Path
from configparser import ConfigParser
from sqlalchemy.engine import URL


ROOT = Path(__file__).resolve().parents[1]



def s3_ini_setting(key: str, default: str = "") -> str:
    parser = ConfigParser(interpolation=None)

    path = os.getenv(
        "JNJ_CONFIG_PATH",
        str(ROOT / "config.ini")
    )

    parser.read(path, encoding="utf-8")

    return parser.get(
        "S3",
        key,
        fallback=default
    ).strip()

def postgres_url_from_ini() -> str:
    path = Path(
        os.getenv("JNJ_CONFIG_PATH", str(ROOT / "config.ini"))
    ).expanduser()

    parser = ConfigParser(interpolation=None)

    if not parser.read(path, encoding="utf-8"):
        raise RuntimeError("config.ini not found")

    if not parser.has_section("POSTGRESQL"):
        raise RuntimeError("Missing [POSTGRESQL] section")

    pg = parser["POSTGRESQL"]

    for key in ("host", "database", "user", "schema"):
        if not pg.get(key, "").strip():
            raise RuntimeError(
                f"Missing PostgreSQL setting: {key}"
            )

    schema = pg["schema"].strip()

    # Use a simple, lowercase PostgreSQL schema name.
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError(
            "Schema must use lowercase letters, digits "
            "and underscores, and cannot start with a digit"
        )

    password = (
        os.getenv("POSTGRES_PASSWORD")
        or pg.get("password", "")
    )

    if not password:
        raise RuntimeError("PostgreSQL password not configured")

    query = {
        "sslmode": pg.get("sslmode", "require"),
        "options": f"-csearch_path={schema}",
    }

    if pg.get("sslrootcert", ""):
        query["sslrootcert"] = pg["sslrootcert"]

    url = URL.create(
        drivername="postgresql+psycopg",
        username=pg["user"],
        password=password,
        host=pg["host"],
        port=pg.getint("port", fallback=5432),
        database=pg["database"],
        query=query,
    )

    return url.render_as_string(hide_password=False)



def setting(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


MODE = setting("APP_MODE", "demo").lower()
DATABASE_URL = setting("DATABASE_URL") or (
    postgres_url_from_ini()
    if MODE == "live"
    else f"sqlite:///{ROOT / 'evidenceflow.db'}"
)
SF_TABLE = setting("SF_METADATA_TABLE", "DEV_RAW.RIMDOCS_CONTROL_STG.ALL_METADATA_JOINED_WITH_TA_CTI")
MAX_OCR_PAGES = int(setting("MAX_OCR_PAGES", "40"))
OCR_ENABLED = setting("ENABLE_GOOGLE_OCR", "false").lower() == "true"
LLM_KIND = setting("LLM_KIND", "gpt4o").lower()
PROFILE_VERSION = setting("EXTRACTION_PROFILE_VERSION", "pbrer-v1")

# Explicit environment overrides take priority.
# Otherwise, reuse the existing config.ini [S3] section.

_source_buckets = (
    setting("ALLOWED_S3_BUCKETS")
    or s3_ini_setting("bucket")
)

_source_prefixes = (
    setting("ALLOWED_S3_PREFIXES")
    or s3_ini_setting("prefix")
)

ALLOWED_S3_BUCKETS = {
    value.strip()
    for value in _source_buckets.split(",")
    if value.strip()
}

ALLOWED_S3_PREFIXES = tuple(
    value.strip().lstrip("/")
    for value in _source_prefixes.split(",")
    if value.strip()
)

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
