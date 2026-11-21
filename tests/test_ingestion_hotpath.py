"""Regression tests for extraction hot-path optimizations without cloud calls."""
from __future__ import annotations

import ast
import contextlib
import sys
import types
import unittest
from pathlib import Path
from threading import Lock
from unittest.mock import patch

MODULE=Path(__file__).resolve().parents[1]/"main.py"


class TestTableSummaryOnePass(unittest.TestCase):
    def test_summary_reuses_first_pdf_parse(self):
        # Compile only the target function to avoid starting the production
        # application or importing cloud/DB clients in this unit test.
        tree=ast.parse(MODULE.read_text(encoding="utf-8"))
        fn=next(n for n in tree.body
                if isinstance(n,ast.FunctionDef) and
                n.name=="_extract_tables_for_page_guarded")
        module=ast.Module(body=[fn],type_ignores=[])
        calls={"extract":0,"summarize":0}
        table=types.SimpleNamespace(markdown="| A | B |",summary="")
        def extract(path,page_number,context_text="",summarise=False):
            calls["extract"]+=1
            self.assertFalse(summarise)
            return [table]
        def summarize(markdown,context):
            calls["summarize"]+=1
            return "Approved table summary"
        fake=types.ModuleType("table_extractor")
        fake._summarise_table_with_gpt4o=summarize
        ns={
            "threading":__import__("threading"),
            "_get_pdfplumber_lock":lambda x:Lock(),
            "extract_tables_for_page":extract,
            "_SEM_GPT4O":Lock(),
        }
        exec(compile(module,str(MODULE),"exec"),ns)
        with patch.dict(sys.modules,{"table_extractor":fake}):
            result=ns["_extract_tables_for_page_guarded"](
                "document.pdf",1,"context",summarise=True
            )
        self.assertEqual(calls,{"extract":1,"summarize":1})
        self.assertEqual(result[0].summary,"Approved table summary")

    def test_pdf_table_batch_opens_once_and_releases_page_cache(self):
        from table_extractor import extract_tables_for_pages, TableExtractor
        pages = []
        for n in range(3):
            p=types.SimpleNamespace(
                extract_tables=lambda *args: [[["A", "B"],["X", "Y"]]],
                close=lambda:None,
            )
            p.closed=False
            p.close=lambda p=p: setattr(p,"closed",True)
            pages.append(p)
        opened=[]
        class MockPDF:
            def __enter__(self):
                opened.append(1)
                return self
            def __exit__(self,*args):
                pass
        pdf=MockPDF()
        pdf.pages=pages
        def process(self,cells,page_number,table_index,context):
            return types.SimpleNamespace(page_number=page_number,summary="")
        with patch("table_extractor._pdfplumber",
                   return_value=types.SimpleNamespace(open=lambda path:pdf)):
            with patch.object(TableExtractor,"_process_table",new=process):
                output,errors=extract_tables_for_pages(
                    "sample.pdf",[1,3],{1:"before",3:"after"})
        self.assertEqual(len(opened),1)
        self.assertEqual(errors,{})
        self.assertEqual(sorted(output),[1,3])
        self.assertTrue(pages[0].closed)
        self.assertFalse(pages[1].closed)
        self.assertTrue(pages[2].closed)

    def test_ocr_low_confidence_is_not_accepted_in_strict_mode(self):
        from vision_ocr import ocr_page_elements,OcrResult
        from pdf_processor import ElementType
        element=types.SimpleNamespace(
            element_type=ElementType.IMAGE,image_bytes=b"synthetic",
            text="",confidence=0.0
        )
        result=OcrResult(
            text="",confidence=0.12,word_count=15,language="en",
            success=True,error="low_confidence_0.12",
        )
        with patch("vision_ocr.ocr_image_bytes",return_value=result):
            with self.assertRaisesRegex(RuntimeError,"OCR_UNRESOLVED"):
                ocr_page_elements([element],"sample.pdf",3,raise_on_error=True)

    def test_metadata_required_and_evidence(self):
        from metadata_extractor import attach_metadata
        from pdf_processor import ElementType
        page=types.SimpleNamespace(
            page_number=1,raw_text="Protocol Number: ABC-123",
            elements=[types.SimpleNamespace(
                element_type=ElementType.TEXT,text="Protocol Number: ABC-123"
            )],is_blank=False,error="")
        struct=types.SimpleNamespace(pages=[page])
        chunk=types.SimpleNamespace(
            chunk_text="Study text",section_title="Study",headers=[],
        )
        policy={
            "inherit_document_fields":True,
            "document":{
                "protocol_number":{"method":"label","label":"Protocol Number",
                                   "type":"string","enabled":True,"required":True,
                                   "sample_values":["NOT_THE_RESULT"]}
            },
            "chunk":{},
        }
        result=attach_metadata(struct,[chunk],policy)
        self.assertEqual(result,{"protocol_number":"ABC-123"})
        self.assertEqual(struct.metadata_evidence["protocol_number"]["page"],1)
        self.assertEqual(chunk.document_metadata["protocol_number"],"ABC-123")
        page.raw_text="No matching document metadata"
        page.elements[0].text="No matching document metadata"
        with self.assertRaisesRegex(ValueError,"Required document metadata"):
            attach_metadata(struct,[chunk],policy)


if __name__=="__main__":
    unittest.main()
