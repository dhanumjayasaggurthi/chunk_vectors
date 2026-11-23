"""Quality and performance-shape tests for semantic v2 chunk projections."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pdf_processor import BBox, DocumentStructure, ElementType, PageContent, PageElement, TocEntry


def document_with_continued_table(*, header_change=False, gap=False):
    pages=[]
    for number in range(1,5):
        header=["Subject ID","Adverse Events"] if not (header_change and number==3) else ["Patient","Events"]
        rows=[header]+[[f"ROW-{number}-{i}", f"Headache {number}-{i}"] for i in range(7)]
        table=PageElement(
            element_type=ElementType.TABLE,
            bbox=BBox(40,70 if number>1 else 130,550,745),
            text="| Subject ID | Adverse Events |\n|------------|----------------|\n"+
                 "\n".join(f"| {r[0]} | {r[1]} |" for r in rows[1:]),
            reading_order=1,
            source_table={"max_columns":2,"source_cells":rows},
        )
        text=PageElement(
            element_type=ElementType.TEXT,bbox=BBox(50,10,550,55),
            text="Safety details continue across the following pages.",
            reading_order=0,
        )
        pages.append(PageContent(
            page_number=number+(1 if gap and number>=3 else 0),
            width=612,height=792,
            elements=[text,table],
            raw_text=text.text,has_tables=True,
        ))
    return DocumentStructure(
        file_path="test.pdf",file_name="test.pdf",doc_id="a"*64,
        page_count=5 if gap else 4,
        pages=pages,toc=[TocEntry(1,"3. Safety",1)],has_toc=True,
    )


def config(**overrides):
    result={
        "strategy":"toc_then_semantic","boundary_mode":"semantic_v2",
        "min_tokens":25,"max_tokens":100,"overlap_tokens":20,
        "preserve_headers":True,"small_section_policy":"keep",
        "tokenizer":"cl100k_base","table_row_mode":"rows",
        "table_continuation":"conservative","max_table_pages":8,
        "table_edge_fraction":0.25,"table_x_tolerance":0.05,
    }
    result.update(overrides)
    return result


class SemanticChunkV2Tests(unittest.TestCase):
    def test_four_page_table_is_one_logical_table_and_row_chunks(self):
        from configurable_chunking import chunk_with_profile
        import tiktoken
        document=document_with_continued_table()
        chunks=chunk_with_profile(document,config(max_tokens=94))
        table_chunks=[c for c in chunks if "table_id" in c.chunk_metadata]
        self.assertGreaterEqual(len(table_chunks),4)
        self.assertEqual(len({c.chunk_metadata["table_id"] for c in table_chunks}),1)
        encoder=tiktoken.get_encoding("cl100k_base")
        for chunk in chunks:
            self.assertLessEqual(len(encoder.encode(chunk.chunk_text)),94)
        ids="\n".join(chunk.chunk_text for chunk in table_chunks)
        for pg in range(1,5):
            for n in range(7):
                self.assertIn(f"ROW-{pg}-{n}",ids)
        self.assertTrue(all(
            c.chunk_metadata["table_continuation_verified"] for c in table_chunks
        ))
        self.assertTrue(all(
            c.chunk_metadata["table_fragment_count"]==4 for c in table_chunks
        ))
        self.assertTrue(all(
            c.chunk_metadata["table_source_pages"]==[1,2,3,4] for c in table_chunks
        ))
        self.assertTrue(all(
            c.chunk_metadata["table_summary_status"]=="STRUCTURAL_ONLY"
            for c in table_chunks
        ))
        self.assertTrue(all("Subject ID" in c.chunk_text for c in table_chunks))
        # No source page table is mutated by reconstruction.
        for page in document.pages:
            source=page.elements[1].source_table["source_cells"]
            self.assertEqual(source[0],["Subject ID","Adverse Events"])
            self.assertEqual(len(source),8)

    def test_mismatch_or_gap_never_merges_unrelated_tables(self):
        from configurable_chunking import chunk_with_profile
        for doc in [
            document_with_continued_table(header_change=True),
            document_with_continued_table(gap=True),
        ]:
            chunks=chunk_with_profile(doc,config(max_tokens=94))
            ids={c.chunk_metadata["table_id"] for c in chunks
                 if "table_id" in c.chunk_metadata}
            self.assertGreater(len(ids),1)

    def test_sentence_boundaries_and_neighbor_links(self):
        from configurable_chunking import chunk_with_profile
        long_text=" ".join(
            f"Sentence {i:03d} describes the original clinical assessment clearly."
            for i in range(45)
        )
        document=DocumentStructure(
            file_path="notes.pdf",file_name="notes.pdf",doc_id="b"*64,
            page_count=2,
            toc=[TocEntry(1,"2. Clinical Assessment",1)],
            pages=[
                PageContent(page_number=1,width=612,height=792,
                            elements=[PageElement(ElementType.TEXT,BBox(1,1,500,400),
                                                  text=long_text)]),
                PageContent(page_number=2,width=612,height=792,
                            elements=[PageElement(ElementType.TEXT,BBox(1,1,500,400),
                                                  text="Final patient assessment follows on this page.")]),
            ],
        )
        chunks=chunk_with_profile(
            document,config(max_tokens=65,table_row_mode="legacy",table_continuation="none")
        )
        self.assertGreater(len(chunks),3)
        self.assertEqual(len({c.chunk_metadata["section_id"] for c in chunks}),1)
        for idx,c in enumerate(chunks):
            self.assertEqual(
                c.chunk_metadata["previous_chunk_id"],
                chunks[idx-1].chunk_id if idx else None,
            )
            self.assertEqual(
                c.chunk_metadata["next_chunk_id"],
                chunks[idx+1].chunk_id if idx+1<len(chunks) else None,
            )
            self.assertLessEqual(len(__import__("tiktoken").get_encoding(
                "cl100k_base").encode(c.chunk_text)),65)
        complete="\n".join(c.chunk_text for c in chunks)
        for i in range(45):
            self.assertIn(f"Sentence {i:03d} describes",complete)

    def test_long_indivisible_unicode_uses_lossless_character_fallback(self):
        from configurable_chunking import chunk_with_profile
        original="語"*800
        doc=DocumentStructure(
            file_path="x",file_name="x",doc_id="c"*64,page_count=1,
            pages=[PageContent(page_number=1,width=100,height=100,
                elements=[PageElement(ElementType.TEXT,BBox(0,0,90,90),text=original)])],
        )
        chunks=chunk_with_profile(doc,config(
            max_tokens=45,overlap_tokens=0,preserve_headers=False,
            table_row_mode="legacy",table_continuation="none",
        ))
        self.assertEqual("".join(c.chunk_text for c in chunks),original)
        self.assertTrue(any(
            "oversized_paragraph_word_fallback" in
            c.chunk_metadata.get("exceptional_splits",[]) for c in chunks
        ))

    def test_profile_validation_and_existing_metadata_survive(self):
        from processing_profile import load_profile, validate_profile
        from configurable_chunking import chunk_with_profile
        from metadata_extractor import attach_metadata
        profile=load_profile("profiles/regulatory_page_stage_example.yml")
        profile["chunking"].update(config())
        validate_profile(profile)
        document=document_with_continued_table()
        chunks=chunk_with_profile(document,profile["chunking"])
        attach_metadata(document,chunks,{"document":{},"chunk":{},
                                         "inherit_document_fields":True})
        self.assertTrue(any("table_id" in c.chunk_metadata for c in chunks))
        self.assertTrue(all("section_id" in c.chunk_metadata for c in chunks))
        profile["chunking"]["boundary_mode"]="v1"
        with self.assertRaisesRegex(ValueError,"semantic-v2"):
            validate_profile(profile)


if __name__=="__main__":
    unittest.main()
