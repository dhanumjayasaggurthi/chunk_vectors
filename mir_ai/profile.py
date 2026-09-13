from __future__ import annotations

import hashlib
import json

# Bump whenever extraction/chunking/embedding semantics change in a way that
# requires a fresh generation for the same source bytes.
PROCESSING_PROFILE_VERSION = "mir-ai-2026-09-13-v4"
MAX_GENERATION_ATTEMPTS = 3


def processing_fingerprint(settings) -> str:
    payload = {
        "profile_version": PROCESSING_PROFILE_VERSION,
        "ocr_render_dpi": settings.ocr_render_dpi,
        "scanned_text_threshold": settings.scanned_text_threshold,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "embedding_api_version": settings.embedding_api_version,
        "enable_embeddings": settings.enable_embeddings,
        "chunk_target_min_tokens": settings.chunk_target_min_tokens,
        "chunk_target_max_tokens": settings.chunk_target_max_tokens,
        "chunk_overlap_tokens": settings.chunk_overlap_tokens,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_runtime(settings) -> None:
    settings.validate()
    if settings.heartbeat_seconds >= settings.lease_seconds:
        raise ValueError("heartbeat_seconds must be shorter than lease_seconds")
