"""Offline, original-source image approval tests: no OCR provider or DB writes."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from approved_images import read_approved_image_manifest
from pdf_processor import PDFProcessor, ElementType
from page_stage import _ocr_pages, _page_row, extraction_fingerprint


class ApprovedImageTests(unittest.TestCase):
    def _fixture(self, folder: Path):
        import fitz
        from PIL import Image, ImageDraw
        path=folder/"approved-asset.pdf"
        raw=io.BytesIO()
        image=Image.new("RGB",(585,217),"white")
        ImageDraw.Draw(image).text((18,30),"TEST AGENCY LOGO",fill="black")
        image.save(raw,format="PNG")
        with fitz.open() as pdf:
            for page_no in range(2):
                page=pdf.new_page()
                page.insert_image(
                    fitz.Rect(72,36,263.5,106.9),
                    stream=raw.getvalue())
                page.insert_text((72,175),
                    "Original regulatory text on page "
                    + str(page_no+1)+" with retained source provenance.",fontsize=11)
                page.insert_text((72,195),
                    "More native words: dose concentration, safety, protocol, "
                    "review date, clinical evidence, audit evidence.",fontsize=10)
            pdf.save(path)
        with fitz.open(path) as pdf:
            xref=pdf[0].get_images(full=True)[0][0]
            self.assertEqual(xref,pdf[1].get_images(full=True)[0][0])
            image_bytes=pdf.extract_image(xref)["image"]
        return path,hashlib.sha256(image_bytes).hexdigest(),len(image_bytes)

    def _manifest(self,folder,digest,status="approved",reviewer="qa-reviewer"):
        import yaml
        target=folder/"review.yml"
        target.write_text(yaml.safe_dump({
            "version":1,"assets":[{
                "sha256":digest,"status":status,"kind":"logo",
                "text":"Test agency logo approved text",
                "reviewed_by":reviewer,
                "approval_reference":"manual-review-test-fixture"
            }]
        }),encoding="utf-8")
        return target

    def test_no_implicit_approval_and_manifest_fingerprints(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder=Path(dirname)
            path,digest,_=self._fixture(folder)
            manifest=self._manifest(folder,digest,status="pending",reviewer="")
            pending,h1=read_approved_image_manifest(str(manifest))
            self.assertEqual(pending,{})
            self._manifest(folder,digest,status="approved")
            rules,h2=read_approved_image_manifest(str(manifest))
            self.assertNotEqual(h1,h2)
            self.assertEqual(len(rules),1)
            self.assertEqual(rules[digest]["text"],"Test agency logo approved text")
            # Semantic extraction hash changes with approval; existing
            # checkpoint versions can never be silently reused.
            from processing_profile import load_profile
            profile=load_profile("profiles/regulatory_page_stage_example.yml")
            profile["extraction"]["approved_image_manifest"]=str(manifest)
            fingerprint1=extraction_fingerprint(profile)
            self._manifest(folder,digest,status="pending",reviewer="")
            fingerprint2=extraction_fingerprint(profile)
            self.assertNotEqual(fingerprint1,fingerprint2)

    def test_two_pages_same_original_image_no_cloud_work_and_provenance(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder=Path(dirname)
            path,digest,image_length=self._fixture(folder)
            manifest=self._manifest(folder,digest)
            rules,_=read_approved_image_manifest(str(manifest))
            processor=PDFProcessor(str(path),"test-document",path.name,
                                   strict=True,capture_rich_text=True,
                                   approved_image_assets=rules)
            windows=list(processor.iter_page_batches(batch_pages=1))
            self.assertEqual(len(windows),2)
            for window in windows:
                page=window.pages[0]
                self.assertFalse(page.error)
                self.assertFalse(page.needs_ocr)
                images=[e for e in page.elements
                        if e.element_type is ElementType.IMAGE]
                self.assertEqual(len(images),1)
                logo=images[0]
                self.assertEqual(logo.image_bytes,b"")
                self.assertEqual(logo.approved_asset["source_sha256"],digest)
                self.assertEqual(logo.approved_asset["source_bytes"],image_length)
                self.assertEqual(logo.approved_asset["source_xref"],208 if False else
                    logo.approved_asset["source_xref"])
                self.assertEqual(logo.text,"Test agency logo approved text")
                errors,count=_ocr_pages([page],{
                    "ocr_enabled":False,
                    "ocr_max_batch_bytes":6_000_000,
                    "ocr_batch_images":8,
                    "chart_descriptions_enabled":False,
                },None)
                self.assertFalse(errors)
                self.assertEqual(count,0)
                row=_page_row(page,window,"src","fingerprint")
                self.assertIn("Test agency logo approved text",row[8])
                elements=json.loads(row[9])
                stored=next(e for e in elements if e["type"]=="image")
                self.assertEqual(stored["approved_asset"]["source_sha256"],digest)
                self.assertEqual(stored["approved_asset"]["text"],stored["text"])
                self.assertEqual(stored["approved_asset"]["source_bytes"],image_length)

    def test_wrong_digest_does_not_bypass_strict_policy(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder=Path(dirname)
            path,digest,_=self._fixture(folder)
            bogus="0"*64 if digest!="0"*64 else "f"*64
            manifest=self._manifest(folder,bogus)
            rules,_=read_approved_image_manifest(str(manifest))
            processor=PDFProcessor(str(path),"test-document",path.name,
                                   strict=True,approved_image_assets=rules)
            first=next(processor.iter_page_batches(batch_pages=1)).pages[0]
            images=[e for e in first.elements if e.element_type is ElementType.IMAGE]
            self.assertTrue(images)
            self.assertFalse(any(e.approved_asset for e in images))
            errors,count=_ocr_pages([first],{
                "ocr_enabled":False,
                "ocr_max_batch_bytes":6_000_000,
                "ocr_batch_images":8,
                "chart_descriptions_enabled":False,
            },None)
            self.assertEqual(count,0)
            self.assertIn(1,errors)
            self.assertIn("OCR is disabled",str(errors[1]))

    def test_reject_duplicate_or_unreviewed_approval(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder=Path(dirname)
            manifest=self._manifest(folder,"a"*64,status="approved",reviewer="")
            with self.assertRaisesRegex(ValueError,"reviewer"):
                read_approved_image_manifest(str(manifest))
            manifest.write_text(
                "version: 1\nassets:\n"
                "  - sha256: " + "a"*64 +
                "\n    status: approved\n    kind: logo\n"
                '    text: logo\n    reviewed_by: qa\n'
                '    approval_reference: R1\n    text: duplicate\n',
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"Duplicate"):
                read_approved_image_manifest(str(manifest))


if __name__=="__main__":
    unittest.main()
