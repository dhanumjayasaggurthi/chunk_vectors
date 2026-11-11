"""Deterministic ingestion-v2 policy, chunk lineage and metadata tests."""
import unittest

from ingestion_v2.plan import ExtractionPolicy, page_ranges
from ingestion_v2.chunking import ChunkPolicy, LayoutElement, chunk_elements
from ingestion_v2.metadata import extract_metadata, validate_metadata_policy


class TestPlanner(unittest.TestCase):
    def test_page_ranges_of_very_large_document(self):
        spans=page_ranges(25000,64)
        self.assertEqual(len(spans),391)
        self.assertEqual(spans[0],(0,64))
        self.assertEqual(spans[-1],(24960,25000))
        self.assertEqual(sum(b-a for a,b in spans),25000)
        self.assertEqual(page_ranges(0,64),[])

    def test_rejects_bad_concurrency(self):
        with self.assertRaises(ValueError):
            ExtractionPolicy(cpu_processes=0)
        with self.assertRaises(ValueError):
            page_ranges(500,0)


class TestChunking(unittest.TestCase):
    def setUp(self):
        self.policy=ChunkPolicy(target_tokens=100,max_tokens=128,
                                min_tokens=30,overlap_tokens=12)

    def test_ten_page_section_never_exceeds_max(self):
        items=[LayoutElement(f"p{p}",p,"paragraph",
                            ("This is a regulatory section sentence. "*70),
                            (0,20,150,400),("3 Clinical Overview",))
               for p in range(1,11)]
        result=chunk_elements("doc",items,self.policy)
        self.assertGreater(len(result),10)
        self.assertTrue(all(0<c.token_count<=128 for c in result))
        self.assertEqual(result[0].pages[0],1)
        self.assertTrue(any(c.boxes for c in result))
        self.assertEqual(len({c.chunk_id for c in result}),len(result))

    def test_explicit_cross_page_continuation_stays_together(self):
        first=LayoutElement("first",1,"paragraph","The study was conducted ",
                            (0,0,100,100))
        second=LayoutElement("second",2,"paragraph","across multiple regions.",
                             (0,0,100,80),continuation_of="first")
        result=chunk_elements("doc",[first,second],self.policy)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].pages,(1,2))
        self.assertEqual(result[0].element_ids,("first","second"))
        self.assertIn("conducted across",result[0].text)

    def test_unlinked_fragments_are_not_guessed_as_one_paragraph(self):
        first=LayoutElement("a",1,"paragraph","Last line of page.")
        second=LayoutElement("b",2,"paragraph","New page first block.")
        result=chunk_elements("doc",[first,second],self.policy)
        self.assertEqual(result[0].element_ids,("a","b"))
        self.assertIn("page.\nNew page",result[0].text)

    def test_table_does_not_split_if_it_fits(self):
        body="| Header A | Header B |\n|---|---|\n| a | b |\n| c | d |"
        table=LayoutElement("t",1,"table",body,(0,0,300,200))
        result=chunk_elements("doc",[table],self.policy)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].text,body)
        self.assertEqual(result[0].content_types,("table",))

    def test_oversized_table_split_is_explicit(self):
        text="| H1 | H2 |\n|---|---|\n"+("| long cell value | X |\n"*120)
        table=LayoutElement("t",1,"table",text)
        result=chunk_elements("doc",[table],self.policy)
        self.assertGreater(len(result),1)
        self.assertTrue(all(c.token_count<=128 for c in result))
        self.assertTrue(any(c.split_element for c in result))

    def test_disable_table_splits_fails_instead_of_truncating(self):
        policy=ChunkPolicy(target_tokens=40,max_tokens=64,min_tokens=10,
                            overlap_tokens=0,split_oversized_tables=False)
        with self.assertRaisesRegex(ValueError,"Table exceeds"):
            chunk_elements("doc",[LayoutElement("t",1,"table","z "*500)],policy)

    def test_overlap_never_exceeds_hard_limit(self):
        items=[LayoutElement(str(i),i,"paragraph",("alpha beta gamma "*30))
               for i in range(1,5)]
        result=chunk_elements("doc",items,self.policy)
        self.assertTrue(all(c.token_count<=self.policy.max_tokens for c in result))
        self.assertTrue(any(c.overlap_from_previous for c in result))


class TestDocumentMetadata(unittest.TestCase):
    def test_field_examples_never_populate_missing_values(self):
        config={"enabled":True,"fields":{
            "protocol":{"enabled":True,"method":"label","type":"string",
                        "label":"Protocol","first_pages":3,
                        "sample_values":["ABC-123"]},
        }}
        data=extract_metadata([LayoutElement("p1",1,"paragraph","No protocol here.")],config)
        self.assertEqual(data["protocol"]["status"],"NOT_FOUND")
        self.assertIsNone(data["protocol"]["value"])

    def test_field_evidence_and_disable(self):
        config={"enabled":True,"fields":{
            "protocol":{"method":"label","type":"string","label":"Protocol"},
            "phase":{"enabled":False,"method":"regex","type":"string",
                     "pattern":"Phase ([IVX]+)"},
        }}
        data=extract_metadata([LayoutElement("p1",1,"paragraph",
                         "Protocol: ABC-123",bbox=(1,2,3,4))],config)
        self.assertEqual(data["protocol"]["value"],"ABC-123")
        self.assertEqual(data["protocol"]["evidence"]["page"],1)
        self.assertEqual(data["protocol"]["evidence"]["bbox"],(1,2,3,4))
        self.assertEqual(data["phase"]["status"],"DISABLED")

    def test_invalid_regex_rejected(self):
        with self.assertRaises(Exception):
            validate_metadata_policy({"enabled":True,"fields":{
                "bad":{"method":"regex","type":"string","pattern":"[unclosed"}
            }})


if __name__=="__main__":
    unittest.main()
