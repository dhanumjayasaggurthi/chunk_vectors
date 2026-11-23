"""Configurable deterministic metadata extraction for existing and new documents.

Never invents values from sample_values; records provenance at page level
when the extracted source supplies page numbers. No LLM/network requests.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from chunker import _page_full_text


def _convert(value, rule):
    value_type = rule.get("type","string")
    if value_type in ("string","enum"):
        result = str(value).strip()
    elif value_type == "integer":
        result = int(str(value).replace(",","").strip())
    elif value_type == "number":
        result = float(str(value).replace(",","").strip())
        if not math.isfinite(result):
            raise ValueError("Nonfinite metadata numbers are unsupported")
    elif value_type == "boolean":
        normalized = str(value).strip().casefold()
        if normalized not in {"true","false","yes","no","1","0"}:
            raise ValueError("Invalid boolean metadata")
        result = normalized in {"true","yes","1"}
    elif value_type == "date":
        result = dt.date.fromisoformat(str(value).strip()).isoformat()
    else:
        raise ValueError("Unsupported metadata type")
    if rule.get("allowed_values") is not None and result not in rule["allowed_values"]:
        raise ValueError("Metadata value is outside allowed_values")
    return result


def _extract(text: str, rule: dict):
    method = rule["method"]
    if method == "constant":
        return _convert(rule["value"],rule)
    if method == "label":
        # Search only at line boundaries; capture the rest of the labeled line.
        pattern = r"(?im)^\s*" + re.escape(rule["label"]) + r"\s*[:#\-]\s*(.+?)\s*$"
    else:
        pattern = rule["pattern"]
    match = re.search(pattern, text[:250_000], re.MULTILINE)
    if not match:
        return None
    value = match.group(1) if match.lastindex else match.group(0)
    if not value or not value.strip():
        return None
    return _convert(value,rule)


def attach_metadata(struct, chunks: list, config: dict) -> dict:
    """Extract document fields once and chunk fields only from local text.

    When a field is required but unavailable, fail rather than fabricate.
    Sample values inform expected formats only; they are never used as data.
    """
    doc_rules = config.get("document", {})
    chunk_rules = config.get("chunk", {})
    pages = sorted(struct.pages, key=lambda p: p.page_number)
    doc_metadata = {}
    evidence = {}
    for name, rule in doc_rules.items():
        if not rule.get("enabled",True):
            continue
        found = None
        matching_page = None
        if rule["method"] == "constant":
            found = _extract("",rule)
        else:
            scope = rule.get("first_pages",3)
            for page in pages:
                if page.page_number > scope:
                    continue
                found = _extract(_page_full_text(page),rule)
                if found is not None:
                    matching_page = page.page_number
                    break
        if found is not None:
            doc_metadata[name] = found
            evidence[name] = (
                {"method":"configured_constant"} if matching_page is None
                else {"method":rule["method"],"page":matching_page}
            )
        elif rule.get("required",False):
            raise ValueError("Required document metadata not found: " + name)

    inherit = config.get("inherit_document_fields",True)
    for chunk in chunks:
        local = {}
        for name, rule in chunk_rules.items():
            if not rule.get("enabled",True):
                continue
            value = _extract(chunk.chunk_text,rule)
            if value is not None:
                local[name] = value
            elif rule.get("required",False):
                raise ValueError("Required chunk metadata not found: " + name)
        chunk.document_metadata = dict(doc_metadata)
        structural=getattr(chunk,"chunk_metadata",{}) or {}
        collisions=set(structural).intersection(local)
        if collisions:
            raise ValueError("Configured metadata conflicts with reserved structural metadata: "
                             + ", ".join(sorted(collisions)))
        chunk.chunk_metadata = {**structural,**local}
        chunk.effective_metadata = {
            **(doc_metadata if inherit else {}),**chunk.chunk_metadata
        }
        if not getattr(chunk,"headers",None):
            chunk.headers = [chunk.section_title] if chunk.section_title else []
    struct.metadata_evidence = evidence
    return doc_metadata
