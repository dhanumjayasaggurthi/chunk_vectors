from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import hashlib
import json


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def to_list(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]


@dataclass
class ElementRef:
    """Lightweight page element. Never stores rendered image bytes."""
    element_id: str
    element_type: str
    bbox: Optional[BBox]
    reading_order: int
    text: str = ""
    source_locator: dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    extraction_status: str = "native"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = self.bbox.to_list() if self.bbox else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ElementRef":
        d = dict(d)
        bb = d.get("bbox")
        d["bbox"] = BBox(*bb) if bb else None
        return cls(**d)


@dataclass
class PageRecord:
    page_number: int
    page_label: str
    width: float
    height: float
    elements: list[ElementRef] = field(default_factory=list)
    native_text: str = ""
    header_candidate: str = ""
    footer_candidate: str = ""
    needs_full_ocr: bool = False
    extraction_status: str = "parsed"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "page_label": self.page_label,
            "width": self.width,
            "height": self.height,
            "elements": [e.to_dict() for e in self.elements],
            "native_text": self.native_text,
            "header_candidate": self.header_candidate,
            "footer_candidate": self.footer_candidate,
            "needs_full_ocr": self.needs_full_ocr,
            "extraction_status": self.extraction_status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PageRecord":
        d = dict(d)
        d["elements"] = [ElementRef.from_dict(e) for e in d.get("elements", [])]
        return cls(**d)


@dataclass
class SemanticUnit:
    unit_id: str
    unit_type: str
    text: str
    page_start: int
    page_end: int
    page_labels: list[str]
    section_path: list[str] = field(default_factory=list)
    bboxes: list[dict[str, Any]] = field(default_factory=list)
    atomic: bool = True
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkRecord:
    chunk_id: str
    doc_id: str
    generation_id: str
    chunk_index: int
    text: str
    page_start: int
    page_end: int
    page_labels: list[str]
    section_path: list[str]
    content_types: list[str]
    source_url: str = ""
    table_bboxes: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: Optional[list[float]] = field(default=None, repr=False)


@dataclass(frozen=True)
class MetadataFieldSpec:
    key: str
    source_label: str
    data_type: str


@dataclass
class MetadataValue:
    key: str
    source_label: str
    value: Any
    raw_value: Any = None
    unit: Optional[str] = None
    source: str = "missing"
    status: str = "missing"
    evidence_text: str = ""
    evidence_pages: list[int] = field(default_factory=list)
    confidence: Optional[float] = None
    normalization_status: str = "not_applicable"


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(str(p) for p in parts)
    return f"{prefix}-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
