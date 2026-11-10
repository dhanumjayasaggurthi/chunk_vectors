"""Compatibility import: existing-table backfills now use the distributed engine.

The previous single-threaded implementation is available in Git history.
"""
from fast_embedding_runtime import embed_existing

__all__ = ["embed_existing"]
