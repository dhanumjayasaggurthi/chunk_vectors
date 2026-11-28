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


def _pattern(rule):
    if rule["method"] == "label":
        # Search only at line boundaries; capture the rest of the labeled line.
        pattern = r"(?im)^\s*" + re.escape(rule["label"]) + r"\s*[:#\-]\s*(.+?)\s*$"
    else:
        pattern = rule["pattern"]
    return re.compile(pattern,re.MULTILINE)


def _group(match,rule):
    return rule.get("group",1 if match.re.groups else 0)


def _unique_match(texts,rule):
    """One distinct capture tuple across the configured source scope.

    Paired fields (e.g. application type/number) use the same regex and
    scope; ambiguity in either capture makes both unavailable.
    No guessing or choosing the first conflicting application.
    """
    pattern=_pattern(rule)
    identity=None
    found=None
    evidence=None
    for page_number,text in texts:
        if len(text)>250_000:
            return None,None
        for match in pattern.finditer(text):
            values=match.groups() if pattern.groups else (match.group(0),)
            value=match.group(_group(match,rule))
            if not value or not value.strip():
                return None,None
            try:
                converted=_convert(value,rule)
            except (TypeError,ValueError):
                return None,None
            if identity is not None and values!=identity:
                return None,None
            if identity is None:
                identity=values
                found=converted
                start,end=match.span(_group(match,rule))
                evidence={"method":rule["method"],"match_policy":"unique",
                          "page":page_number,"text_start":start,"text_end":end,
                          "coordinate_space":"assembled_native_page_text"}
    return found,evidence


def _extract(text: str, rule: dict):
    if rule["method"]=="constant":
        return _convert(rule["value"],rule)
    if rule.get("match_policy")=="unique":
        return _unique_match([(None,text)],rule)[0]
    match = _pattern(rule).search(text[:250_000])
    if not match:
        return None
    value = match.group(_group(match,rule))
    if not value or not value.strip():
        return None
    return _convert(value,rule)


def extract_document_metadata(struct, config: dict):
    """Extract document-scoped fields and evidence before chunk construction."""
    doc_rules = config.get("document", {})
    pages = sorted(struct.pages, key=lambda p: p.page_number)
    doc_metadata = {}
    evidence = {}
    # Assemble each in-scope page at most once, even for many enabled fields.
    scope=max((rule.get("first_pages",3) for rule in doc_rules.values()
               if rule.get("enabled",True) and rule["method"]!="constant"),default=0)
    page_texts=[(page.page_number,_page_full_text(page)) for page in pages
                if page.page_number<=scope]
    for name, rule in doc_rules.items():
        if not rule.get("enabled",True):
            continue
        found = None
        matching_page = None
        matched_evidence=None
        if rule["method"] == "constant":
            found = _extract("",rule)
        else:
            scope = rule.get("first_pages",3)
            in_scope=((number,text) for number,text in page_texts if number<=scope)
            if rule.get("match_policy")=="unique":
                found,matched_evidence=_unique_match(in_scope,rule)
            else:
                for number,text in in_scope:
                    found=_extract(text,rule)
                    if found is not None:
                        matching_page=number
                        break
        if found is not None:
            doc_metadata[name] = found
            evidence[name] = matched_evidence or (
                {"method":"configured_constant"} if matching_page is None
                else {"method":rule["method"],"page":matching_page}
            )
        elif rule.get("required",False):
            raise ValueError("Required document metadata not found: " + name)
    return doc_metadata,evidence


def attach_chunk_metadata(chunks, config: dict, doc_metadata: dict) -> None:
    """Apply deterministic chunk-local rules to already constructed chunks."""
    chunk_rules = config.get("chunk", {})

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


def attach_metadata(struct, chunks: list, config: dict) -> dict:
    """Extract document fields once and chunk fields only from local text.

    When a field is required but unavailable, fail rather than fabricate.
    Sample values inform expected formats only; they are never used as data.
    """
    doc_metadata,evidence=extract_document_metadata(struct,config)
    attach_chunk_metadata(chunks,config,doc_metadata)
    struct.metadata_evidence = evidence
    return doc_metadata
