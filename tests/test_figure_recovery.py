"""Offline privacy-safe synthetic tests for fragmented-figure reconstruction."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path


class FigureRecoveryTests(unittest.TestCase):
    def _fixture(self, folder: Path, *, gap: float = 0):
        import fitz
        from PIL import Image, ImageDraw
        path = folder / "seven-chart-strips.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=612, height=792)
            page.insert_text((60, 85), "Figure 14: Synthetic Kaplan-Meier chart")
            for index in range(7):
                canvas = Image.new("RGB", (480, 36), "white")
                draw = ImageDraw.Draw(canvas)
                draw.line((0, 18, 479, 16 + index), fill="black", width=2)
                if index == 0:
                    draw.text((14, 2), "ORIGINAL SYNTHETIC FIGURE", fill="black")
                source = io.BytesIO()
                canvas.save(source, format="PNG")
                top = 100.0 + index * (32.0 + gap)
                page.insert_image(
                    fitz.Rect(93.5, top, 539.9, top + 32.0),
                    stream=source.getvalue(),
                )
            page.insert_text((70, 430), "Source narrative remains independently native text.")
            pdf.save(path)
        return path

    def test_adjacent_strips_one_candidate_not_seven(self):
        import fitz
        from figure_recovery import find_figure_groups
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            with fitz.open(path) as pdf:
                groups = find_figure_groups(pdf[0])
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]), 7)
            self.assertEqual(len(set(f["xref"] for f in groups[0])), 7)
            self.assertEqual(
                [f["bbox"][1] for f in groups[0]], sorted(f["bbox"][1] for f in groups[0])
            )

    def test_nonadjacent_strips_not_merged(self):
        import fitz
        from figure_recovery import find_figure_groups
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp), gap=8.0)
            with fitz.open(path) as pdf:
                groups = find_figure_groups(pdf[0])
            self.assertEqual(groups, [])

    def test_evidence_immutable_repeatable_and_never_approved(self):
        from figure_recovery import prepare_page
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = self._fixture(folder)
            out = folder / "evidence"
            result = prepare_page(
                path, 1, out, native_extraction_hash="native-test-version"
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["fragments"], 7)
            self.assertEqual(result[0]["review_status"], "VISUAL_REVIEW_REQUIRED")
            manifest_path = Path(result[0]["manifest"])
            evidence = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["interpretation_status"], "NOT_PERFORMED")
            self.assertEqual(evidence["fragment_count"], 7)
            self.assertEqual(evidence["native_extraction_hash"], "native-test-version")
            self.assertEqual(
                evidence["source_pdf_sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                hashlib.sha256(Path(result[0]["png"]).read_bytes()).hexdigest(),
                evidence["composite_sha256"],
            )
            self.assertEqual(
                len({f["original_sha256"] for f in evidence["source_fragments"]}), 7,
            )
            # Native input and independently versioned visual result remain stable.
            self.assertEqual(result, prepare_page(path, 1, out, native_extraction_hash="native-test-version"))
            with manifest_path.open("ab") as f:
                f.write(b"tamper")
            with self.assertRaisesRegex(RuntimeError, "Existing evidence differs"):
                prepare_page(path, 1, out, native_extraction_hash="native-test-version")

    def test_render_and_group_limits_fail_closed(self):
        import fitz
        from figure_recovery import FigureSettings, find_figure_groups, prepare_page
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = self._fixture(folder)
            with fitz.open(path) as pdf:
                with self.assertRaisesRegex(ValueError, "PAGE_IMAGE_COUNT_LIMIT"):
                    find_figure_groups(pdf[0], FigureSettings(max_page_images=6))
            with self.assertRaisesRegex(ValueError, "FIGURE_RENDER_PIXEL_BUDGET_EXCEEDED"):
                prepare_page(
                    path, 1, folder / "blocked",
                    settings=FigureSettings(max_render_pixels=100000),
                )
            with self.assertRaisesRegex(ValueError, "Invalid figure setting"):
                FigureSettings(dpi=1000).validate()

    def test_no_modification_to_native_extraction_profile(self):
        from processing_profile import load_profile
        from page_stage import extraction_fingerprint
        profile = load_profile("profiles/regulatory_page_stage_example.yml")
        before = extraction_fingerprint(profile)
        import fitz
        from figure_recovery import find_figure_groups
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            with fitz.open(path) as pdf:
                self.assertEqual(len(find_figure_groups(pdf[0])), 1)
        self.assertEqual(before, extraction_fingerprint(profile))


if __name__ == "__main__":
    unittest.main()
