import unittest

from mir_ai.models import ElementRef, PageRecord
from mir_ai.pipeline import MIRPipeline


class TestPageValidation(unittest.TestCase):
    def test_page_level_parse_error_blocks_activation(self):
        page = PageRecord(9, "9", 0, 0, error="broken xref")
        with self.assertRaisesRegex(RuntimeError, "Page 9 parsing failed"):
            MIRPipeline._validate_page(page)

    def test_required_element_error_blocks_activation(self):
        page = PageRecord(
            2,
            "2",
            0,
            0,
            elements=[
                ElementRef(
                    "img",
                    "image",
                    None,
                    0,
                    extraction_status="error",
                    raw={"error": "vision unavailable", "stage": "image"},
                )
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "required extraction failed"):
            MIRPipeline._validate_page(page)

    def test_scanned_page_requires_actual_ocr_text(self):
        page = PageRecord(
            3,
            "3",
            0,
            0,
            elements=[
                ElementRef(
                    "scan",
                    "image",
                    None,
                    0,
                    text="",
                    extraction_status="low_confidence",
                    raw={"full_page_ocr": True},
                )
            ],
            needs_full_ocr=True,
        )
        with self.assertRaisesRegex(RuntimeError, "no OCR text was produced"):
            MIRPipeline._validate_page(page)

    def test_low_confidence_ocr_with_text_is_explicit_but_accepted(self):
        page = PageRecord(
            4,
            "4",
            0,
            0,
            elements=[
                ElementRef(
                    "scan",
                    "image",
                    None,
                    0,
                    text="Readable evidence",
                    extraction_status="low_confidence",
                    raw={"full_page_ocr": True},
                )
            ],
            needs_full_ocr=True,
        )
        MIRPipeline._validate_page(page)


if __name__ == "__main__":
    unittest.main()
