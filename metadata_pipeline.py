"""MIR-AI document-metadata overlap and selective-extraction utilities.

RimDocs metadata is authoritative. LLM extraction is intentionally limited to
fields requested by study_metadata_list_u.xlsx that are absent (or null) in
RimDocs. This module does not invent values: every extracted field must carry
source evidence, and unresolved fields remain explicitly missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Any

NULL_LIKE = (None, "", [], {})


def normalize_field_name(name: str) -> str:
    return " ".join(str(name or "").strip().lower().replace("_", " ").split())


def overlap_analysis(target_fields: Iterable[str], rimdocs_metadata: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    authoritative = {normalize_field_name(k): v for k, v in (rimdocs_metadata or {}).items()}
    present, missing = [], []
    for field in target_fields:
        key = normalize_field_name(field)
        if key in authoritative and authoritative[key] not in NULL_LIKE:
            present.append(field)
        else:
            missing.append(field)
    return present, missing


def load_target_fields_from_xlsx(path: str) -> list[str]:
    """Load non-empty unique field names from the first worksheet/column.

    The business workbook's exact column name is not supplied in this task, so
    this intentionally uses the first column and fails loudly on an empty file.
    """
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    fields: list[str] = []
    seen = set()
    for row in ws.iter_rows(min_row=1, values_only=True):
        value = row[0] if row else None
        if value is None:
            continue
        field = str(value).strip()
        if not field or normalize_field_name(field) in {"field", "field name", "metadata field"}:
            continue
        key = normalize_field_name(field)
        if key not in seen:
            seen.add(key); fields.append(field)
    if not fields:
        raise ValueError(f"no target metadata fields found in {path}")
    return fields


@dataclass
class ExtractedField:
    value: Any = None
    evidence: str = ""
    source_section: str = ""
    confidence: float | None = None


@dataclass
class MetadataResult:
    authoritative: dict = field(default_factory=dict)
    extracted: dict = field(default_factory=dict)
    merged: dict = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


def merge_metadata(target_fields: Iterable[str], rimdocs_metadata: Mapping[str, Any], extracted: Mapping[str, Any]) -> MetadataResult:
    """Merge without overwriting non-null authoritative business values."""
    authoritative = dict(rimdocs_metadata or {})
    ext = dict(extracted or {})
    auth_norm = {normalize_field_name(k): (k, v) for k, v in authoritative.items()}
    ext_norm = {normalize_field_name(k): (k, v) for k, v in ext.items()}
    merged = dict(authoritative)
    missing: list[str] = []
    provenance: dict[str, str] = {}
    for field in target_fields:
        key = normalize_field_name(field)
        if key in auth_norm and auth_norm[key][1] not in NULL_LIKE:
            merged[field] = auth_norm[key][1]
            provenance[field] = "RimDocs"
        elif key in ext_norm and ext_norm[key][1] not in NULL_LIKE:
            val = ext_norm[key][1]
            if isinstance(val, ExtractedField):
                if not val.evidence.strip():
                    merged[field] = None
                    missing.append(field)
                    provenance[field] = "MISSING"
                    continue
                merged[field] = val.value
            else:
                merged[field] = val
            provenance[field] = "LLM"
        else:
            merged[field] = None
            missing.append(field)
            provenance[field] = "MISSING"
    return MetadataResult(authoritative=authoritative, extracted=ext, merged=merged,
                          missing_fields=missing, provenance=provenance)


def select_extraction_text(section_text: Mapping[str, str], first_20_pages_text: str = "") -> str:
    """Use required report sections; fall back to first 20 pages when unavailable."""
    wanted = ("summary", "study administration", "protocol", "report approval")
    selected = []
    norm = {normalize_field_name(k): v for k, v in (section_text or {}).items()}
    for name in wanted:
        text = norm.get(name, "")
        if text and text.strip():
            selected.append(f"[{name.upper()}]\n{text.strip()}")
    return "\n\n".join(selected) if selected else (first_20_pages_text or "")


def extract_missing_fields(
    target_fields: Iterable[str],
    rimdocs_metadata: Mapping[str, Any],
    extraction_text: str,
    extractor: Callable[[list[str], str], Mapping[str, Any]],
) -> MetadataResult:
    """Invoke a supplied extractor only for fields missing from RimDocs."""
    _, missing = overlap_analysis(target_fields, rimdocs_metadata)
    if not missing:
        return merge_metadata(target_fields, rimdocs_metadata, {})
    if not extraction_text.strip():
        return merge_metadata(target_fields, rimdocs_metadata, {})
    extracted = dict(extractor(missing, extraction_text) or {})
    # Drop any field the caller returned but business did not authorize for extraction.
    allowed = {normalize_field_name(f) for f in missing}
    extracted = {k: v for k, v in extracted.items() if normalize_field_name(k) in allowed}
    return merge_metadata(target_fields, rimdocs_metadata, extracted)
