"""
embedder.py
===========
Attaches embedding vectors to Chunk objects produced by chunker.py.

  - Calls azure_client.get_embeddings() in batches
  - Handles token-limit overflows  (auto-truncates oversized chunks)
  - Exponential back-off retry on transient API failures
  - Rate-limit awareness  (respects 429 Retry-After headers)
  - Progress logging every N batches
  - Never raises — failed chunks get an empty vector and are logged

Flow:
    chunks  →  batch into groups of EMBEDDING_BATCH_SIZE
            →  get_embeddings([text, text, ...])
            →  attach vector to each chunk.chunk_vector
            →  return chunks  (same list, mutated in-place)

Dependencies:
    azure_client.py  (get_embeddings already written)
"""

from __future__ import annotations

import time
import re
from typing import Optional

from config import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MAX_TOKENS,
    EMBEDDING_DIM,
    API_MAX_RETRIES,
    API_RETRY_DELAY_S,
)
from chunker import Chunk
from logger import get_logger

logger = get_logger("epod.embedder")


# ─────────────────────────────────────────────────────────────────────────────
# Token estimation  (fast approximation — no tiktoken required)
# ─────────────────────────────────────────────────────────────────────────────

# ~4 chars per token is a safe approximation for English/regulatory text
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Rough token count: len(text) / 4, minimum 1."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _truncate_to_token_limit(text: str, max_tokens: int = EMBEDDING_MAX_TOKENS) -> str:
    """
    Truncate text to fit within the embedding model's token limit.
    Cuts at a paragraph or sentence boundary where possible.
    """
    if _estimate_tokens(text) <= max_tokens:
        return text

    max_chars = max_tokens * _CHARS_PER_TOKEN

    # Try paragraph boundary
    para_cut = text.rfind("\n\n", 0, max_chars)
    if para_cut > max_chars * 0.7:
        return text[:para_cut].strip()

    # Try sentence boundary
    sent_cut = max(
        text.rfind(". ", 0, max_chars),
        text.rfind(".\n", 0, max_chars),
    )
    if sent_cut > max_chars * 0.5:
        return text[:sent_cut + 1].strip()

    # Hard cut
    return text[:max_chars].strip()


# ─────────────────────────────────────────────────────────────────────────────
# Rate-limit handling
# ─────────────────────────────────────────────────────────────────────────────

def _extract_retry_after(error_str: str) -> Optional[float]:
    """
    Parse 'Retry-After: N' from a 429 error message string.
    Returns seconds to wait, or None if not found.
    """
    m = re.search(r"retry.after[:\s]+(\d+\.?\d*)", error_str, re.I)
    if m:
        return float(m.group(1))
    # Also look for 'Please retry after N seconds'
    m2 = re.search(r"retry after (\d+)", error_str, re.I)
    if m2:
        return float(m2.group(1))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Core embedding call with retry
# ─────────────────────────────────────────────────────────────────────────────

