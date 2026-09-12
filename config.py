"""
config.py
=========
Central configuration for the EPOD ingestion pipeline.
Reads from the same config.ini used by the rest of the project.
All tuneable parameters live here — never scatter magic numbers across files.
"""

import configparser
import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Load config.ini  (same file used by azure_client.py)
# ─────────────────────────────────────────────────────────────────────────────
_cfg = configparser.ConfigParser()
_cfg.read("config.ini")


def _get(section: str, key: str, fallback=None):
    try:
        return _cfg[section][key]
    except KeyError:
        if fallback is not None:
            return fallback
        raise KeyError(f"Missing [{section}] {key} in config.ini")


# ─────────────────────────────────────────────────────────────────────────────
# Postgres  (same DB as RIMDocs RAG)
# ─────────────────────────────────────────────────────────────────────────────
POSTGRES = {
    "host":     _get("POSTGRES", "host"),
    "port":     int(_get("POSTGRES", "port", 5432)),
    "database": _get("POSTGRES", "database"),
    "user":     _get("POSTGRES", "user"),
    "password": _get("POSTGRES", "password"),
    "schema": _get("POSTGRES", "schema"),
    "folder": _get("POSTGRES", "folder"),
}

# Schema to create EPOD tables inside (keeps them separate from rimdocs_nipo)
DB_SCHEMA = _get("POSTGRES", "schema")
FOLDER = _get("POSTGRES", "folder")

# ─────────────────────────────────────────────────────────────────────────────
# Google Cloud Vision
# ─────────────────────────────────────────────────────────────────────────────
GCP_KEY_PATH = _get("GCP", "gcp_key_path")

# ─────────────────────────────────────────────────────────────────────────────
# Azure OpenAI  (re-uses azure_client.py functions directly — no duplication)
# ─────────────────────────────────────────────────────────────────────────────
# Embedding dimension for text-embedding-3-small
EMBEDDING_DIM = 1536

# Max tokens to send to embedding model per chunk
# text-embedding-3-small hard limit = 8191 tokens  (~6000 words)
# We stay conservative to leave headroom
EMBEDDING_MAX_TOKENS = 6000

# Batch size for embedding API calls (max texts per request)
EMBEDDING_BATCH_SIZE = 16

# ─────────────────────────────────────────────────────────────────────────────
# Document source: "nas" (UNC/local path) or "s3" (Amazon S3 bucket)
# Set [PATHS] source_type in config.ini to switch between them.
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_TYPE = _get("PATHS", "source_type", "nas").lower()   # "nas" | "s3"

# NAS / local path (used when SOURCE_TYPE = "nas")
DOCS_ROOT = Path(_get("PATHS", "docs_root", fallback="."))

# File types to process
SUPPORTED_EXTENSIONS = {".pdf"}

# ─────────────────────────────────────────────────────────────────────────────
# S3 settings  (only required when SOURCE_TYPE = "s3")
# ─────────────────────────────────────────────────────────────────────────────
S3_BUCKET            = _get("S3", "bucket",            fallback="")
S3_PREFIX            = _get("S3", "prefix",            fallback="")   # folder within bucket
S3_REGION            = _get("S3", "region",            fallback="us-east-1")
S3_PROFILE           = _get("S3", "profile",           fallback="")   # named profile in ~/.aws/credentials
S3_ENDPOINT_URL      = _get("S3", "endpoint_url",      fallback="")   # custom/VPC endpoint; leave blank for public AWS
S3_ACCESS_KEY_ID     = _get("S3", "access_key_id",     fallback="")   # leave blank when using profile or IAM role
S3_SECRET_ACCESS_KEY = _get("S3", "secret_access_key", fallback="")
S3_SESSION_TOKEN     = _get("S3", "session_token",     fallback="")   # optional STS token
S3_TEMP_DIR          = _get("S3", "temp_dir",          fallback="")   # local download dir; blank = system temp

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline feature toggles
# ─────────────────────────────────────────────────────────────────────────────
# Set enable_embeddings = false in [PIPELINE] to skip Azure embedding calls.
# Chunks are still stored in the DB but chunk_vector will be NULL.
ENABLE_EMBEDDINGS = _get("PIPELINE", "enable_embeddings", "true").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
# Chunking strategy
# ─────────────────────────────────────────────────────────────────────────────

# Level 1 — Section chunking
# Min characters a TOC entry's section must have to be kept as its own chunk
SECTION_MIN_CHARS = 200

# Level 2 — Page-level grouping
# How many consecutive pages to group under the same detected heading
MAX_PAGES_PER_GROUP = 10

# Level 3 — Size-based splitting (sliding window)
# Target chunk size in characters (~4 chars per token on average)
CHUNK_TARGET_CHARS   = 4000    # ~1000 tokens
CHUNK_OVERLAP_CHARS  = 400     # ~100 tokens overlap to preserve context

# Hard upper limit — chunks larger than this get force-split regardless
CHUNK_MAX_CHARS = 24000        # ~6000 tokens (embedding model limit)

# Minimum chunk size — anything shorter is merged with next or discarded
CHUNK_MIN_CHARS = 150

# ─────────────────────────────────────────────────────────────────────────────
# PDF rendering (for Vision OCR)
# ─────────────────────────────────────────────────────────────────────────────
# DPI to render pages at before sending to Vision API
# 150 = fast,  200 = balanced,  300 = best quality (use for scanned docs)
OCR_RENDER_DPI = 200

# A page is considered "scanned / image-heavy" if its extractable text
# is shorter than this many characters (triggers Vision OCR)
SCANNED_PAGE_TEXT_THRESHOLD = 100

# ─────────────────────────────────────────────────────────────────────────────
# Table extraction
# ─────────────────────────────────────────────────────────────────────────────
# pdfplumber table detection settings
TABLE_SETTINGS = {
    "vertical_strategy":   "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance":      3,
}

# How to serialise a table row into text
# "markdown" → | col1 | col2 |   "csv" → col1, col2
TABLE_FORMAT = "markdown"

# ─────────────────────────────────────────────────────────────────────────────
# Retry / resilience
# ─────────────────────────────────────────────────────────────────────────────
# How many times to retry a failed API call before giving up
API_MAX_RETRIES   = 3
API_RETRY_DELAY_S = 5       # seconds between retries (doubles each attempt)

# How many times to retry a failed document before marking it ERROR
DOC_MAX_RETRIES = 2

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
LOG_DIR      = Path("logs")
LOG_FILE     = LOG_DIR / "epod_pipeline.log"
LOG_MAX_BYTES   = 10 * 1024 * 1024   # 10 MB per log file
LOG_BACKUP_COUNT = 5                  # keep 5 rotated files

# ─────────────────────────────────────────────────────────────────────────────
# Concurrency
# ─────────────────────────────────────────────────────────────────────────────
# Number of worker threads for parallel page processing within a single PDF
# Keep low to avoid hammering the Vision / embedding APIs
PAGE_WORKERS = 4
