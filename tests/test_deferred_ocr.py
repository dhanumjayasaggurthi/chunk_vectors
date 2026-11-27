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
    def test_retained_crop_normalizes_only_subpixel_boundary_noise(self):
        from visual_evidence import capture, read_region
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=make_pdf(folder)
            visual={"dpi":300,"workers":1}
            captured=capture(source,root/"retained",1,1,settings=visual,
                             policy_id="synthetic-test",authorized_page_derivatives=True)
            cid=captured["capture_id"]
            source.unlink()
            expected,expected_meta=read_region(root/"retained",cid,1,[7.8,0,604.2,792],
                                               settings=visual)
            requested=[7.8,-0.00006103515625,604.2,792]
            actual,meta=read_region(root/"retained",cid,1,requested,settings=visual)
            self.assertEqual(actual,expected)
            self.assertEqual(meta["pixel_bbox"],expected_meta["pixel_bbox"])
            self.assertEqual(meta["requested_bbox_pt"],requested)
            for bbox in ([7.8,-0.01,604.2,792],[7.8,0,613,792],
                         [-0.01,0,604.2,792],[7.8,0,604.2,793]):
                with self.assertRaisesRegex(ValueError,"outside retained page"):
                    read_region(root/"retained",cid,1,bbox,settings=visual)

    def test_retained_ocr_uses_existing_queue_without_original_or_cloud(self):
        import concurrent.futures
        import multiprocessing
        import fitz
        from visual_evidence import capture, read_region, _paths
        from vision_ocr import OcrResult
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=make_pdf(folder)
            sha=hashlib.sha256(source.read_bytes()).hexdigest()
            doc_id=hashlib.sha256(str(source).encode("utf-8")).hexdigest()
            visual=dict(dpi=200,workers=1)
            result=capture(source,root/"retained",1,1,settings=visual,
                           policy_id="synthetic-test",authorized_page_derivatives=True)
            p={**profile(),"deferred_ocr":{"retained_evidence":{
                "root":str(root/"retained"),"policy_id":"synthetic-test",
                "renderer":str(fitz.VersionBind),"visual_settings":visual,"read_workers":2}}}
            cfg=ocr.settings(p)
            self.assertNotEqual(ocr.ocr_fingerprint(cfg),ocr.ocr_fingerprint(ocr.settings(profile())))
            tuned={**cfg,"retained_evidence":{**cfg["retained_evidence"],"read_workers":1}}
            self.assertEqual(ocr.ocr_fingerprint(cfg),ocr.ocr_fingerprint(tuned))
            job=("retained-job",doc_id,sha,"e"*64,1,"image",[45,50,200,190],1,"lease")
            expected,_=read_region(root/"retained",result["capture_id"],1,job[6],settings=visual)
            source.unlink()
            with concurrent.futures.ProcessPoolExecutor(
                    max_workers=2,mp_context=multiprocessing.get_context("spawn")) as pool:
                with (patch("deferred_ocr._file_path",side_effect=AssertionError("source lookup forbidden")),
                      patch("deferred_ocr.renew_leases"),
                      patch("deferred_ocr.complete") as completed,
                      patch("vision_ocr.batch_document_text_detection",
                            return_value=[OcrResult("Dose 20 mg",.98,3,"en",True)]) as api):
                    report=ocr.run_group(p,[job],cfg,None,ocr._SourceVerifier(),pool)
                    self.assertEqual(report["TEXT_READY"],1)
                    self.assertEqual(api.call_args.args[0],[expected])
                    self.assertEqual(completed.call_args.kwargs["status"],"TEXT_READY")
            png,_=_paths(root/"retained",result["capture_id"],1)
            with png.open("ab") as stream:
                stream.write(b"tamper")
            with (patch("deferred_ocr._file_path",side_effect=AssertionError("source lookup forbidden")),
                  patch("deferred_ocr.renew_leases"),
                  patch("deferred_ocr.complete") as completed,
                  patch("vision_ocr.batch_document_text_detection") as api):
                report=ocr.run_group(p,[job],cfg,None,ocr._SourceVerifier())
                self.assertEqual(report["FAILED_TERMINAL"],1)
                api.assert_not_called()
                self.assertIn("checksum",completed.call_args.kwargs["error_message"])
            bad={**p,"deferred_ocr":{**p["deferred_ocr"],"render_dpi":300}}
            with self.assertRaisesRegex(ValueError,"DPI"):
                ocr.settings(bad)

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
                 [45,50,200,190] if i==0 else [45,100,200,190],
                 1,f"lease-{i}")
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

    def test_identical_inputs_share_ocr_not_source_identity(self):
        from vision_ocr import OcrResult
        with tempfile.TemporaryDirectory() as folder:
            path=make_pdf(folder)
            doc_id=hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            source=hashlib.sha256(path.read_bytes()).hexdigest()
            jobs=[(f"job-{i}",doc_id,source,"e"*64,1,"image",
                   [45,50,200,190] if i<2 else [45,100,200,190],
                   1,f"lease-{i}") for i in range(3)]
            completed=[]
            with (patch("deferred_ocr._file_path",return_value=path),
                  patch("deferred_ocr.renew_leases"),
                  patch("deferred_ocr.complete",
                        side_effect=lambda p,j,**kw:completed.append((j[0],j[8],kw))),
                  patch("vision_ocr.batch_document_text_detection",
                        return_value=[OcrResult("Dose 20 mg",.98,3,"en",True),
                                      OcrResult("",0,0,"",True)]) as api):
                result=ocr.run_group(profile(),jobs,ocr.settings(profile()),
                                     None,ocr._SourceVerifier())
                self.assertEqual(len(api.call_args.args[0]),2)
            self.assertEqual(result,{"TEXT_READY":2,"VISUAL_PENDING":1})
            self.assertEqual([(x[0],x[1]) for x in completed],
                             [(f"job-{i}",f"lease-{i}") for i in range(3)])
            self.assertEqual(completed[0][2]["text"],completed[1][2]["text"])
            self.assertEqual(completed[2][2]["error_code"],"NO_OCR_TEXT")

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
        job=ocr.claim(self.p,1,max_attempts=3,lease_seconds=3600,doc_id="d"*64)
        self.assertEqual(len(job),1)
        ocr.renew_leases(self.p,job,3600)
        ocr.complete(self.p,job[0],status="TEXT_READY",
                     text="20 mg",regions=[],confidence=0.99,language="en")
        self.assertEqual(ocr.status(self.p,doc_id="d"*64)["statuses"]["TEXT_READY"],1)
        second=ocr.claim(self.p,2,3,3600,doc_id="d"*64)
        self.assertEqual(len(second),1)
        ocr.complete(self.p,second[0],status="VISUAL_PENDING",
                     error_code="NO_OCR_TEXT")
        self.assertEqual(ocr.claim(self.p,2,3,3600,doc_id="d"*64),[])
        self.assertEqual(ocr.status(self.p,doc_id="d"*64)["statuses"]["VISUAL_PENDING"],1)
        parts,pages=ocr.retrieval_results(self.p,"d"*64,"a"*64,2)
        self.assertEqual([x["text"] for x in parts],["20 mg"])
        self.assertEqual(pages,[1])
        with self.assertRaisesRegex(ValueError,"coverage"):
            ocr.retrieval_results(self.p,"d"*64,"a"*64,3)


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



    def test_retained_source_bytes_support_ocr_after_original_purge(self):
        """Regression: an independently verified archive outlives intake."""
        import os
        from source_evidence_archive import capture
        from db import get_conn

        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            path=make_pdf(folder)
            contents=path.read_bytes()
            source_sha=hashlib.sha256(contents).hexdigest()
            doc_id=hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            version="b"*64
            storage=root/"authorized-archive"
            result=capture(path,storage,authorized_retention=True)
            self.assertTrue(result["content_verified"])
            conn=get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {self.n['docs']}
                          (doc_id,source_hash,extraction_hash,page_count)
                        VALUES (%s,%s,%s,1)
                    """,(doc_id,source_sha,version))
                    cur.execute(f"""
                        INSERT INTO {self.n['published']}
                          (doc_id,page_number,source_hash,extraction_hash,
                           file_path,elements)
                        VALUES (%s,1,%s,%s,%s,'[]'::jsonb)
                    """,(doc_id,source_sha,version,str(path)))
                conn.commit()
            finally:
                conn.close()
            path.unlink()
            with patch.dict(os.environ,{"SOURCE_EVIDENCE_ARCHIVE_ROOT":str(storage)}):
                recovered=ocr._file_path(self.p,doc_id,source_sha,version)
                self.assertEqual(Path(recovered).read_bytes(),contents)
                verifier=ocr._SourceVerifier()
                state=verifier.verify(recovered,doc_id,source_sha)
                verifier.ensure_unchanged(recovered,state)
            with patch.dict(os.environ,{"SOURCE_EVIDENCE_ARCHIVE_ROOT":""}):
                with self.assertRaisesRegex(FileNotFoundError,"authorized"):
                    ocr._file_path(self.p,doc_id,source_sha,version)

    def test_retry_only_claim_does_not_consume_pending_ocr_assets(self):
        """An eight-asset recovery pilot must not claim eight new PDFs/assets."""
        from db import get_conn
        doc_id="e"*64
        source="a"*64
        version="b"*64
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""INSERT INTO {self.n['docs']}
                    (doc_id,source_hash,extraction_hash,page_count)
                    VALUES (%s,%s,%s,2)""",(doc_id,source,version))
                for page_number in (1,2):
                    elements=[{
                        "type":"image", "bbox":[45,50,200,190],
                        "visual_asset":image_ref(page_number),
                    }]
                    cur.execute(f"""INSERT INTO {self.n['published']}
                        (doc_id,page_number,source_hash,extraction_hash,file_path,elements)
                        VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                        (doc_id,page_number,source,version,
                         "/tmp/no-source-pdf-open-for-claim.pdf",
                         json.dumps(elements)))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            ocr.enqueue(self.p,doc_id=doc_id,apply=True)["inserted"],2)
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""UPDATE {self.n['ocr']}
                    SET status='RETRY_WAIT', attempts=1,
                        error_code='ModuleNotFoundError',
                        next_attempt_at=now()-interval '5 seconds'
                    WHERE doc_id=%s AND page_number=1""",(doc_id,))
                self.assertEqual(cur.rowcount,1)
            conn.commit()
        finally:
            conn.close()
        jobs=ocr.claim(self.p,8,3,3600,doc_id=doc_id,retry_only=True)
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0][4],1)
        self.assertEqual(jobs[0][7],2)
        self.assertEqual(
            ocr.status(self.p,doc_id=doc_id)["statuses"],
            {"RUNNING":1,"PENDING":1})
        ocr.complete(self.p,jobs[0],status="TEXT_READY",
                     text="Readable",regions=[],confidence=.97,language="en")
        self.assertEqual(
            ocr.status(self.p,doc_id=doc_id)["statuses"],
            {"TEXT_READY":1,"PENDING":1})
        self.assertEqual(
            ocr.claim(self.p,8,3,3600,doc_id=doc_id,retry_only=True),[])
        with self.assertRaisesRegex(ValueError,"explicit document"):
            ocr.claim(self.p,8,3,3600,retry_only=True)

    def test_z_large_enqueue_releases_reads_before_insert_under_pool_cap_2(self):
        """Reproduce the user's 30-second pool exhaustion with 70 pages/140 assets.

        The test enforces a hard two-lease cap even if CI runs with pooling
        disabled. Keyset pagination (32 pages) and a full 128-asset upsert
        must occur with no generator-suspended DB connections.
        """
        from db import get_conn as real_get_conn
        from psycopg2.extras import execute_values

        doc_id="c"*64
        source="a"*64
        version="b"*64
        pages=[]
        for page_no in range(1,71):
            elements=[
                {"type":"image","bbox":[45,50,200,190],
                 "visual_asset":image_ref(page_no)},
                {"type":"vector_region","bbox":[45,200,200,290],
                 "unresolved_region":vector_ref(page_no)},
            ]
            pages.append((
                doc_id,page_no,source,version,
                "/tmp/unchanged-original.pdf",json.dumps(elements),
            ))
        conn=real_get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self.n['docs']} VALUES (%s,%s,%s,%s)",
                    (doc_id,source,version,70),
                )
                execute_values(cur,f"""
                    INSERT INTO {self.n['published']}
                      (doc_id,page_number,source_hash,extraction_hash,
                       file_path,elements)
                    VALUES %s
                """,pages,template="(%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=70)
            conn.commit()
        finally:
            conn.close()

        leased=0
        peak=0

        class TrackedLease:
            def __init__(self,lease):
                self.lease=lease
                self.closed=False

            def __getattr__(self,key):
                return getattr(self.lease,key)

            def close(self):
                nonlocal leased
                if not self.closed:
                    self.closed=True
                    try:
                        self.lease.close()
                    finally:
                        leased-=1

        def tracked_get_conn():
            nonlocal leased,peak
            if leased>=2:
                raise AssertionError(
                    "OCR enqueue acquired a third PostgreSQL lease"
                )
            raw=real_get_conn()
            leased+=1
            peak=max(peak,leased)
            return TrackedLease(raw)

        with patch("db.get_conn",side_effect=tracked_get_conn):
            result=ocr.enqueue(self.p,doc_id=doc_id,apply=True)
            self.assertEqual(result["documents"],1)
            self.assertEqual(result["candidates"],140)
            self.assertEqual(result["inserted"],140)
            self.assertEqual(
                ocr.enqueue(self.p,doc_id=doc_id,apply=True)["inserted"],0
            )
        self.assertEqual(leased,0)
        self.assertLessEqual(peak,1)
        self.assertEqual(
            ocr.status(self.p,doc_id=doc_id)["statuses"],{"PENDING":140}
        )



if __name__=="__main__":
    unittest.main()
