"""Deployment mapping tests; no real provider credentials or DDL in unit tests."""
import configparser
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production import resolve_configuration,plan,_storage_and_inventory,preflight

REPO=Path(__file__).resolve().parents[1]

def fixture(root):
    import fitz
    from datetime import datetime,timezone,timedelta
    from visual_evidence import digest_file
    c=configparser.ConfigParser()
    c.read(REPO/"config.ini.example")
    c["POSTGRES"].update(host="localhost",database="embedding_test",user="postgres",password="test",
                         schema="intake_ci",native_table="cv_deploy_pages",chunk_table="cv_deploy_chunks")
    dep=c["DEPLOYMENT"]
    for key in ("policy","batch_profile","docx_profile"):
        dep[key]=str(REPO/dep[key])
    dep.update(source_root=str(root/"source"),inventory=str(root/"inventory.jsonl"),
               receipt_dir=str(root/"receipts"),preflight_min_free_gib="1")
    # Fixed profile exercises Windows preflight path, no adaptive guard needed.
    dep["batch_profile"]=str(REPO/"profiles/retention_batch_example.json")
    c["RETENTION"].update(root=str(root/"evidence"),policy_id="ci-test",
                          renderer=str(fitz.VersionBind),authorized_page_derivatives="true",
                          authorized_receipt_storage="true",
                          visual_profile=str(REPO/"profiles/visual_evidence_native.example.json"))
    for key in ("source","receipts","evidence"):
        (root/key).mkdir()
    src=root/"source"/"doc.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((20,40),"Title: Test document")
        pdf.save(src)
    row={"path":str(src),"source_sha256":digest_file(src),
         "purge_at_utc":(datetime.now(timezone.utc)+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (root/"inventory.jsonl").write_text(json.dumps(row)+"\n",encoding="utf-8")
    path=root/"config.ini"
    with path.open("w") as f:c.write(f)
    return path,c

def save(path,c):
    with path.open("w") as f:c.write(f)

class DeploymentTests(unittest.TestCase):
    def test_finish_reports_visual_failure_and_stops_systemic_error(self):
        from production import finish_captured
        from unittest.mock import Mock
        d={"capture":{},"retrieval":{},"visual_llm":({},{}),"translation":{},
           "gcp_key":"unused","authorized_ocr":True,"limits":{"ocr_max_jobs":100},
           "evidence":"unused"}
        d["translation"]={"model":"nmt"}
        d["capture"]={"extraction":{"retained_evidence":{"visual_settings":{}}}}
        d["visual_llm"]=({},{"max_pages":100})
        records=[json.dumps({"doc_id":str(i)*64,"status":"LOCAL_CAPTURE_VERIFIED",
                     "format":"pdf","source_sha256":"a"*64,"capture_id":"b"*64}) for i in (1,2)]
        with (patch("visual_enrichment.LocalQuota"),patch("reuse_materialize.verify_output"),
              patch("translation_stage.GoogleTranslation"),
              patch("deferred_ocr.enqueue",return_value={"candidates":1,"inserted":0}) as enqueue,
              patch("deferred_ocr.retrieval_results",return_value=([],[7])),
              patch("deferred_ocr.run") as ocr_run,
              patch("visual_enrichment.run_pages",return_value=(x for x in [{"status":"FAILED","source_page":7,
                                                                  "error_code":"HTTP_401"}])),
              self.assertLogs("production",level="INFO") as captured):
            result=finish_captured(d,None,records=records)
        self.assertEqual(result,{"FAILED":1})
        self.assertEqual(enqueue.call_count,1)
        ocr_run.assert_not_called()
        self.assertTrue(any("page=7 code=HTTP_401" in line for line in captured.output))

    def test_finish_reuses_visual_pool_and_requires_all_selected_pages(self):
        from production import finish_captured
        d={"capture":{"extraction":{"retained_evidence":{"visual_settings":{}}}},
           "retrieval":{},"visual_llm":({},{"max_pages":1}),"translation":{"model":"nmt"},
           "gcp_key":"unused","authorized_ocr":True,"limits":{"ocr_max_jobs":100},
           "evidence":"unused"}
        records=[json.dumps({"doc_id":"d"*64,"status":"LOCAL_CAPTURE_VERIFIED",
                     "format":"pdf","source_sha256":"a"*64,"capture_id":"b"*64})]
        def outcome(number):
            return {"status":"COMPLETED_UNVERIFIED","source_page":number,
                    "verified_result":{"request_id":str(number),
                        "result":{"observations":[],"limitations":["Requires review"]}}}
        with (patch("visual_enrichment.LocalQuota"),patch("reuse_materialize.verify_output"),
              patch("translation_stage.GoogleTranslation"),
              patch("deferred_ocr.enqueue",return_value={"candidates":3,"inserted":0}),
              patch("deferred_ocr.retrieval_results",return_value=([],[7,8,9])),
              patch("visual_enrichment.run_pages",side_effect=lambda *a,**kw:iter(outcome(n) for n in (7,8,9))) as visual,
              patch("reuse_materialize.run_one",return_value={"status":"SUCCESS"}) as publish):
            self.assertEqual(finish_captured(d,None,records=records),{"SUCCESS":1})
            visual.assert_called_once()
            self.assertEqual(visual.call_args.kwargs["document_pages"],3)
            self.assertEqual([part["page"] for part in publish.call_args.kwargs["enrichment"]],[7,8,9])
            visual.side_effect=lambda *a,**kw:(outcome(n) for n in (7,))
            self.assertEqual(finish_captured(d,None,records=records),{"FAILED":1})
            self.assertEqual(publish.call_count,1)

    def test_optional_enrichment_rejects_invented_source(self):
        from optional_enrichment import validate
        source=[{"chunk_id":"a","text":"A clinical study title\nDose 20 mg"}]
        good=json.dumps({"value":"A clinical study title","citations":[
            {"chunk_id":"a","quote":"A clinical study title"}]})
        self.assertEqual(validate(good,"document_title",source,10000)["status"],"REVIEW_REQUIRED")
        for bad in (
            {"value":"Other title","citations":[{"chunk_id":"a","quote":"A clinical study title"}]},
            {"value":"Other title","citations":[{"chunk_id":"a","quote":"Other title"}]},
            {"value":"Other title","citations":[]},
        ):
            with self.assertRaises(ValueError):
                validate(json.dumps(bad),"document_title",source,10000)
        with self.assertRaisesRegex(ValueError,"Numeric"):
            validate(json.dumps({"value":"Dose 99 mg","citations":[{"chunk_id":"a","quote":"Dose 20 mg"}]}),
                     "table_summary",source,10000)
        self.assertEqual(validate('{"value":"","citations":[]}',"document_title",source,10000)["status"],"NOT_FOUND_IN_SCOPE")

    def test_missing_or_invalid_embedding_dimensions_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,c=fixture(Path(tmp))
            del c["AZURE_OPENAI_EMBEDDING"]["dimensions"]
            save(path,c)
            with self.assertRaisesRegex(ValueError,r"Missing required configuration setting \[AZURE_OPENAI_EMBEDDING\] dimensions"):
                resolve_configuration(path)
            for value,message in (("","Missing required"),("auto","must be an integer"),("0","must be in")):
                c["AZURE_OPENAI_EMBEDDING"]["dimensions"]=value
                save(path,c)
                with self.assertRaisesRegex(ValueError,message):
                    resolve_configuration(path)


    def test_one_table_mapping_and_host_independent_retrieval_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path,c=fixture(root)
            d=resolve_configuration(path)
            self.assertEqual(d["capture"]["extraction"]["table"],"cv_deploy_pages")
            self.assertEqual(d["retrieval"]["extracted_content"]["table"],"cv_deploy_pages")
            self.assertEqual(d["retrieval"]["output"]["table"],"cv_deploy_chunks")
            self.assertEqual(d["retrieval"]["existing_chunks"]["table"],"cv_deploy_chunks")
            self.assertEqual(d["capture"]["deferred_ocr"]["render_dpi"],300)
            self.assertEqual(d["capture"]["deferred_ocr"]["retained_evidence"]["root"],str(root/"evidence"))
            c["RETENTION"]["root"]=str(root/"other_mount")
            save(path,c)
            other=resolve_configuration(path)
            self.assertEqual(d["retrieval"]["_fingerprint"],other["retrieval"]["_fingerprint"])
            self.assertNotEqual(d["capture"]["_fingerprint"],other["capture"]["_fingerprint"])
            c["POSTGRES"]["chunk_table"]="cv_deploy_pages_docs"
            save(path,c)
            with self.assertRaisesRegex(ValueError,"collide"):
                resolve_configuration(path)

    def test_directory_mode_requires_no_inventory(self):
        from retention_batch import iter_source_files
        with tempfile.TemporaryDirectory() as tmp:
            path,c=fixture(Path(tmp))
            c["DEPLOYMENT"]["inventory"]=""
            save(path,c)
            d=resolve_configuration(path)
            self.assertIsNone(d["inventory"])
            self.assertEqual(len(list(iter_source_files(d["source"],100))),1)
            usage=type("Usage",(),{"free":20*1024**3})()
            with patch("production.shutil.disk_usage",return_value=usage),patch("production._database",return_value={"ready":True}):
                report=preflight(d)
            self.assertEqual(report["storage"]["source_mode"],"directory")

    def test_enrichment_chunks_are_distinct_and_bounded(self):
        from reuse_materialize import append_enrichment_rows,quality_summary
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            path,c=fixture(Path(tmp))
            q=resolve_configuration(path)["retrieval"]
            struct=SimpleNamespace(doc_id="d"*64,file_path="/example.pdf",file_name="example.pdf",page_count=2,pages=[])
            parts=[{"page":1,"kind":"ocr","text":"Detected label 20 mg. "*1000,
                    "provenance":{"asset_id":"test"}},
                   {"page":2,"kind":"visual_model","text":"[MODEL OBSERVATION — UNVERIFIED] Image shows a pattern.",
                    "provenance":{"request_id":"test"}}]
            rows=append_enrichment_rows(struct,q,[],parts)
            self.assertEqual(quality_summary(rows,q["chunking"])["structural_errors"],0)
            self.assertEqual("".join(row[6] for row in rows if row[8]==1),parts[0]["text"])
            self.assertEqual(json.loads(rows[-1][13])["source_text_status"],"MODEL_OBSERVATIONS_UNVERIFIED")

    def test_overlap_reads_flushed_receipts_before_capture_finishes(self):
        import threading
        from production import run_capture_and_finish
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path,c=fixture(root)
            d=resolve_configuration(path)
            d["inventory"]=None
            receipt=root/"overlap.jsonl"
            received=threading.Event()
            def capture(source,path,profile,**kw):
                self.assertTrue(kw["stream_receipts"])
                self.assertEqual(kw["config"]["workers"],d["batch"]["workers"]-1)
                with Path(path).open("w",encoding="utf-8") as f:
                    f.write('{"status":"LOCAL_CAPTURE_VERIFIED"}\n')
                    f.flush()
                    self.assertTrue(received.wait(5),"enrichment must consume before capture ends")
                    f.write('{"status":"FAILED"}\n')
                return {"all_local_capture_verified":False}
            def finish(d,path,*,records,admit):
                self.assertIsNone(admit)
                lines=iter(records)
                self.assertEqual(json.loads(next(lines))["status"],"LOCAL_CAPTURE_VERIFIED")
                received.set()
                self.assertEqual([json.loads(line)["status"] for line in lines],["FAILED"])
                return {"SUCCESS":1}
            with patch("retention_batch.run_root",side_effect=capture),patch("production.finish_captured",side_effect=finish):
                report=run_capture_and_finish(d,receipt,overlap=True,finish=True)
            self.assertEqual(report["retrieval"],{"SUCCESS":1})
            self.assertEqual(report["overlap"]["reserved_reader_processes"],1)

    def test_embedding_switch_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,c=fixture(Path(tmp))
            self.assertFalse(resolve_configuration(path)["enable_embeddings"])
            c["PIPELINE"]["enable_embeddings"]="true"
            save(path,c)
            with self.assertRaises(PermissionError):
                resolve_configuration(path)
            c["DEPLOYMENT"]["authorized_embedding"]="true"
            save(path,c)
            self.assertTrue(resolve_configuration(path)["enable_embeddings"])

    def test_preflight_read_only_storage_and_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,c=fixture(Path(tmp))
            d=resolve_configuration(path)
            # Avoid dependence on CI disk free space while checking source and
            # inventory handling. No output file may be created.
            usage=type("Usage",(),{"free":20*1024**3})()
            with patch("production.shutil.disk_usage",return_value=usage),patch("production._database",return_value={"ready":True}):
                report=preflight(d)
            self.assertEqual(report["storage"]["sampled_readable_sources"],1)
            self.assertFalse(report["writes"])
            self.assertEqual(list(d["receipts"].iterdir()),[])
            self.assertEqual(list(d["evidence"].iterdir()),[])
            c["RETENTION"]["authorized_page_derivatives"]="false"
            save(path,c)
            with self.assertRaises(PermissionError):
                resolve_configuration(path)


@unittest.skipUnless(
    __import__("os").getenv("INGESTION_REUSE_TEST_DB")=="1"
    and __import__("os").getenv("PGDATABASE")=="embedding_test",
    "Only disposable PostgreSQL integration")
class DeploymentPostgresTests(unittest.TestCase):
    def test_config_to_preflight_capture_replay_and_chunk_publication(self):
        from production import _database,status
        from page_stage import init
        from reuse_materialize import init_output,run_one,verify_output
        from retention_batch import run_batch
        from db import get_conn
        from profile_store import _safe_identifier
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path,c=fixture(root)
            d=resolve_configuration(path)
            p,q=d["capture"],d["retrieval"]
            conn=get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT current_database()")
                        self.assertEqual(cur.fetchone()[0],"embedding_test")
                        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        for schema,table in reversed(d["relations"]):
                            cur.execute("DROP TABLE IF EXISTS "+_safe_identifier(schema)+"."+_safe_identifier(table))
            finally:
                conn.close()
            try:
                before=_database(d,before_init=True)
                empty=status(d)
                self.assertIsNone(empty["native"])
                self.assertIsNone(empty["retrieval"])
                self.assertIsNone(empty["ocr"])
                self.assertFalse(empty["writes"])
                self.assertFalse(empty["cloud_calls"])
                self.assertFalse(before["native_tables_ready"])
                self.assertFalse(before["retrieval_tables_ready"])
                with self.assertRaisesRegex(ValueError,"absent"):
                    _database(d)
                init(p,apply=True)
                init_output(q,apply=True)
                ready=_database(d)
                self.assertTrue(ready["native_tables_ready"])
                self.assertTrue(ready["retrieval_tables_ready"])
                empty=status(d)
                self.assertEqual(empty["native"]["documents"],0)
                self.assertEqual(empty["retrieval"]["chunks"],0)
                self.assertIsNone(empty["ocr"])
                for i in range(2):
                    receipt=d["receipts"]/f"run-{i}.jsonl"
                    report=run_batch(d["inventory"],receipt,p,docx_config=d["docx"],config=d["batch"])
                    self.assertEqual(report["outcomes"],{"LOCAL_CAPTURE_VERIFIED":1},report)
                    record=json.loads(receipt.read_text())
                    self.assertEqual(record["native_status"],"SUCCESS" if i==0 else "SKIPPED")
                    self.assertEqual(record["rendered_pages"],1 if i==0 else 0)
                # Directory mode requires neither deadline nor input inventory.
                from retention_batch import run_root
                extra=d["receipts"]/"directory-replay.jsonl"
                report=run_root(d["source"],extra,p,docx_config=d["docx"],config=d["batch"])
                self.assertEqual(report["outcomes"],{"LOCAL_CAPTURE_VERIFIED":1})
                record=json.loads(extra.read_text())
                self.assertEqual(record["native_status"],"SKIPPED")
                self.assertTrue(record["source_sha256"])
                import hashlib
                doc_id=hashlib.sha256(str((d["source"]/"doc.pdf").resolve()).encode()).hexdigest()
                n=verify_output(q)
                self.assertEqual(q["output"]["text_contract"],"english_v1")
                self.assertEqual(run_one(doc_id,q,n)["status"],"FAILED")
                # Actual root -> native DB -> retained OCR and model -> same
                # chunk table. Real PDF bytes, real DB, actual provider
                # adapters mocked only at network boundary.
                import io
                import fitz
                from PIL import Image
                from vision_ocr import OcrResult
                import deferred_ocr as ocr
                from production import finish_captured
                src=d["source"]/"scanned.pdf"
                image=io.BytesIO()
                Image.new("RGB",(240,180),"white").save(image,format="PNG")
                with fitz.open() as pdf:
                    page=pdf.new_page(width=240,height=180)
                    page.insert_image(page.rect,stream=image.getvalue())
                    pdf.save(src)
                receipt=d["receipts"]/"full-run.jsonl"
                cap=run_root(d["source"],receipt,p,docx_config=d["docx"],config=d["batch"])
                self.assertEqual(cap["outcomes"],{"LOCAL_CAPTURE_VERIFIED":2},cap)
                scanned=next(x for x in map(json.loads,receipt.read_text().splitlines()) if x["doc_id"]!=doc_id)
                self.assertEqual(scanned["format"],"pdf")
                src.unlink()
                ocr.init(p,apply=True)
                model=json.loads((REPO/"profiles/visual_enrichment_example.json").read_text())
                execution=json.loads((REPO/"profiles/visual_enrichment_execution_example.json").read_text())
                model["authorized_provider"]=True
                execution.update(authorized_result_storage=True,workers=2,read_workers=1,rpm=60000,tpm=10000000)
                d.update(authorized_ocr=True,visual_llm=(model,execution),
                         translation={"model":"nmt","rpm":10000,"max_chars":30000,"timeout_seconds":60,"attempts":1})
                raw=json.dumps({"observations":[{"target":"page","observation":"A visual field is visible",
                    "caption_quote":"","uncertainty":"limited"}],"limitations":[]})
                from unittest.mock import Mock
                session=Mock()
                session.post.return_value.status_code=200
                def translation_reply(*args,**kwargs):
                    response=Mock()
                    response.status_code=200
                    response.json.return_value={"data":{"translations":[
                        {"detectedSourceLanguage":"en" if "MODEL" in value else "fr",
                         "translatedText":value if "MODEL" in value else "Translated clinical document"}
                        for value in kwargs["json"]["q"]]}}
                    return response
                session.post.side_effect=translation_reply
                with (patch("translation_stage._google_session",return_value=session),
                      patch("ocr_quota.detect_project_id",return_value="ci-only"),
                      patch("ocr_quota.init_quota",return_value="ci-only"),
                      patch("ocr_quota.PostgresOcrLimiter"),
                      patch("vision_ocr.batch_document_text_detection",
                            side_effect=lambda images,**kw:[OcrResult("",0,0,"",True) for _ in images]) as vision,
                      patch("azure_client.call_visual_structured",return_value=(raw,{})) as llm):
                    first=finish_captured(d,receipt)
                    self.assertEqual(first,{"SUCCESS":2},first)
                    self.assertGreater(vision.call_count,0)
                    self.assertEqual(llm.call_count,1)
                    self.assertEqual(session.post.call_count,2)
                    counts=vision.call_count,llm.call_count,session.post.call_count
                    second=finish_captured(d,receipt)
                    self.assertEqual(second,{"SKIPPED":2},second)
                    self.assertEqual((vision.call_count,llm.call_count,session.post.call_count),counts)
                report=status(d)
                self.assertEqual(report["native"]["documents"],2)
                self.assertEqual(report["native"]["pages"],2)
                self.assertEqual(report["retrieval"]["documents"],2)
                self.assertGreater(report["retrieval"]["chunks"],0)
                self.assertTrue(report["retrieval"]["profile_matches"])
                self.assertEqual(report["ocr"]["statuses"],{"VISUAL_PENDING":1})
                from optional_enrichment import run as enrich
                d["optional_enrichment"]={"enabled":True,"metadata":True,"table_summaries":False,
                     "first_pages":10,"max_context_bytes":16000,"max_requests":1}
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(f"UPDATE {n['table']} SET document_metadata=document_metadata-'document_title' WHERE doc_id=%s",(doc_id,))
                            cur.execute(f"SELECT chunk_id FROM {n['table']} WHERE doc_id=%s",(doc_id,))
                            cid=cur.fetchone()[0]
                finally:
                    conn.close()
                preview=enrich(d,doc_id)
                self.assertEqual(preview["outcomes"],{"ELIGIBLE":1})
                self.assertFalse(preview["writes"])
                candidate=json.dumps({"value":"Test document","citations":[{"chunk_id":cid,"quote":"Test document"}]})
                with patch("azure_client.call_visual_structured",return_value=(candidate,{})) as api:
                    self.assertEqual(enrich(d,doc_id,apply=True)["outcomes"],{"REVIEW_REQUIRED":1})
                    self.assertEqual(enrich(d,doc_id,apply=True)["outcomes"],{"SKIPPED":1})
                    self.assertEqual(api.call_count,1)
                other={**d,"retrieval":{**q,"_fingerprint":"0"*64}}
                self.assertFalse(status(other)["retrieval"]["profile_matches"])
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"""SELECT chunk_text,chunk_metadata->>'source_text_status',native_text
                            FROM {n['table']} WHERE doc_id=%s""",(scanned["doc_id"],))
                        result=cur.fetchall()
                        self.assertTrue(any("A visual field is visible" in x[0] and x[1]=="MODEL_OBSERVATIONS_UNVERIFIED" and x[2] is None for x in result))
                        cur.execute(f"SELECT native_text,chunk_text FROM {n['table']} WHERE doc_id=%s",(doc_id,))
                        native,english=cur.fetchone()
                        self.assertIn("Title: Test document",native)
                        self.assertEqual(english,"Translated clinical document")
                finally:
                    conn.close()
            finally:
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            for schema,table in reversed(d["relations"]):
                                cur.execute("DROP TABLE IF EXISTS "+_safe_identifier(schema)+"."+_safe_identifier(table))
                finally:
                    conn.close()


if __name__=="__main__":
    unittest.main()
