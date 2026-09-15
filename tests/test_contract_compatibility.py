from mir_ai.compatibility import CONTRACT_FIELDS, project_chunk_contract


def test_contract_projection_has_exact_required_fields_and_no_invention():
    chunk = {
        "chunk_id": "c1",
        "doc_id": "d1",
        "chunk_text": "authoritative persisted chunk text",
        "page_labels": ["12"],
        "content_types": ["table"],
        "table_bboxes": [{"page": 12, "bbox": [1, 2, 3, 4]}],
        "source_url": "s3://bucket/report.pdf",
        "metadata": {},
        "created_at": "2026-09-15T00:00:00Z",
    }
    document = {
        "canonical_path": "reports/report.pdf",
        "source_url": "s3://bucket/report.pdf",
        "business_metadata": {"Study ID": "STUDY-1", "Compound Number": "CMP-7"},
    }
    result = project_chunk_contract(chunk, document)
    assert tuple(result) == CONTRACT_FIELDS
    assert result["id"] == "c1"
    assert result["content"] == chunk["chunk_text"]
    assert result["original_text"] == chunk["chunk_text"]
    assert result["bbox"] == chunk["table_bboxes"]
    assert result["study_id"] == "STUDY-1"
    assert result["compound_number"] == "CMP-7"
    assert result["report_title"] is None
    assert result["artifact_name"] is None


def test_contract_projection_prefers_persisted_chunk_metadata_over_business_fallback():
    chunk = {
        "chunk_id": "c2", "doc_id": "d2", "chunk_text": "text",
        "metadata": {"report_title": "Persisted title", "study_id": "S2", "original_text": "raw evidence"},
    }
    document = {"business_metadata": {"Document Title": "Business title", "Study ID": "S1"}}
    result = project_chunk_contract(chunk, document)
    assert result["report_title"] == "Persisted title"
    assert result["study_id"] == "S2"
    assert result["original_text"] == "raw evidence"
