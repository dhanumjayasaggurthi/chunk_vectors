"""Exact-fidelity retrieval from the PostgreSQL fidelity store.

The retrieval path never re-typesets a section. It rehydrates the stored layout
manifest, verifies the DB-resident source PDF by SHA-256, and then copies the
requested source page/clip directly from those stored PDF bytes.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from layout_preserver import extract_content_pdf, extract_section_pdf, find_text_matches
from layout_store import get_source_blob, load_document_layout


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_verified(doc_id: str):
    layout = load_document_layout(doc_id)
    if layout is None:
        raise KeyError(f"layout manifest not found for doc_id={doc_id}")
    blob = get_source_blob(doc_id)
    if blob is None:
        raise KeyError(f"source PDF bytes not found for doc_id={doc_id}")
    data = bytes(blob["source_bytes"])
    digest = _sha256_bytes(data)
    if digest != layout.source_sha256 or digest != blob["source_sha256"]:
        raise ValueError(
            "stored source integrity failure: "
            f"layout={layout.source_sha256} source_row={blob['source_sha256']} actual={digest}"
        )
    if int(blob["byte_length"]) != len(data):
        raise ValueError(
            f"stored source length mismatch: row={blob['byte_length']} actual={len(data)}"
        )
    return layout, blob, data


def retrieve_source_pdf(doc_id: str, output_pdf: str | Path) -> Path:
    """Write the original PDF from PostgreSQL exactly, byte-for-byte."""
    _, _, data = _load_verified(doc_id)
    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return output


def retrieve_section_pdf(doc_id: str, section_title: str, output_pdf: str | Path) -> Path:
    """Retrieve one section using DB layout fragments and DB-resident source bytes."""
    layout, _, data = _load_verified(doc_id)
    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="epod-retrieve-") as td:
        source = Path(td) / "source.pdf"
        source.write_bytes(data)
        return extract_section_pdf(source, layout, section_title, output)


def retrieve_content_pdf(
    doc_id: str,
    literal_text: str,
    output_pdf: str | Path,
    *,
    occurrence: int = 0,
    padding: float = 2.0,
) -> Path:
    """Retrieve an exact vector clip around one literal text occurrence."""
    layout, _, data = _load_verified(doc_id)
    matches = find_text_matches(layout, literal_text)
    if occurrence < 0 or occurrence >= len(matches):
        raise KeyError(
            f"text occurrence {occurrence} not found for {literal_text!r}; matches={len(matches)}"
        )
    match = matches[occurrence]
    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="epod-retrieve-") as td:
        source = Path(td) / "source.pdf"
        source.write_bytes(data)
        return extract_content_pdf(
            source,
            match["page_number"],
            match["bbox"],
            output,
            padding=padding,
        )


def retrieval_metadata(doc_id: str) -> dict[str, Any]:
    """Return integrity and inventory metadata for diagnostics/UI."""
    layout, blob, data = _load_verified(doc_id)
    return {
        "doc_id": doc_id,
        "file_name": blob["file_name"],
        "media_type": blob["media_type"],
        "source_sha256": layout.source_sha256,
        "byte_length": len(data),
        "page_count": layout.page_count,
        "section_count": len(layout.sections),
        "schema_version": layout.schema_version,
    }
