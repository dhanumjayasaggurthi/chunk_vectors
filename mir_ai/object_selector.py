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


def _strip_known_extension(value: str) -> tuple[str, str]:
    text = value.strip()
    lower = text.casefold()
    if lower.endswith(".pdf"):
        return text[:-4], "pdf"
    if lower.endswith(".docx"):
        return text[:-5], "docx"
    return text, ""


def _normalize_object_id(value: object, case_sensitive: bool) -> str:
    text = str(value or "").strip().replace("\\", "/")
    name = PurePosixPath(text).name
    name, _ = _strip_known_extension(name)
    name = name.strip()
    return name if case_sensitive else name.casefold()


def _candidate_parts(canonical_path: str, case_sensitive: bool) -> tuple[str, str]:
    normalized = str(canonical_path).replace("\\", "/")
    name = PurePosixPath(normalized).name
    stem, fmt = _strip_known_extension(name)
    key = stem.strip() if case_sensitive else stem.strip().casefold()
    return key, fmt


def _normalize_discovered_id(value: object, case_sensitive: bool) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    text, _ = _strip_known_extension(text)
    return text if case_sensitive else text.casefold()


def _replace_selection_fields(
    item: T,
    *,
    logical_object_id: str,
    chosen_format: str,
    reason: str,
    available_formats: tuple[str, ...],
) -> T:
    try:
        return replace(
            item,
            logical_object_id=logical_object_id,
            selected_format=chosen_format,
            selection_reason=reason,
            available_formats=available_formats,
        )
    except TypeError:
        return item


def _choose(
    display: str,
    by_format: dict[str, list[T]],
    *,
    enable_pdf: bool,
    enable_docx: bool,
    preferred_format: str,
    strict: bool,
) -> tuple[T | None, SelectionIssue | None]:
    enabled = {"pdf": enable_pdf, "docx": enable_docx}
    format_order = [preferred_format] + [
        fmt for fmt in ("pdf", "docx") if fmt != preferred_format
    ]
    available_formats = tuple(
        fmt for fmt in ("pdf", "docx") if by_format.get(fmt)
    )
    chosen_format = next(
        (fmt for fmt in format_order if enabled[fmt] and by_format.get(fmt)),
        None,
    )

    if chosen_format is None:
        if available_formats:
            return None, SelectionIssue(
                display,
                "FORMAT_DISABLED",
                "Source exists only in a disabled format",
                tuple(
                    getattr(item, "canonical_path", "")
                    for fmt in available_formats
                    for item in by_format[fmt]
                ),
            )
        return None, SelectionIssue(
            display,
            "SOURCE_NOT_FOUND",
            "No PDF or DOCX source was found",
        )

    matches = by_format[chosen_format]
    if len(matches) != 1:
        issue = SelectionIssue(
            display,
            "AMBIGUOUS_SOURCE",
            f"Multiple {chosen_format.upper()} sources match the same logical object ID",
            tuple(getattr(item, "canonical_path", "") for item in matches),
        )
        # Never guess between duplicate physical sources. The `strict` argument
        # remains for API compatibility and future explicitly approved policy.
        return None, issue

    if len(available_formats) > 1 and chosen_format == preferred_format:
        reason = f"{preferred_format.upper()}_PREFERRED_OVER_" + (
            "DOCX" if preferred_format == "pdf" else "PDF"
        )
    elif chosen_format == preferred_format:
        reason = f"{chosen_format.upper()}_AVAILABLE"
    else:
        reason = (
            f"{preferred_format.upper()}_UNAVAILABLE_FALLBACK_"
            f"{chosen_format.upper()}"
        )

    return _replace_selection_fields(
        matches[0],
        logical_object_id=display,
        chosen_format=chosen_format,
        reason=reason,
        available_formats=available_formats,
    ), None


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
    """Resolve one physical PDF/DOCX source for each authoritative object ID."""
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
    for key, display in allowed_order:
        item, issue = _choose(
            display,
            candidates[key],
            enable_pdf=enable_pdf,
            enable_docx=enable_docx,
            preferred_format=preferred_format,
            strict=strict,
        )
        if issue:
            issues.append(issue)
        elif item is not None:
            selected.append(item)

    return SelectionOutcome(tuple(selected), tuple(issues))


def resolve_discovered_sources(
    items: Iterable[T],
    *,
    enable_pdf: bool = True,
    enable_docx: bool = True,
    preferred_format: str = "pdf",
    strict: bool = True,
    case_sensitive: bool = False,
    max_items: int | None = None,
) -> SelectionOutcome:
    """Resolve one representation per discovered logical document.

    This is used when no control table is enabled. A source iterator supplies a
    relative logical ID (path without .pdf/.docx), so the same document present
    as both PDF and DOCX is de-duplicated deterministically while files with the
    same basename in different subfolders remain separate logical documents.
    """
    if not enable_pdf and not enable_docx:
        raise ValueError("At least one of enable_pdf or enable_docx must be true")
    preferred_format = str(preferred_format).strip().lower()
    if preferred_format not in {"pdf", "docx"}:
        raise ValueError("preferred_format must be pdf or docx")

    order: list[str] = []
    display_by_key: dict[str, str] = {}
    candidates: dict[str, dict[str, list[T]]] = {}

    for item in items:
        logical = getattr(item, "logical_object_id", None)
        if not logical:
            canonical = getattr(item, "canonical_path", "")
            name = PurePosixPath(str(canonical).replace("\\", "/")).name
            logical, _ = _strip_known_extension(name)
        display = str(logical).strip().replace("\\", "/")
        key = _normalize_discovered_id(display, case_sensitive)
        if not key:
            continue
        if key not in candidates:
            candidates[key] = {"pdf": [], "docx": []}
            display_by_key[key] = display
            order.append(key)
        _, fmt = _candidate_parts(getattr(item, "canonical_path", ""), case_sensitive)
        if fmt:
            candidates[key][fmt].append(item)

    selected: list[object] = []
    issues: list[SelectionIssue] = []
    for key in order:
        item, issue = _choose(
            display_by_key[key],
            candidates[key],
            enable_pdf=enable_pdf,
            enable_docx=enable_docx,
            preferred_format=preferred_format,
            strict=strict,
        )
        if issue:
            issues.append(issue)
        elif item is not None:
            selected.append(item)
            if max_items and len(selected) >= max_items:
                break

    return SelectionOutcome(tuple(selected), tuple(issues))
