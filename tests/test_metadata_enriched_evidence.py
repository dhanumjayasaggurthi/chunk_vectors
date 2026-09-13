import unittest

from mir_ai.metadata import MetadataExtractor, candidate_evidence_pages, page_evidence_text
from mir_ai.models import ElementRef, PageRecord


class FakeGateway:
    def __init__(self, payload):
        self.payload = payload
        self.messages = None

    def chat_json(self, messages, **kwargs):
        self.messages = messages
        return self.payload


class TestEnrichedMetadataEvidence(unittest.TestCase):
    def test_page_evidence_includes_ocr_and_table_text(self):
        page = PageRecord(
            7,
            "7",
            600,
            800,
            native_text="Native paragraph",
            elements=[
                ElementRef("e2", "table", None, 2, text="Study ID: S-77"),
                ElementRef("e1", "image", None, 1, text="Report Approval"),
            ],
        )
        text = page_evidence_text(page)
        self.assertIn("Native paragraph", text)
        self.assertLess(text.index("Report Approval"), text.index("Study ID: S-77"))

    def test_section_detection_can_use_enriched_ocr_text(self):
        pages = [
            PageRecord(1, "1", 0, 0, native_text="front matter"),
            PageRecord(
                2,
                "2",
                0,
                0,
                elements=[ElementRef("ocr", "image", None, 0, text="STUDY ADMINISTRATION")],
            ),
            PageRecord(3, "3", 0, 0, native_text="neighbor"),
        ]
        selected = candidate_evidence_pages(pages)
        self.assertEqual([p.page_number for p in selected], [1, 2, 3])

    def test_llm_evidence_is_validated_against_enriched_text(self):
        page = PageRecord(
            4,
            "4",
            0,
            0,
            elements=[ElementRef("tbl", "table", None, 0, text="Study ID: TOX-42")],
        )
        gateway = FakeGateway(
            {
                "study_id": {
                    "value": "TOX-42",
                    "raw_value": "TOX-42",
                    "unit": None,
                    "evidence_text": "Study ID: TOX-42",
                    "evidence_pages": [4],
                    "confidence": 0.99,
                }
            }
        )
        result = MetadataExtractor(gateway).extract_missing(["study_id"], [page])
        self.assertEqual(result["study_id"].status, "extracted")
        self.assertEqual(result["study_id"].value, "TOX-42")


if __name__ == "__main__":
    unittest.main()