def _embed_batch(texts: list[str]) -> Optional[list[list[float]]]:
    """
    Call get_embeddings() for a batch of texts.
    Returns list of vectors on success, None on permanent failure.

    Retry policy:
      - Attempt 1..API_MAX_RETRIES
      - Base delay API_RETRY_DELAY_S, doubles each attempt
      - On 429: honour Retry-After header if present
    """
    from azure_client import get_embeddings

    last_error = ""
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            vectors = get_embeddings(texts)

            # Validate response shape
            if not vectors or len(vectors) != len(texts):
                raise ValueError(
                    f"Expected {len(texts)} vectors, got {len(vectors) if vectors else 0}"
                )
            if any(len(v) != EMBEDDING_DIM for v in vectors):
                bad = [i for i, v in enumerate(vectors) if len(v) != EMBEDDING_DIM]
                raise ValueError(f"Wrong vector dim at indices {bad}")

            return vectors

        except Exception as e:
            last_error = str(e)
            error_str  = last_error.lower()

            # 429 rate-limit — honour Retry-After if present
            if "429" in last_error or "rate limit" in error_str:
                wait = _extract_retry_after(last_error) or (API_RETRY_DELAY_S * (2 ** attempt))
                logger.warning(
                    f"Rate-limited (attempt {attempt}/{API_MAX_RETRIES}) "
                    f"— waiting {wait:.0f}s"
                )
                time.sleep(wait)
                continue

            # 400 / token overflow — truncate and retry once
            if ("400" in last_error or "token" in error_str or "length" in error_str) \
                    and attempt == 1:
                logger.warning(
                    f"Token limit error — truncating batch and retrying: {last_error[:120]}"
                )
                texts = [_truncate_to_token_limit(t, EMBEDDING_MAX_TOKENS // 2) for t in texts]
                continue

            # Other transient error — exponential back-off
            if attempt < API_MAX_RETRIES:
                delay = API_RETRY_DELAY_S * (2 ** (attempt - 1))
                logger.warning(
                    f"Embed attempt {attempt}/{API_MAX_RETRIES} failed: "
                    f"{last_error[:100]} — retrying in {delay}s"
                )
                time.sleep(delay)

    logger.error(f"Embedding permanently failed after {API_MAX_RETRIES} attempts: {last_error[:200]}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Zero vector fallback
# ─────────────────────────────────────────────────────────────────────────────

def _zero_vector() -> list[float]:
    """Return a zero vector of the correct dimension as a safe fallback."""
    return [0.0] * EMBEDDING_DIM


# ─────────────────────────────────────────────────────────────────────────────
# Main embedder
# ─────────────────────────────────────────────────────────────────────────────

def embed_chunks(
    chunks:           list[Chunk],
    batch_size:       int  = EMBEDDING_BATCH_SIZE,
    log_every_n:      int  = 10,         # log progress every N batches
    skip_zero_text:   bool = True,       # skip chunks with empty text
) -> list[Chunk]:
    """
    Embed all chunks and attach vectors in-place.

    Parameters
    ----------
    chunks       : list of Chunk objects (from chunker.chunk_document)
    batch_size   : how many texts to send per API call
    log_every_n  : log a progress line every N batches
    skip_zero_text : if True, chunks with empty text get a zero vector silently

    Returns
    -------
    The same list with chunk.chunk_vector populated.
    Chunks that failed embedding get a zero vector (logged as WARNING).
    """
    if not chunks:
        return chunks

    total           = len(chunks)
    embedded_count  = 0
    failed_count    = 0
    skipped_count   = 0
    start_time      = time.time()

    logger.info(f"Embedding {total} chunks in batches of {batch_size}")

    # ── Pre-process: truncate oversized chunk texts ──────────────────────────
    for chunk in chunks:
        if _estimate_tokens(chunk.chunk_text) > EMBEDDING_MAX_TOKENS:
            original_len = len(chunk.chunk_text)
            chunk.chunk_text = _truncate_to_token_limit(chunk.chunk_text)
            logger.debug(
                f"Chunk {chunk.chunk_index} truncated: "
                f"{original_len} → {len(chunk.chunk_text)} chars"
            )

    # ── Process in batches ───────────────────────────────────────────────────
    batch_num = 0
    for batch_start in range(0, total, batch_size):
        batch_chunks = chunks[batch_start: batch_start + batch_size]
        batch_num   += 1

        # Filter out empty-text chunks
        active = []
        for chunk in batch_chunks:
            if not chunk.chunk_text.strip():
                if skip_zero_text:
                    chunk.chunk_vector = _zero_vector()
                    skipped_count += 1
                else:
                    active.append(chunk)
            else:
                active.append(chunk)

        if not active:
            continue

        texts   = [chunk.chunk_text for chunk in active]
        vectors = _embed_batch(texts)

        if vectors is not None:
            for chunk, vector in zip(active, vectors):
                chunk.chunk_vector = vector
            embedded_count += len(active)
        else:
            # Permanent failure — assign zero vectors so pipeline continues
            for chunk in active:
                chunk.chunk_vector = _zero_vector()
            failed_count += len(active)
            logger.warning(
                f"Batch {batch_num} failed — {len(active)} chunks got zero vectors"
            )

        # Progress logging
        if batch_num % log_every_n == 0 or batch_start + batch_size >= total:
            elapsed   = time.time() - start_time
            processed = batch_start + len(batch_chunks)
            rate      = processed / elapsed if elapsed > 0 else 0
            eta       = (total - processed) / rate if rate > 0 else 0
            logger.info(
                f"  Embedded {processed}/{total} chunks  "
                f"({rate:.1f}/s  ETA {eta:.0f}s)  "
                f"failed={failed_count}"
            )

    elapsed = time.time() - start_time
    logger.info(
        f"Embedding complete — "
        f"embedded={embedded_count}  "
        f"skipped={skipped_count}  "
        f"failed={failed_count}  "
        f"elapsed={elapsed:.1f}s"
    )

    if failed_count > 0:
        logger.warning(
            f"{failed_count} chunks have zero vectors — "
            "they will be stored but won't appear in similarity searches"
        )

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Single-chunk embedding  (used for query embedding in the RAG UI)
# ─────────────────────────────────────────────────────────────────────────────

def embed_query(query_text: str) -> list[float]:
    """
    Embed a single query string.
    Returns vector on success, zero vector on failure.
    Used by the RAG retrieval layer.
    """
    if not query_text or not query_text.strip():
        logger.warning("embed_query called with empty text")
        return _zero_vector()

    text    = _truncate_to_token_limit(query_text)
    vectors = _embed_batch([text])

    if vectors:
        return vectors[0]

    logger.error("embed_query failed — returning zero vector")
    return _zero_vector()


# ─────────────────────────────────────────────────────────────────────────────
# Embedding stats
# ─────────────────────────────────────────────────────────────────────────────

def embedding_stats(chunks: list[Chunk]) -> dict:
    """
    Return stats about the embedding state of a chunk list.
    Useful for post-embed validation before DB insert.
    """
    total         = len(chunks)
    with_vector   = sum(1 for c in chunks if any(v != 0.0 for v in c.chunk_vector))
    zero_vector   = sum(1 for c in chunks if all(v == 0.0 for v in c.chunk_vector))
    empty_vector  = sum(1 for c in chunks if not c.chunk_vector)

    token_counts  = [_estimate_tokens(c.chunk_text) for c in chunks]
    avg_tokens    = sum(token_counts) / total if total else 0
    max_tokens    = max(token_counts) if token_counts else 0

    return {
        "total_chunks":    total,
        "with_vector":     with_vector,
        "zero_vector":     zero_vector,
        "empty_vector":    empty_vector,
        "avg_tokens":      round(avg_tokens, 1),
        "max_tokens":      max_tokens,
        "embedding_dim":   EMBEDDING_DIM,
    }
