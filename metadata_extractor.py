"""Deterministic document and chunk metadata extraction using profile field rules."""
from __future__ import annotations

import re
from chunker import _page_full_text


def _extract(text: str, rule: dict):
    method = rule["method"]
    if method == "constant":
        return rule["value"]
    if method == "label":
        # Search only at line boundaries; capture the rest of the labeled line.
        pattern = r"(?im)^\s*" + re.escape(rule["label"]) + r"\s*[:#\-]\s*(.+?)\s*$"
    else:
        pattern = rule["pattern"]
    match = re.search(pattern, text[:250_000], re.MULTILINE)
    if not match:
        return None
    value = match.group(1) if match.lastindex else match.group(0)
    return value.strip() or None


def attach_metadata(struct, chunks: list, config: dict) -> dict:
    """Extract document once; chunk fields only from the chunk's own text.

    Unknown values remain missing: never infer an ungrounded field.
    """
    doc_rules = config.get("document", {})
    chunk_rules = config.get("chunk", {})
    pages = sorted(struct.pages, key=lambda p: p.page_number)
    doc_metadata = {}
    for name, rule in doc_rules.items():
        scope = rule.get("first_pages", 3)
        text = "\n".join(_page_full_text(p) for p in pages if p.page_number <= scope)
        value = _extract(text, rule)
        if value is not None:
            doc_metadata[name] = value

    inherit = config.get("inherit_document_fields", True)
    for chunk in chunks:
        local = {}
        for name, rule in chunk_rules.items():
            value = _extract(chunk.chunk_text, rule)
            if value is not None:
                local[name] = value
        chunk.document_metadata = dict(doc_metadata)
        chunk.chunk_metadata = local
        chunk.effective_metadata = {**(doc_metadata if inherit else {}), **local}
        if not getattr(chunk, "headers", None):
            chunk.headers = [chunk.section_title] if chunk.section_title else []
    return doc_metadata
