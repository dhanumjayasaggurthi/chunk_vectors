from dataclasses import dataclass

from mir_ai.object_selector import (
    resolve_discovered_sources,
    resolve_preferred_sources,
)


@dataclass(frozen=True)
class Item:
    canonical_path: str
    logical_object_id: str | None = None
    selected_format: str | None = None
    selection_reason: str = ""
    available_formats: tuple[str, ...] = ()


def test_pdf_wins_when_both_formats_exist():
    outcome = resolve_preferred_sources(
        [
            Item("s3://bucket/path/STUDY-001.docx"),
            Item("s3://bucket/path/STUDY-001.pdf"),
        ],
        ["STUDY-001"],
        preferred_format="pdf",
    )
    assert len(outcome.selected) == 1
    selected = outcome.selected[0]
    assert selected.canonical_path.endswith(".pdf")
    assert selected.selected_format == "pdf"
    assert selected.available_formats == ("pdf", "docx")
    assert selected.selection_reason == "PDF_PREFERRED_OVER_DOCX"
    assert not outcome.issues


def test_docx_is_used_when_pdf_is_disabled():
    outcome = resolve_preferred_sources(
        [
            Item("/archive/STUDY-002.pdf"),
            Item("/archive/STUDY-002.docx"),
        ],
        ["STUDY-002"],
        enable_pdf=False,
        enable_docx=True,
        preferred_format="pdf",
    )
    assert [item.selected_format for item in outcome.selected] == ["docx"]
    assert outcome.selected[0].selection_reason == "PDF_UNAVAILABLE_FALLBACK_DOCX"


def test_only_available_allowed_format_is_used():
    outcome = resolve_preferred_sources(
        [Item("/archive/STUDY-003.docx")],
        ["STUDY-003"],
        enable_pdf=True,
        enable_docx=True,
        preferred_format="pdf",
    )
    assert outcome.selected[0].selected_format == "docx"
    assert outcome.selected[0].selection_reason == "PDF_UNAVAILABLE_FALLBACK_DOCX"


def test_missing_and_disabled_formats_are_reported():
    outcome = resolve_preferred_sources(
        [Item("/archive/STUDY-004.docx")],
        ["STUDY-004", "STUDY-005"],
        enable_pdf=True,
        enable_docx=False,
    )
    statuses = {issue.object_id: issue.status for issue in outcome.issues}
    assert statuses == {
        "STUDY-004": "FORMAT_DISABLED",
        "STUDY-005": "SOURCE_NOT_FOUND",
    }


def test_duplicate_preferred_sources_are_not_guessed():
    outcome = resolve_preferred_sources(
        [
            Item("/archive/a/STUDY-006.pdf"),
            Item("/archive/b/STUDY-006.pdf"),
            Item("/archive/a/STUDY-006.docx"),
        ],
        ["STUDY-006"],
        enable_pdf=True,
        enable_docx=True,
        preferred_format="pdf",
    )
    assert not outcome.selected
    assert len(outcome.issues) == 1
    assert outcome.issues[0].status == "AMBIGUOUS_SOURCE"
    assert len(outcome.issues[0].candidates) == 2


def test_all_files_mode_prefers_pdf_for_same_relative_logical_document():
    outcome = resolve_discovered_sources(
        [
            Item("/root/sub/STUDY-100.docx", logical_object_id="sub/STUDY-100"),
            Item("/root/sub/STUDY-100.pdf", logical_object_id="sub/STUDY-100"),
            Item("/root/sub/STUDY-101.docx", logical_object_id="sub/STUDY-101"),
        ],
        preferred_format="pdf",
    )
    assert [item.logical_object_id for item in outcome.selected] == [
        "sub/STUDY-100",
        "sub/STUDY-101",
    ]
    assert outcome.selected[0].selected_format == "pdf"
    assert outcome.selected[1].selected_format == "docx"


def test_all_files_mode_keeps_same_basename_in_different_subfolders_separate():
    outcome = resolve_discovered_sources(
        [
            Item("/root/a/STUDY.pdf", logical_object_id="a/STUDY"),
            Item("/root/b/STUDY.pdf", logical_object_id="b/STUDY"),
        ]
    )
    assert [item.logical_object_id for item in outcome.selected] == [
        "a/STUDY",
        "b/STUDY",
    ]
    assert not outcome.issues
