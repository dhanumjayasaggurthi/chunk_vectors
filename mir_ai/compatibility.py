from __future__ import annotations

"""Requirement-contract projection for normalized MIR-AI chunk records.

The production schema intentionally remains normalized.  This module exposes the
legacy/contract field names without duplicating source-of-truth data or inventing
values.  Missing values remain None/empty rather than being inferred.
"""

CONTRACT_FIELDS = (
    "id", "created_at", "content", "full_doc_id", "file_path", "sourcefile_url",
    "bbox", "original_text", "page_label", "_node_content", "_node_type",
    "report_title", "study_id", "artifact_name", "compound_number",
)


def _first(mapping, *keys):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def project_chunk_contract(chunk: dict, document: dict | None = None) -> dict:
    """Project normalized chunk/document data to the exact contractual field names.

    No fallback fabricates provenance.  `original_text` defaults only to the stored
    chunk text because that is the persisted source representation for a chunk;
    business fields are read from authoritative/persisted metadata when present.
    """
    document = document or {}
    metadata = chunk.get("metadata") or {}
    business = document.get("business_metadata") or {}
    page_labels = chunk.get("page_labels") or []
    table_bboxes = chunk.get("table_bboxes") or []
    content_types = chunk.get("content_types") or []
    canonical_path = document.get("canonical_path")
    source_url = _first(chunk, "source_url") or _first(document, "source_url")
    content = _first(chunk, "chunk_text", "content") or ""

    result = {
        "id": _first(chunk, "chunk_id", "id"),
        "created_at": chunk.get("created_at"),
        "content": content,
        "full_doc_id": _first(chunk, "doc_id", "full_doc_id"),
        "file_path": canonical_path,
        "sourcefile_url": source_url,
        "bbox": table_bboxes,
        "original_text": metadata.get("original_text", content),
        "page_label": page_labels,
        "_node_content": metadata.get("_node_content", content),
        "_node_type": metadata.get("_node_type", content_types),
        "report_title": _first(metadata, "report_title", "Document Title") or _first(business, "report_title", "Document Title"),
        "study_id": _first(metadata, "study_id", "Study ID") or _first(business, "study_id", "Study ID"),
        "artifact_name": _first(metadata, "artifact_name") or _first(business, "artifact_name"),
        "compound_number": _first(metadata, "compound_number", "Compound Number") or _first(business, "compound_number", "Compound Number"),
    }
    return {field: result.get(field) for field in CONTRACT_FIELDS}
