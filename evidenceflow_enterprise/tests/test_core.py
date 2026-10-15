from pathlib import Path
import pytest
from backend.core import Evidence, Status, extraction_key, validate_fields
from backend.runner import process


def test_evidence_must_exist_on_correct_page():
    pages = {12: "The International Birth Date is 12 December 2008."}
    assert Evidence(12, "International Birth Date is 12 December 2008", "12 December 2008").verify(pages)
    assert not Evidence(11, "International Birth Date is 12 December 2008", "12 December 2008").verify(pages)
    assert not Evidence(12, "Invented excerpt", "12 December 2008").verify(pages)


def test_no_unverified_fields_accepted():
    verified, issues = validate_fields({"date": Evidence(1, "January", "January")}, {1: "December"})
    assert verified == {}
    assert len(issues) == 1


def test_checkpoint_resume_for_1601_pages(tmp_path: Path):
    identity = extraction_key("doc", "v1", "profile")
    reads = []
    def source(start, end):
        reads.append((start, end))
        return [{"page": n, "text": f"PAGE {n}"} for n in range(start, end + 1)]
    def extract(pages):
        return {"last_page": Evidence(1601, "PAGE 1601", "PAGE 1601")}
    first = process("doc", "v1", identity, 1601, source, extract, tmp_path)
    assert first.status == Status.COMPLETED
    assert len(reads) == 26
    second = process("doc", "v1", identity, 1601, source, extract, tmp_path)
    assert second.status == Status.COMPLETED
    assert len(reads) == 26


def test_corrupt_checkpoint_fails_closed(tmp_path: Path):
    identity = extraction_key("doc", "v1", "profile")
    def source(start, end):
        return [{"page": n, "text": str(n)} for n in range(start, end + 1)]
    process("doc", "v1", identity, 2, source, lambda p: {}, tmp_path, chunk_pages=2)
    checkpoint = tmp_path / identity / "00000000.json"
    checkpoint.write_text('[{"page":1,"text":"1"}]')
    with pytest.raises(ValueError, match="coverage"):
        process("doc", "v1", identity, 2, source, lambda p: {}, tmp_path, chunk_pages=2)
