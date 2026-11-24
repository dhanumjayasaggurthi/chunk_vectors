"""No-cloud deferred OCR source, rendering, lease, and retry tests."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import deferred_ocr as ocr


def profile():
    return {"extraction":{
        "enabled":True,"schema":"intake_ci","table":"ci_deferred_source",
        "visual_processing":"preserve_defer","ocr_enabled":False,
    }}


def image_ref(page=1):
    return {
        "version":1,"source":"original_pdf","source_page":page,
        "status":"PENDING_OCR_OR_VISUAL_ENRICHMENT",
        "numeric_interpretation":"NOT_PERFORMED",
        "group_type":"single_image","fragment_count":1,
        "fragments":[{
            "xref":0,"bbox":[45,50,200,190],
            "pixel_width":200,"pixel_height":180,
        }],
    }


def vector_ref(page=1):
    return {
        "version":1,"source":"original_pdf","source_page":page,
        "detector":"pdfplumber_table_candidate","candidate_index":0,
        "status":"PENDING_OCR_OR_VISUAL_ENRICHMENT",
        "structured_table_recovered":False,
    }


def make_pdf(where):
    import fitz
    path=Path(where)/"source.pdf"
    with fitz.open() as doc:
        page=doc.new_page(width=612,height=792)
        page.insert_text((50,80),"Source reference only")
        page.draw_rect((45,50,200,190))
        doc.save(path)
    return path.resolve(strict=True)


class DeferredOcrUnitTests(unittest.TestCase):
    def test_versioned_discovery_and_source_validation(self):
        c=ocr.settings(profile())
        version=ocr.ocr_fingerprint(c)
        doc="d"*64
        src="a"*64
        extracted="b"*64
        first=ocr.discover_element(doc,src,extracted,1,2,{
            "type":"image","bbox":[45,50,200,190],
            "visual_asset":image_ref(),
        },version)
        other=ocr.discover_element(doc,src,extracted,1,3,{
            "type":"vector_region","bbox":[45,200,200,290],
            "unresolved_region":vector_ref(),
        },version)
        self.assertEqual(len(first),9)
        self.assertNotEqual(first[0],other[0])
        self.assertEqual(first[4],version)
        self.assertEqual(first[5],1)
        self.assertEqual(first[6],"image")
        self.assertEqual(json.loads(first[7]),[45,50,200,190])
        higher=ocr.settings({
            **profile(),"deferred_ocr":{"render_dpi":300},
        })
        self.assertNotEqual(version,ocr.ocr_fingerprint(higher))
        changed=ocr.discover_element(doc,src,extracted,1,2,{
            "type":"image","bbox":[45,50,200,190],
            "visual_asset":image_ref(),
        },ocr.ocr_fingerprint(higher))
        self.assertNotEqual(first[0],changed[0])
        with self.assertRaisesRegex(ValueError,"provenance"):
            ocr.discover_element(doc,src,extracted,2,2,{
                "type":"image","bbox":[45,50,200,190],
                "visual_asset":image_ref(),
            },version)

    def test_no_permissive_config_or_broken_lease_budget(self):
        p=profile()
        self.assertEqual(ocr.settings(p)["batch_images"],8)
        with self.assertRaisesRegex(ValueError,"Unknown deferred_ocr"):
            ocr.settings({**p,"deferred_ocr":{"unknown":1}})
        with self.assertRaisesRegex(ValueError,"lease"):
            ocr.settings({**p,"deferred_ocr":{"lease_seconds":300}})
        with self.assertRaisesRegex(ValueError,"workers"):
            ocr.settings({**p,"deferred_ocr":{"workers":0}})
        from processing_profile import validate_profile
        full={
            "version":1,
            "extraction":p["extraction"],
            "chunking":{"strategy":"legacy","min_tokens":200,
                        "max_tokens":800,"overlap_tokens":100,
                        "preserve_headers":True,"small_section_policy":"keep"},
            "metadata":{"document":{},"chunk":{},
                        "inherit_document_fields":True},
            "embedding":{"enabled":False,"reuse_existing":True,
                         "batch_size":16},
            "output":{},"existing_chunks":{},"extracted_content":{},
            "deferred_ocr":{"workers":2,"batch_images":4},
        }
        validate_profile(full)
        full["deferred_ocr"]["workers"]=500
        with self.assertRaisesRegex(ValueError,"workers"):
            validate_profile(full)

    def test_source_clip_is_bounded_and_does_not_mutate_pdf(self):
        import fitz
        cfg=ocr.settings(profile())
        with tempfile.TemporaryDirectory() as folder:
            path=make_pdf(folder)
            before=path.read_bytes()
            with fitz.open(path) as pdf:
                rendered=ocr.render_clipped(pdf,1,[45,50,200,190],cfg)
                self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
                with self.assertRaisesRegex(ValueError,"outside page"):
                    ocr.render_clipped(pdf,1,[45,50,999,190],cfg)
                with self.assertRaisesRegex(ValueError,"page index"):
                    ocr.render_clipped(pdf,2,[45,50,200,190],cfg)
                with self.assertRaisesRegex(ValueError,"pixel budget"):
                    ocr.render_clipped(pdf,1,[45,50,200,190],{
                        **cfg,"max_render_pixels":1,
                    })
            self.assertEqual(path.read_bytes(),before)

    def test_fake_ocr_batch_persists_text_and_unresolved_visual_state(self):
        from vision_ocr import OcrResult
        with tempfile.TemporaryDirectory() as folder:
            path=make_pdf(folder)
            doc_id=hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            source=hashlib.sha256(path.read_bytes()).hexdigest()
            version="e"*64
            jobs=[
                (f"job-{i}",doc_id,source,version,1,
                 "image" if i==0 else "vector_region",
                 [45,50,200,190],1,f"lease-{i}")
                for i in range(2)
            ]
            results=[
                OcrResult("Dose 20 mg",0.98,3,"en",True),
                OcrResult("",0,0,"",True),
            ]
            completed=[]
            with (patch("deferred_ocr._file_path",return_value=path),
                  patch("deferred_ocr.renew_leases"),
                  patch("deferred_ocr.complete",
                        side_effect=lambda p,j,**kw:completed.append((j[0],kw))),
                  patch("vision_ocr.batch_document_text_detection",
                        return_value=results) as api):
                summary=ocr.run_group(
                    profile(),jobs,ocr.settings(profile()),None,
                    ocr._SourceVerifier())
            self.assertEqual(summary["TEXT_READY"],1)
            self.assertEqual(summary["VISUAL_PENDING"],1)
            self.assertEqual(api.call_count,1)
            self.assertEqual(completed[0][1]["text"],"Dose 20 mg")
            self.assertEqual(completed[1][1]["error_code"],"NO_OCR_TEXT")

    def test_retryable_cloud_failure_never_looks_like_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path=make_pdf(folder)
            doc_id=hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            source=hashlib.sha256(path.read_bytes()).hexdigest()
            job=("j",doc_id,source,"e"*64,1,"image",
                 [45,50,200,190],1,"lease")
            records=[]
            with (patch("deferred_ocr._file_path",return_value=path),
                  patch("deferred_ocr.renew_leases"),
                  patch("deferred_ocr.complete",
                        side_effect=lambda p,j,**kw:records.append(kw)),
                  patch("vision_ocr.batch_document_text_detection",
                        side_effect=TimeoutError("Transient Vision failure"))):
                summary=ocr.run_group(
                    profile(),[job],ocr.settings(profile()),None,
                    ocr._SourceVerifier())
            self.assertEqual(summary["RETRY_WAIT"],1)
            self.assertEqual(records[0]["status"],"RETRY_WAIT")
            self.assertFalse(records[0].get("text"))

    def test_changed_original_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=make_pdf(folder)
            doc_id=hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            job=("j",doc_id,"0"*64,"e"*64,1,"image",
                 [45,50,200,190],1,"lease")
            statuses=[]
            with (patch("deferred_ocr._file_path",return_value=path),
                  patch("deferred_ocr.renew_leases"),
                  patch("deferred_ocr.complete",
                        side_effect=lambda p,j,**kw:statuses.append(kw["status"])),
                  patch("vision_ocr.batch_document_text_detection",
                        side_effect=AssertionError("Cloud call forbidden"))):
                counts=ocr.run_group(profile(),[job],ocr.settings(profile()),
                                     None,ocr._SourceVerifier())
            self.assertEqual(counts["FAILED_TERMINAL"],1)
            self.assertEqual(statuses,["FAILED_TERMINAL"])


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1"
    and os.getenv("PGDATABASE")=="embedding_test",
    "Disposable CI PostgreSQL only",
)
class DeferredOcrPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from db import get_conn
        cls.p=profile()
        cls.n=ocr.tables(cls.p)
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                for name in (cls.n["ocr"],cls.n["published"],cls.n["docs"]):
                    cur.execute(f"DROP TABLE IF EXISTS {name}")
                cur.execute(f"""CREATE TABLE {cls.n['docs']} (
                    doc_id text PRIMARY KEY,source_hash text NOT NULL,
                    extraction_hash text NOT NULL,page_count integer NOT NULL)""")
                cur.execute(f"""CREATE TABLE {cls.n['published']} (
                    doc_id text NOT NULL,page_number integer NOT NULL,
                    source_hash text NOT NULL,extraction_hash text NOT NULL,
                    file_path text NOT NULL,elements jsonb NOT NULL,
                    PRIMARY KEY(doc_id,page_number))""")
                elements=[
                    {"type":"image","bbox":[45,50,200,190],
                     "visual_asset":image_ref()},
                    {"type":"vector_region","bbox":[45,200,200,290],
                     "unresolved_region":vector_ref()},
                ]
                cur.execute(f"""INSERT INTO {cls.n['docs']}
                    VALUES (%s,%s,%s,%s)""",
                    ("d"*64,"a"*64,"b"*64,1))
                cur.execute(f"""INSERT INTO {cls.n['published']}
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                    ("d"*64,1,"a"*64,"b"*64,
                     "/tmp/safe-original.pdf",json.dumps(elements)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        ocr.init(cls.p,apply=True)

    @classmethod
    def tearDownClass(cls):
        from db import get_conn
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                for name in (cls.n["ocr"],cls.n["published"],cls.n["docs"]):
                    cur.execute(f"DROP TABLE IF EXISTS {name}")
            conn.commit()
        finally:
            conn.close()

    def test_idempotent_enqueue_claim_version_filter_and_result(self):
        discovery=ocr.enqueue(self.p,doc_id="d"*64,apply=False)
        self.assertEqual(discovery["candidates"],2)
        self.assertEqual(discovery["inserted"],0)
        queued=ocr.enqueue(self.p,doc_id="d"*64,apply=True)
        self.assertEqual(queued["inserted"],2)
        self.assertEqual(
            ocr.enqueue(self.p,doc_id="d"*64,apply=True)["inserted"],0)
        job=ocr.claim(self.p,1,max_attempts=3,lease_seconds=3600)
        self.assertEqual(len(job),1)
        ocr.renew_leases(self.p,job,3600)
        ocr.complete(self.p,job[0],status="TEXT_READY",
                     text="20 mg",regions=[],confidence=0.99,language="en")
        self.assertEqual(ocr.status(self.p)["statuses"]["TEXT_READY"],1)
        second=ocr.claim(self.p,2,3,3600)
        self.assertEqual(len(second),1)
        ocr.complete(self.p,second[0],status="VISUAL_PENDING",
                     error_code="NO_OCR_TEXT")
        self.assertEqual(ocr.claim(self.p,2,3,3600),[])
        self.assertEqual(ocr.status(self.p)["statuses"]["VISUAL_PENDING"],1)


    def test_claim_is_scoped_to_explicit_document_and_status(self):
        """Never charge another document's OCR quota during a single-PDF pilot."""
        from db import get_conn
        doc_id="f"*64
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""INSERT INTO {self.n['docs']}
                    (doc_id,source_hash,extraction_hash,page_count)
                    VALUES (%s,%s,%s,1)""",(doc_id,"a"*64,"b"*64))
                cur.execute(f"""INSERT INTO {self.n['published']}
                    (doc_id,page_number,source_hash,extraction_hash,file_path,elements)
                    VALUES (%s,1,%s,%s,%s,%s::jsonb)""",
                    (doc_id,"a"*64,"b"*64,"/tmp/safe-original.pdf",
                     json.dumps([{"type":"image","bbox":[45,50,200,190],
                                  "visual_asset":image_ref()}])))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(ocr.enqueue(self.p,doc_id=doc_id,apply=True)["inserted"],1)
        self.assertEqual(ocr.claim(self.p,2,3,3600,doc_id="0"*64),[])
        claimed=ocr.claim(self.p,2,3,3600,doc_id=doc_id)
        self.assertEqual(len(claimed),1)
        self.assertTrue(all(j[1]==doc_id for j in claimed))
        self.assertEqual(ocr.status(self.p,doc_id=doc_id)["statuses"]["RUNNING"],1)
        for job in claimed:
            ocr.complete(self.p,job,status="VISUAL_PENDING",error_code="NO_OCR_TEXT")
        self.assertEqual(ocr.status(self.p,doc_id=doc_id)["statuses"]["VISUAL_PENDING"],1)
        self.assertEqual(ocr.claim(self.p,2,3,3600,doc_id=doc_id),[])



if __name__=="__main__":
    unittest.main()
