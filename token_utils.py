"""Token accounting helpers with an exact tiktoken path and safe fallback."""
from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger("epod.token_utils")
_warned_fallback = False


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken
        try:
            return tiktoken.encoding_for_model("text-embedding-3-large")
        except Exception:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def has_exact_tokenizer() -> bool:
    return _encoder() is not None


def count_tokens(text: str) -> int:
    """Count embedding tokens. Uses tiktoken in production; conservative fallback otherwise."""
    global _warned_fallback
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    if not _warned_fallback:
        logger.warning("tiktoken unavailable; using conservative token estimator")
        _warned_fallback = True
    # Regulatory prose contains IDs, units and punctuation that tokenize more densely
    # than simple word counts. This estimator intentionally errs high.
    pieces = re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?|[^\w\s]", text, flags=re.UNICODE)
    return max(1, int(len(pieces) * 1.12))


def truncate_tokens(text: str, max_tokens: int) -> str:
    if not text or max_tokens <= 0:
        return ""
    enc = _encoder()
    if enc is not None:
        toks = enc.encode(text, disallowed_special=())
        return enc.decode(toks[:max_tokens])
    # Fallback by binary searching characters using the conservative counter.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def tail_tokens(text: str, token_count: int) -> str:
    if not text or token_count <= 0:
        return ""
    enc = _encoder()
    if enc is not None:
        toks = enc.encode(text, disallowed_special=())
        return enc.decode(toks[-token_count:])
    # Find shortest suffix whose estimate is at least requested size.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if count_tokens(text[mid:]) > token_count:
            lo = mid + 1
        else:
            hi = mid
    return text[lo:]
