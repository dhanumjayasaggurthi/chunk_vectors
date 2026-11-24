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
    def test_pack_small_tables_preserves_every_source_table_identity(self):
        from configurable_chunking import chunk_with_profile
        import tiktoken
        tables=[]
        for i in range(8):
            rows=[["Endpoint","Result"],[f"Result-{i}","Observed"]]
            tables.append(PageElement(
                ElementType.TABLE,BBox(60,80+i*70,520,120+i*70),
                text=f"| Endpoint | Result |\n| --- | --- |\n| Result-{i} | Observed |",
                source_table={"max_columns":2,"source_cells":rows},
                reading_order=i,
            ))
        page=PageContent(page_number=1,width=612,height=792,
                         raw_text="Reported safety outcomes.",
                         elements=tables,has_tables=True)
        doc=DocumentStructure(
            file_path="safety.pdf",file_name="safety.pdf",doc_id="f"*64,
            page_count=1,pages=[page],
            toc=[TocEntry(1,"4. Safety results",1)],has_toc=True,
        )
        base=config(max_tokens=180,min_tokens=100,overlap_tokens=0,
                    small_section_policy="keep")
        baseline=chunk_with_profile(doc,base)
        packed=chunk_with_profile(doc,{
            **base,"small_section_policy":"merge_forward",
            "small_chunk_max_pages":1,"small_chunk_max_tables":3,
        })
        self.assertLess(len(packed),len(baseline))
        tokenizer=tiktoken.get_encoding("cl100k_base")
        for c in packed:
            self.assertLessEqual(len(tokenizer.encode(c.chunk_text)),180)
        all_text="\n".join(c.chunk_text for c in packed)
        for i in range(8):
            self.assertEqual(all_text.count(f"Result-{i}"),1)
        baseline_ids={
            c.chunk_metadata["table_id"] for c in baseline
            if "table_id" in c.chunk_metadata
        }
        packed_ids=set()
        for chunk in packed:
            metadata=chunk.chunk_metadata
            packed_ids.update(metadata.get("table_ids",[
                metadata["table_id"]] if metadata.get("table_id") else []))
            if len(metadata.get("table_ids",[]))>1:
                self.assertNotIn("table_id",metadata)
                self.assertGreaterEqual(len(metadata["source_components"]),2)
            self.assertEqual(chunk.page_start,1)
            self.assertEqual(chunk.page_end,1)
        self.assertEqual(packed_ids,baseline_ids)
        self.assertEqual(len(baseline_ids),8)
        self.assertTrue(all(
            tab.source_table["source_cells"][1][0]==f"Result-{index}"
            for index,tab in enumerate(tables)
        ))

    def test_packing_never_crosses_different_section_or_unknown_page(self):
        from configurable_chunking import chunk_with_profile
        pages=[]
        for pg,heading in ((1,"Safety"),(2,"Efficacy")):
            pages.append(PageContent(
                page_number=pg,width=612,height=792,
                raw_text=f"Data_{pg} for section {heading}",
                elements=[PageElement(ElementType.TEXT,BBox(50,100,550,160),
                                      text=f"Data_{pg} for section {heading}")],
            ))
            pages[-1].source_section_title=heading
        doc=DocumentStructure(
            file_path="s.pdf",file_name="s.pdf",doc_id="9"*64,
            page_count=2,pages=pages,
        )
        packed=chunk_with_profile(doc,config(
            max_tokens=180,min_tokens=120,overlap_tokens=0,
            small_section_policy="merge_forward",
            small_chunk_max_pages=2,table_row_mode="legacy",
            table_continuation="none",
        ))
        self.assertEqual(len(packed),2)
        self.assertEqual({c.page_start for c in packed},{1,2})
        self.assertEqual(len({c.chunk_metadata["section_id"] for c in packed}),2)
        self.assertFalse(any("Data_1" in c.chunk_text and "Data_2" in c.chunk_text
                             for c in packed))

    def test_packed_materialized_neighbors_and_source_lineage(self):
        from processing_profile import load_profile
        from reuse_materialize import prepare_document, quality_summary
        document=document_with_continued_table()
        p=load_profile("profiles/regulatory_page_stage_example.yml")
        p["chunking"].update(config(max_tokens=180,min_tokens=120,
                                    small_section_policy="merge_forward",
                                    small_chunk_max_pages=2,
                                    small_chunk_max_tables=3))
        rows=prepare_document(document,p)
        summary=quality_summary(rows,p["chunking"])
        self.assertEqual(summary["structural_errors"],0)
        self.assertEqual(summary["chunks_above_budget"],0)
        self.assertGreaterEqual(summary["logical_tables"],1)
        for i,row in enumerate(rows):
            meta=json.loads(row[13])
            self.assertEqual(meta["previous_chunk_id"],
                             rows[i-1][0] if i else None)
            self.assertEqual(meta["next_chunk_id"],
                             rows[i+1][0] if i+1<len(rows) else None)

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

    def test_streamed_four_page_table_projection_has_source_row_citations(self):
        from table_continuity import link_tables, iter_logical_table_rows
        document=document_with_continued_table()
        groups=link_tables(document,config())
        self.assertEqual(len(groups),1)
        source=list(groups.values())[0]
        original=[list(el.source_table["source_cells"]) for el in source]
        rows=list(iter_logical_table_rows(source))
        self.assertEqual(len(rows),29)  # Four source grids x 8 rows, 3 repeated headers.
        self.assertEqual([page for page,idx,row in rows if idx==0],[1])
        self.assertEqual(rows[0][2],["Subject ID","Adverse Events"])
        self.assertEqual(rows[-1][0],4)
        self.assertEqual(rows[-1][2][0],"ROW-4-6")
        for el,original_rows in zip(source,original):
            self.assertEqual(el.source_table["source_cells"],original_rows)
        with self.assertRaisesRegex(ValueError,"fragment count"):
            list(iter_logical_table_rows(source[:2]))

    def test_semantic_implementation_version_rotates_profile_identity(self):
        import hashlib
        import json
        from processing_profile import load_profile
        v2=load_profile("profiles/regulatory_semantic_v2_example.yml")
        same=load_profile("profiles/regulatory_semantic_v2_example.yml")
        self.assertEqual(v2["_fingerprint"],same["_fingerprint"])
        legacy_source=dict(v2)
        legacy_source.pop("_fingerprint")
        old_hash=hashlib.sha256(json.dumps(
            legacy_source,sort_keys=True,separators=(",",":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        self.assertNotEqual(v2["_fingerprint"],old_hash)
        old=load_profile("profiles/regulatory_page_stage_example.yml")
        old_source=dict(old)
        old_source.pop("_fingerprint")
        self.assertEqual(old["_fingerprint"],hashlib.sha256(json.dumps(
            old_source,sort_keys=True,separators=(",",":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest())

    def test_persisted_profile_versioned_chunk_ids_resolve_neighbors(self):
        from processing_profile import load_profile
        from reuse_materialize import prepare_document,quality_summary
        p=load_profile("profiles/regulatory_page_stage_example.yml")
        p["chunking"].update(config(max_tokens=94))
        rows=prepare_document(document_with_continued_table(),p)
        self.assertGreater(len(rows),4)
        for i,row in enumerate(rows):
            metadata=json.loads(row[13])
            self.assertEqual(
                metadata["previous_chunk_id"],
                rows[i-1][0] if i else None,
            )
            self.assertEqual(
                metadata["next_chunk_id"],
                rows[i+1][0] if i+1<len(rows) else None,
            )
        report=quality_summary(rows,p["chunking"])
        self.assertEqual(report["structural_errors"],0)

    def test_no_header_separator_prevents_cross_page_false_positive(self):
        from configurable_chunking import chunk_with_profile
        doc=document_with_continued_table()
        for page in doc.pages:
            table=page.elements[1]
            table.text=table.text.replace("|------------|----------------|", "")
        chunks=chunk_with_profile(doc,config(max_tokens=94))
        tables={
            c.chunk_metadata["table_id"]
            for c in chunks if c.chunk_metadata.get("table_id")
        }
        self.assertEqual(len(tables),4)

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

    def test_adjacent_page_paragraph_join_is_opt_in_and_provenanced(self):
        from configurable_chunking import chunk_with_profile
        first="This report described a clinically meaningful"
        second="outcome after the follow-up period."
        doc=DocumentStructure(
            file_path="x",file_name="x",doc_id="d"*64,page_count=2,
            toc=[TocEntry(1,"Study Results",1)],
            pages=[
                PageContent(page_number=1,width=612,height=792,
                    elements=[PageElement(ElementType.TEXT,BBox(10,10,400,750),
                                          text=first)]),
                PageContent(page_number=2,width=612,height=792,
                    elements=[PageElement(ElementType.TEXT,BBox(10,50,400,700),
                                          text=second)]),
            ],
        )
        joined=chunk_with_profile(doc,config(
            max_tokens=120,merge_cross_page_paragraphs=True,
            table_row_mode="legacy",table_continuation="none",
        ))
        self.assertTrue(any(
            "meaningful outcome" in c.chunk_text and
            c.chunk_metadata["cross_page_paragraph_joined"]
            for c in joined
        ))
        self.assertEqual(joined[0].page_start,1)
        self.assertEqual(joined[0].page_end,2)
        separate=chunk_with_profile(doc,config(
            max_tokens=120,merge_cross_page_paragraphs=False,
            table_row_mode="legacy",table_continuation="none",
        ))
        self.assertFalse(any(c.chunk_metadata["cross_page_paragraph_joined"]
                             for c in separate))
        self.assertEqual(doc.pages[0].elements[0].text,first)
        self.assertEqual(doc.pages[1].elements[0].text,second)

    def test_cross_page_paragraph_not_joined_without_page_edge_evidence(self):
        from configurable_chunking import chunk_with_profile
        doc=DocumentStructure(
            file_path="x",file_name="x",doc_id="e"*64,page_count=2,
            pages=[
                PageContent(page_number=1,width=612,height=792,
                    elements=[PageElement(ElementType.TEXT,BBox(50,70,500,250),
                                          text="A study described a statistically meaningful")]),
                PageContent(page_number=2,width=612,height=792,
                    elements=[PageElement(ElementType.TEXT,BBox(50,200,500,350),
                                          text="outcome after further observation.")]),
            ],
        )
        chunks=chunk_with_profile(doc,config(
            max_tokens=120,merge_cross_page_paragraphs=True,
            page_join_edge_fraction=0.20,
            table_row_mode="legacy",table_continuation="none",
        ))
        self.assertFalse(any(
            c.chunk_metadata["cross_page_paragraph_joined"] for c in chunks
        ))
        self.assertFalse(any(
            "meaningful outcome" in c.chunk_text for c in chunks
        ))

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
