from __future__ import annotations
import hashlib, json
PROCESSING_PROFILE_VERSION = "mir-ai-2026-09-14-v7"
MAX_GENERATION_ATTEMPTS = 3  # compatibility only; runtime uses settings.generation_max_attempts

def processing_fingerprint(settings) -> str:
    payload={
        "profile_version":PROCESSING_PROFILE_VERSION,
        "ocr_render_dpi":settings.ocr_render_dpi,
        "scanned_text_threshold":settings.scanned_text_threshold,
        "embedding_model":settings.embedding_model,
        "embedding_dim":settings.embedding_dim,
        "embedding_api_version":settings.embedding_api_version,
        "enable_embeddings":settings.enable_embeddings,
        "chunk_target_min_tokens":settings.chunk_target_min_tokens,
        "chunk_target_max_tokens":settings.chunk_target_max_tokens,
        "chunk_overlap_tokens":settings.chunk_overlap_tokens,
        "metadata_mode":settings.metadata_mode,
        "requirements_mode":settings.requirements_mode,
    }
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def validate_runtime(settings)->None:
    settings.validate()
