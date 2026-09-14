from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Iterable, Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SelectionIssue:
    object_id: str
    status: str
    detail: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectionOutcome:
    selected: tuple[object, ...]
    issues: tuple[SelectionIssue, ...]


def _normalize_object_id(value: object, case_sensitive: bool) -> str:
    text = str(value or "").strip().replace("\\", "/")
    name = PurePosixPath(text).name
    lower = name.casefold()
    if lower.endswith(".pdf"):
        name = name[:-4]
    elif lower.endswith(".docx"):
        name = name[:-5]
    name = name.strip()
    return name if case_sensitive else name.casefold()


def _candidate_parts(canonical_path: str, case_sensitive: bool) -> tuple[str, str]:
    normalized = str(canonical_path).replace("\\", "/")
    name = PurePosixPath(normalized).name
    lower = name.casefold()
    if lower.endswith(".pdf"):
        fmt = "pdf"
        stem = name[:-4]
    elif lower.endswith(".docx"):
        fmt = "docx"
        stem = name[:-5]
    else:
        return "", ""
    key = stem.strip() if case_sensitive else stem.strip().casefold()
    return key, fmt


def resolve_preferred_sources(
    items: Iterable[T],
    object_ids: Sequence[object],
    *,
    enable_pdf: bool = True,
    enable_docx: bool = True,
    preferred_format: str = "pdf",
    strict: bool = True,
    case_sensitive: bool = False,
) -> SelectionOutcome:
    """Resolve one physical PDF/DOCX source for each logical object ID.

    The authoritative object list contains extensionless logical IDs. Source
    listing order is intentionally ignored. Candidates are collected first so
    PDF preference cannot be defeated by S3/NAS listing order.

    Items are expected to be dataclass instances with a ``canonical_path``
    attribute. When possible, the selected instance is copied with
    ``logical_object_id``, ``selected_format``, ``selection_reason`` and
    ``available_formats`` populated.
    """
    if not enable_pdf and not enable_docx:
        raise ValueError("At least one of enable_pdf or enable_docx must be true")
    preferred_format = str(preferred_format).strip().lower()
    if preferred_format not in {"pdf", "docx"}:
        raise ValueError("preferred_format must be pdf or docx")

    allowed_order: list[tuple[str, str]] = []
    seen_allowed: set[str] = set()
    for raw in object_ids:
        display = str(raw or "").strip()
        key = _normalize_object_id(raw, case_sensitive)
        if not key or key in seen_allowed:
            continue
        seen_allowed.add(key)
        allowed_order.append((key, display))

    candidates: dict[str, dict[str, list[T]]] = {
        key: {"pdf": [], "docx": []} for key, _ in allowed_order
    }
    for item in items:
        canonical = getattr(item, "canonical_path", "")
        key, fmt = _candidate_parts(canonical, case_sensitive)
        if key in candidates and fmt:
            candidates[key][fmt].append(item)

    selected: list[object] = []
    issues: list[SelectionIssue] = []
    enabled = {"pdf": enable_pdf, "docx": enable_docx}
    format_order = [preferred_format] + [fmt for fmt in ("pdf", "docx") if fmt != preferred_format]

    for key, display in allowed_order:
        by_format = candidates[key]
        available_formats = tuple(fmt for fmt in ("pdf", "docx") if by_format[fmt])
        chosen_format = next(
            (fmt for fmt in format_order if enabled[fmt] and by_format[fmt]),
            None,
        )

        if chosen_format is None:
            if available_formats:
                issues.append(
                    SelectionIssue(
                        display,
                        "FORMAT_DISABLED",
                        "Source exists only in a disabled format",
                        tuple(
                            getattr(item, "canonical_path", "")
                            for fmt in available_formats
                            for item in by_format[fmt]
                        ),
                    )
                )
            else:
                issues.append(
                    SelectionIssue(display, "SOURCE_NOT_FOUND", "No PDF or DOCX source was found")
                )
            continue

        matches = by_format[chosen_format]
        if len(matches) != 1:
            issue = SelectionIssue(
                display,
                "AMBIGUOUS_SOURCE",
                f"Multiple {chosen_format.upper()} sources match the same logical object ID",
                tuple(getattr(item, "canonical_path", "") for item in matches),
            )
            issues.append(issue)
            if strict:
                continue
            continue

        item = matches[0]
        if len(available_formats) > 1 and chosen_format == preferred_format:
            reason = f"{preferred_format.upper()}_PREFERRED_OVER_" + (
                "DOCX" if preferred_format == "pdf" else "PDF"
            )
        elif chosen_format == preferred_format:
            reason = f"{chosen_format.upper()}_AVAILABLE"
        else:
            reason = f"{preferred_format.upper()}_UNAVAILABLE_FALLBACK_{chosen_format.upper()}"

        try:
            item = replace(
                item,
                logical_object_id=display,
                selected_format=chosen_format,
                selection_reason=reason,
                available_formats=available_formats,
            )
        except TypeError:
            pass
        selected.append(item)

    return SelectionOutcome(tuple(selected), tuple(issues))
