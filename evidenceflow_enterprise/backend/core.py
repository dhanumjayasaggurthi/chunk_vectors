"""Small, dependable contracts shared by API and workers."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any


class Status(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {Status.COMPLETED, Status.REVIEW_REQUIRED, Status.FAILED, Status.CANCELLED}


def extraction_key(document_id: str, version: str, profile: str) -> str:
    """Stable cache identity for source version and extraction configuration."""
    if not all((document_id, version, profile)):
        raise ValueError("All key components must be set")
    return sha256("\0".join((document_id, version, profile)).encode()).hexdigest()


@dataclass(frozen=True)
class Evidence:
    page: int
    excerpt: str
    value: str

    def verify(self, pages: dict[int, str]) -> bool:
        if self.page <= 0 or not self.value.strip() or not self.excerpt.strip():
            return False
        source = pages.get(self.page)
        return bool(source and self.excerpt in source and self.value in self.excerpt)


@dataclass
class DocumentResult:
    document_id: str
    source_version: str
    status: Status
    fields: dict[str, Evidence] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    summary: str | None = None

    def accepted(self, pages: dict[int, str]) -> bool:
        return self.status == Status.COMPLETED and not self.issues and all(
            evidence.verify(pages) for evidence in self.fields.values()
        )


def validate_fields(candidates: dict[str, Evidence], pages: dict[int, str]) -> tuple[dict[str, Evidence], list[str]]:
    verified: dict[str, Evidence] = {}
    issues: list[str] = []
    for field_name, candidate in candidates.items():
        if candidate.verify(pages):
            verified[field_name] = candidate
        else:
            issues.append(f"Unverified source evidence: {field_name}")
    return verified, issues
